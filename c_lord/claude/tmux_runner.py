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
import re
from collections.abc import AsyncGenerator

from ..tmux import TmuxSessionManager
from .types import MessageType, StreamEvent

logger = logging.getLogger(__name__)

# How often to poll capture-pane (seconds).
_POLL_INTERVAL = 0.5

# If extracted response text hasn't changed for this long, consider Claude done.
_RESPONSE_STABLE_TIMEOUT = 3.0

# If no response appears at all for this long, give up (idle timeout).
_IDLE_TIMEOUT = 15.0

# How long to wait for Claude to become ready (show input prompt).
_STARTUP_TIMEOUT = 30.0

# Delay after startup before polling begins (seconds).
_POST_STARTUP_DELAY = 1.0

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

# Regex for separator lines (all box-drawing horizontal characters).
_SEPARATOR_RE = re.compile(r"^[─━═─\s]{10,}$")

# TUI status bar patterns at the very bottom.
_STATUS_BAR_MARKERS = ("-- INSERT --", "-- NORMAL --", "⏵⏵", "⏸⏸")

# TUI generation status indicators (shown during Claude's response generation).
# These appear between two separator lines at the bottom of the pane.
# Claude uses various Unicode dingbats (✻ ✽ ✹ ✦ etc.) as thinking animations
# (e.g. "✻ Envisioning…") and completion summaries (e.g. "✻ Cooked for 56s").
# tmux capture-pane sometimes converts dingbats to plain ASCII (e.g. ✻ → *),
# so we match both the Dingbats Unicode block and common fallback characters.
_GENERATION_STATUS_RE = re.compile(r"^(?!❯)[\u2700-\u27BF*·] .+$")
# Additional explicit markers.
_GENERATION_STATUS_MARKERS = ("Tip:", "·")

# Lines to strip from the response (TUI hints, not useful on Discord).
_STRIP_PATTERNS = (
    re.compile(r"● Recalled \d+ memor(?:y|ies).*"),
    re.compile(r"\(ctrl\+o to expand\)"),
    # Thinking animations: "✻ Forming…", "* Moseying…", "· Thinking…"
    re.compile(r"[^\w\s●⎿❯>] \w+…"),
    # Completion summaries: "✻ Cooked for 56s", "* Worked for 3s"
    re.compile(r"[^\w\s●⎿❯>] \w+ for \d+s?"),
    # TUI noise — interactive prompts and greetings
    re.compile(r"Press Ctrl-C again to exit"),
    re.compile(r"Hi! How can I help you.*"),
    re.compile(r"Claude Code has switched.*"),
    # Vim-style status bar lines that leak into the response area
    re.compile(r"--\s*INSERT\s.*"),
    re.compile(r"--\s*NORMAL\s.*"),
    # ASCII hyphen separator lines (5+ hyphens)
    re.compile(r"^-{5,}$"),
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
        self._last_capture: str = ""

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Start Claude in tmux and yield extracted response events.

        1. Check if Claude is already running; if not, start it.
        2. Handle trust/safety prompts automatically.
        3. Send the user's prompt.
        4. Poll ``capture-pane`` and extract Claude's response text.
        5. Yield response text as ASSISTANT events (cumulative).
        6. Detect completion (input prompt reappears or idle timeout).
        7. Yield a final RESULT event with ``is_complete=True``.
        """
        self._stopped = False

        # Start Claude or send a new prompt to an already-running instance.
        claude_running = await asyncio.to_thread(self._tmux.is_claude_running, self._thread_id)

        if claude_running:
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
        await asyncio.sleep(_POST_STARTUP_DELAY)

        # Poll capture-pane and extract response text.
        # Completion is detected by the response text stabilising (not changing
        # for _RESPONSE_STABLE_TIMEOUT seconds).  This is more reliable than
        # looking for bare "❯" prompts, because the Claude TUI always shows
        # the input area ❯ at the bottom, even during generation.
        elapsed = 0.0
        stable_seconds = 0.0
        last_response = ""

        while not self._stopped and elapsed < self.timeout_seconds:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            current = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)

            # Auto-accept permission prompts so the bot doesn't stall.
            if self._has_permission_prompt(current):
                await self._accept_permission_prompt()
                continue

            # Extract the clean response from the TUI pane.
            response = self._extract_response(current)

            if response != last_response:
                # Response changed (new content appeared or grew).
                stable_seconds = 0.0
                if response:
                    last_response = response
                    yield StreamEvent(
                        raw={},
                        message_type=MessageType.ASSISTANT,
                        text=last_response,
                        is_partial=True,
                    )
            else:
                stable_seconds += _POLL_INTERVAL

            # Done: non-empty response has been stable.
            if last_response and stable_seconds >= _RESPONSE_STABLE_TIMEOUT:
                break

            # Idle timeout: no response received for too long.
            if not last_response and stable_seconds >= _IDLE_TIMEOUT:
                # During a fresh start, allow extra time for Claude to load.
                if not claude_running and elapsed < _STARTUP_TIMEOUT:
                    continue
                logger.info(
                    "Idle timeout (%.1fs) — no response (thread=%d)",
                    stable_seconds,
                    self._thread_id,
                )
                break

        # Yield final complete event.
        if self._stopped:
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                text=last_response or None,
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
            if last_response:
                yield StreamEvent(
                    raw={},
                    message_type=MessageType.ASSISTANT,
                    text=last_response,
                    is_partial=False,
                )
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                text=last_response or None,
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
        """Handle any interactive prompts during Claude startup."""
        elapsed = 0.0
        trust_handled = False

        while elapsed < _STARTUP_TIMEOUT and not self._stopped:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            pane = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)

            if not trust_handled and self._has_trust_prompt(pane):
                logger.info(
                    "Trust prompt detected, sending Enter to accept (thread=%d)",
                    self._thread_id,
                )
                from ..tmux import SESSION_NAME, _run

                window = self._tmux._find_window_for_thread(self._thread_id)
                if window:
                    _run(["tmux", "send-keys", "-t", f"{SESSION_NAME}:{window}", "Enter"])
                trust_handled = True
                await asyncio.sleep(1.0)
                elapsed += 1.0
                continue

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
        """Auto-accept a permission prompt by pressing Enter."""
        logger.info(
            "Permission prompt detected, auto-accepting (thread=%d)",
            self._thread_id,
        )
        from ..tmux import SESSION_NAME, _run

        window = self._tmux._find_window_for_thread(self._thread_id)
        if window:
            _run(["tmux", "send-keys", "-t", f"{SESSION_NAME}:{window}", "Enter"])

    @staticmethod
    def _extract_response(pane_text: str) -> str:
        """Extract Claude's latest response from the TUI pane text.

        Parses the Claude TUI structure to find the response content
        after the last user prompt, stripping TUI chrome (banner,
        shell noise, separators, status bar, prompt markers).

        The TUI layout (bottom to top):
        - Status bar: ``-- INSERT -- ⏵⏵ bypass permissions on ...``
        - Separator: ``────────────...``
        - Input prompt: ``❯`` (bare, waiting for input)
        - Separator: ``────────────...``
        - Response content (``●`` markers, ``⎿`` tool results)
        - User prompt: ``❯ <user message>``
        - (previous exchanges, banner, shell noise above)
        """
        lines = pane_text.splitlines()

        # Step 1: Strip bottom TUI chrome (status bar, separators, input
        # area, and generation status indicators).
        #
        # The bottom of the Claude TUI has this structure:
        #   ──────────── (separator 1)
        #   ❯ <input or hint>  (or "· Thinking…", "Tip: ...")
        #   ──────────── (separator 2)
        #   -- INSERT -- (status bar)
        #
        # We track separator_count so that ❯ lines are only stripped while
        # inside the input area (between or below the two separators).
        # Once we've passed both separators, a ❯ line is a user prompt
        # and should NOT be stripped.
        end = len(lines)
        separator_count = 0
        while end > 0:
            stripped = lines[end - 1].strip()
            if not stripped or any(stripped.startswith(m) for m in _STATUS_BAR_MARKERS):
                end -= 1
            elif _SEPARATOR_RE.match(stripped):
                separator_count += 1
                end -= 1
            elif stripped.startswith("❯") or stripped.startswith(">"):
                if separator_count < 2:
                    # Still in the input area (between/below separators).
                    end -= 1
                else:
                    # Above the input area — this is a user prompt; stop.
                    break
            elif any(
                stripped.startswith(m) for m in _GENERATION_STATUS_MARKERS
            ) or _GENERATION_STATUS_RE.match(stripped):
                end -= 1
            else:
                break

        if end == 0:
            return ""

        # Step 2: Find the last user prompt (❯ followed by actual text).
        # Now that bottom chrome is stripped, search backwards from end.
        # The user prompt uses a regular space after ❯ (not \xa0).
        prompt_idx = -1
        for i in range(end - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped.startswith("❯ ") and len(stripped) > 2:
                prompt_idx = i
                break

        if prompt_idx == -1:
            return ""

        # Step 3: Extract response lines between prompt and end.
        response_lines = lines[prompt_idx + 1 : end]

        # Step 4: Clean up the response.
        return _clean_tui_lines(response_lines)

    @staticmethod
    def _has_input_prompt(text: str) -> bool:
        """Check if the pane text contains a Claude input prompt near the bottom.

        The TUI shows a status bar (``-- INSERT --``, separator lines) below
        the ``❯`` prompt, so we cannot simply check ``endswith``.  Instead,
        look at the last few lines for a line that is *only* the prompt
        character (with optional whitespace).
        """
        lines = text.rstrip().splitlines()
        for line in lines[-6:]:
            stripped_line = line.strip()
            if stripped_line in ("❯", ">"):
                return True
        return False

    # Keep for backward compatibility / testing.
    @staticmethod
    def _compute_delta(old: str, new: str) -> str:
        """Compute the text that was added between two pane captures."""
        old_stripped = old.rstrip()
        new_stripped = new.rstrip()

        if new_stripped.startswith(old_stripped):
            return new_stripped[len(old_stripped) :]

        old_lines = old_stripped.splitlines()
        new_lines = new_stripped.splitlines()

        overlap = 0
        for i in range(min(len(old_lines), len(new_lines)), 0, -1):
            if old_lines[-i:] == new_lines[:i]:
                overlap = i
                break

        if overlap > 0:
            added_lines = new_lines[overlap:]
            return "\n".join(added_lines)

        return new_stripped


def _clean_tui_lines(lines: list[str]) -> str:
    """Clean Claude TUI response lines for Discord display.

    - Strips leading/trailing empty lines
    - Removes ``●`` response markers
    - Cleans ``⎿`` tool result markers
    - Removes TUI hints (``ctrl+o to expand``, ``Recalled N memory``)
    """
    # Skip leading empty lines.
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1

    cleaned: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()

        # Determine the content text after removing TUI markers (● / ⎿).
        if stripped.startswith("● "):
            content = stripped[2:]
        elif stripped == "●":
            content = ""
        elif stripped.startswith("⎿"):
            content = stripped[1:].lstrip()
        else:
            content = stripped

        # Skip lines matching strip patterns — check both the raw line
        # and the marker-stripped content so patterns like "Hi! How can I
        # help you.*" match even when the line has a "● " prefix.
        skip = False
        for pat in _STRIP_PATTERNS:
            if pat.fullmatch(stripped) or (content != stripped and pat.fullmatch(content)):
                skip = True
                break
        if skip:
            continue

        # Remove (ctrl+o to expand) hints inline.
        line = re.sub(r"\s*\(ctrl\+o to expand\)", "", line)
        stripped = line.strip()

        # Strip ● marker from response/tool lines.
        if stripped.startswith("● "):
            cleaned.append(stripped[2:])
        elif stripped == "●":
            cleaned.append("")
        # Clean ⎿ tool result marker (keep content, indent slightly).
        elif stripped.startswith("⎿"):
            content = stripped[1:].lstrip()
            if content:
                cleaned.append(f"  {content}")
            else:
                cleaned.append("")
        else:
            # Keep other lines (indented continuation, etc.) with original indent.
            cleaned.append(line.rstrip())

    # Remove leading empty lines (may appear after filtered lines are removed).
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)

    # Remove trailing empty lines.
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    return "\n".join(cleaned)
