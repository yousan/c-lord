"""Claude Code runner that executes inside a tmux window.

Instead of spawning a subprocess with ``-p --output-format stream-json``,
this runner starts Claude in TUI mode inside a tmux pane and polls
``tmux capture-pane`` to extract text changes.

The trade-off is that structured stream events (tool use, thinking, etc.)
are not available — only plain text deltas.  However, users can
``tmux attach -t clord:workN`` to see the full Claude TUI in real time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator

from ..tmux import TmuxSessionManager
from .types import MessageType, StreamEvent

logger = logging.getLogger(__name__)

# How often to poll capture-pane (seconds).
_POLL_INTERVAL = 0.5

# If no text change is detected for this many seconds, consider Claude done.
_IDLE_TIMEOUT = 8.0

# How long to wait for Claude to become ready (show input prompt).
_STARTUP_TIMEOUT = 30.0

# Patterns that indicate Claude is waiting for user input (response complete).
# The TUI shows these prompts when Claude finishes its response.
_INPUT_PROMPT_MARKERS = (
    "\n❯ ",
    "\n> ",
    "\n❯",
)

# Patterns that indicate a trust/safety prompt that needs Enter to dismiss.
_TRUST_PROMPT_MARKERS = (
    "Yes, I trust this folder",
    "Enter to confirm",
)

# Patterns that indicate a permission/approval prompt that needs "Yes".
_PERMISSION_PROMPT_MARKERS = (
    "Do you want to proceed?",
    "This command requires approval",
)


class TmuxClaudeRunner:
    """Runs Claude Code inside a tmux window and streams output via capture-pane.

    Provides the same public interface as ``ClaudeRunner`` (``run``,
    ``interrupt``, ``kill``) so it can be used interchangeably.
    """

    def __init__(
        self,
        tmux_manager: TmuxSessionManager,
        thread_id: int,
        model: str = "sonnet",
        working_dir: str | None = None,
        timeout_seconds: int = 300,
        permission_mode: str = "acceptEdits",
        dangerously_skip_permissions: bool = False,
    ) -> None:
        self._tmux = tmux_manager
        self._thread_id = thread_id
        self.model = model
        self.working_dir = working_dir
        self.timeout_seconds = timeout_seconds
        self._permission_mode = permission_mode
        self._dangerously_skip_permissions = dangerously_skip_permissions
        self._stopped = False
        # Track the last captured pane text for diffing.
        self._last_capture: str = ""

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Start Claude in tmux and yield text diff events.

        1. Check if Claude is already running; if not, start it.
        2. Wait for Claude to be ready (input prompt visible).
        3. Handle trust/safety prompts automatically.
        4. Send the user's prompt.
        5. Poll ``capture-pane`` and yield text deltas as ASSISTANT events.
        6. Detect completion (input prompt reappears or idle timeout).
        7. Yield a final RESULT event with ``is_complete=True``.
        """
        self._stopped = False

        # Start Claude or send a new prompt to an already-running instance.
        claude_running = await asyncio.to_thread(self._tmux.is_claude_running, self._thread_id)

        if claude_running:
            # Claude is already running and waiting for input — send prompt.
            ok = await asyncio.to_thread(self._tmux.send_input, self._thread_id, prompt)
            if not ok:
                yield StreamEvent(
                    raw={},
                    message_type=MessageType.RESULT,
                    is_complete=True,
                    error="Failed to send input to Claude in tmux",
                )
                return
        else:
            # Start Claude fresh with the prompt as a CLI argument.
            # (Without a prompt, `claude` tries to resume and exits with
            # "No conversation found to continue".)
            ok = await asyncio.to_thread(
                self._tmux.start_claude,
                self._thread_id,
                prompt,
                self.model,
                permission_mode=self._permission_mode,
                dangerously_skip_permissions=self._dangerously_skip_permissions,
            )
            if not ok:
                yield StreamEvent(
                    raw={},
                    message_type=MessageType.RESULT,
                    is_complete=True,
                    error="Failed to start Claude in tmux",
                )
                return

            # Handle trust prompt if it appears.
            await self._handle_startup_prompts()

        # Wait a moment then snapshot.
        await asyncio.sleep(1.0)
        self._last_capture = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)

        # Poll capture-pane for text changes.
        idle_seconds = 0.0
        elapsed = 0.0
        accumulated_text = ""

        while not self._stopped and elapsed < self.timeout_seconds:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            current = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)

            if current != self._last_capture:
                # Compute delta text
                delta = self._compute_delta(self._last_capture, current)
                self._last_capture = current
                idle_seconds = 0.0

                if delta:
                    accumulated_text += delta
                    yield StreamEvent(
                        raw={},
                        message_type=MessageType.ASSISTANT,
                        text=accumulated_text,
                        is_partial=True,
                    )

                # Auto-accept permission prompts so the bot doesn't stall.
                if self._has_permission_prompt(current):
                    await self._accept_permission_prompt()

                # Check if Claude has returned to input prompt
                elif self._has_input_prompt(current):
                    break
            else:
                # During a fresh start, suppress idle timeout until we've
                # seen some output (Claude's TUI may be static while loading).
                # For already-running sessions, always apply idle timeout.
                if claude_running or accumulated_text:
                    idle_seconds += _POLL_INTERVAL
                    if idle_seconds >= _IDLE_TIMEOUT:
                        logger.info(
                            "Idle timeout (%.1fs) — assuming Claude finished (thread=%d)",
                            idle_seconds,
                            self._thread_id,
                        )
                        break

        # Yield final complete event.
        if self._stopped:
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                text=accumulated_text or None,
                error="Stopped by user",
            )
        elif elapsed >= self.timeout_seconds:
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error=f"Timed out after {self.timeout_seconds} seconds",
            )
        else:
            # Normal completion — yield the final full text as non-partial,
            # then the result event.
            if accumulated_text:
                yield StreamEvent(
                    raw={},
                    message_type=MessageType.ASSISTANT,
                    text=accumulated_text,
                    is_partial=False,
                )
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                text=accumulated_text or None,
            )

    async def interrupt(self) -> None:
        """Send C-c to the tmux pane (graceful interrupt)."""
        self._stopped = True
        await asyncio.to_thread(self._tmux.send_interrupt, self._thread_id)

    async def kill(self) -> None:
        """Kill the tmux window entirely."""
        self._stopped = True
        await asyncio.to_thread(self._tmux.kill_session, self._thread_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _handle_startup_prompts(self) -> None:
        """Handle any interactive prompts during Claude startup.

        After ``start_claude`` sends the command, Claude may show a
        "trust this folder?" prompt that needs Enter to dismiss.
        This method polls for up to ``_STARTUP_TIMEOUT`` seconds,
        handling any prompts it finds.
        """
        elapsed = 0.0
        trust_handled = False

        while elapsed < _STARTUP_TIMEOUT and not self._stopped:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            pane = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)

            # Check for trust/safety prompt — press Enter to accept.
            if not trust_handled and self._has_trust_prompt(pane):
                logger.info(
                    "Trust prompt detected, sending Enter to accept (thread=%d)",
                    self._thread_id,
                )
                # Send just Enter via send-keys (not send_input which adds text).
                from ..tmux import SESSION_NAME, _run

                window = self._tmux._find_window_for_thread(self._thread_id)
                if window:
                    _run(["tmux", "send-keys", "-t", f"{SESSION_NAME}:{window}", "Enter"])
                trust_handled = True
                await asyncio.sleep(1.0)
                elapsed += 1.0
                continue

            # Once Claude is processing (no trust prompt, not idle shell),
            # we can return and let the polling loop take over.
            if trust_handled or elapsed > 3.0:
                return

        logger.info(
            "Startup prompts handled in %.1fs (thread=%d)",
            elapsed,
            self._thread_id,
        )

    @staticmethod
    def _has_trust_prompt(text: str) -> bool:
        """Check if the pane shows a trust/safety confirmation prompt."""
        return any(marker in text for marker in _TRUST_PROMPT_MARKERS)

    @staticmethod
    def _has_permission_prompt(text: str) -> bool:
        """Check if the pane shows a permission/approval prompt."""
        return any(marker in text for marker in _PERMISSION_PROMPT_MARKERS)

    async def _accept_permission_prompt(self) -> None:
        """Auto-accept a permission prompt by pressing Enter.

        The TUI defaults to ``❯ 1. Yes``, so pressing Enter confirms it.
        """
        logger.info(
            "Permission prompt detected, auto-accepting (thread=%d)",
            self._thread_id,
        )
        from ..tmux import SESSION_NAME, _run

        window = self._tmux._find_window_for_thread(self._thread_id)
        if window:
            _run(["tmux", "send-keys", "-t", f"{SESSION_NAME}:{window}", "Enter"])

    @staticmethod
    def _compute_delta(old: str, new: str) -> str:
        """Compute the text that was added between two pane captures.

        Uses a simple suffix-based approach: find the longest common prefix
        and return the remainder.  This works well because tmux pane output
        is append-mostly (new text appears at the bottom).
        """
        # Strip trailing whitespace from both to normalize
        old_stripped = old.rstrip()
        new_stripped = new.rstrip()

        if new_stripped.startswith(old_stripped):
            return new_stripped[len(old_stripped) :]

        # If the old text isn't a prefix (e.g. screen was redrawn),
        # find the longest common suffix of old lines in new.
        old_lines = old_stripped.splitlines()
        new_lines = new_stripped.splitlines()

        # Find where old ends in new
        overlap = 0
        for i in range(min(len(old_lines), len(new_lines)), 0, -1):
            if old_lines[-i:] == new_lines[:i]:
                overlap = i
                break

        if overlap > 0:
            added_lines = new_lines[overlap:]
            return "\n".join(added_lines)

        # Fallback: return all new content
        return new_stripped

    @staticmethod
    def _has_input_prompt(text: str) -> bool:
        """Check if the pane text contains a Claude input prompt near the bottom.

        The TUI shows a status bar (``-- INSERT --``, separator lines) below
        the ``❯`` prompt, so we cannot simply check ``endswith``.  Instead,
        look at the last few lines for a line that is *only* the prompt
        character (with optional whitespace).
        """
        lines = text.rstrip().splitlines()
        # Check the last 6 lines for a bare prompt line.
        for line in lines[-6:]:
            stripped_line = line.strip()
            if stripped_line in ("❯", ">"):
                return True
        return False
