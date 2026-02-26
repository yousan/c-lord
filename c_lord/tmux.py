"""Tmux session management for Claude Code sessions.

Uses a single tmux session ``clord`` with per-thread windows (``work1``,
``work2``, ...).  The mapping between thread IDs and window names is
stored in tmux window options (``@thread_id``) so it survives bot restarts.

All operations use ``asyncio.to_thread`` for non-blocking execution.
When tmux is not installed, operations degrade gracefully (log warning, skip).
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

SESSION_NAME = "clord"
WINDOW_PREFIX = "work"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return the result (never raises on non-zero exit)."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
    )


def _tmux_available() -> bool:
    """Return True if tmux is installed and accessible."""
    result = _run(["tmux", "-V"])
    return result.returncode == 0


class TmuxSessionManager:
    """Manages tmux windows for Claude Code Discord threads.

    One global tmux session ``clord`` holds all threads.  Each thread gets
    a window named ``work{N}`` with the thread ID stored in the ``@thread_id``
    window option.
    """

    def __init__(self, session_name: str | None = None) -> None:
        self.session_name: str = session_name or SESSION_NAME
        self._available: bool | None = None
        self._next_work_id: int = 1
        self._thread_to_window: dict[int, str] = {}

    def _check_available(self) -> bool:
        """Check and cache tmux availability."""
        if self._available is None:
            self._available = _tmux_available()
            if not self._available:
                logger.warning("tmux is not installed — tmux features disabled")
        return self._available

    # ── Helpers ────────────────────────────────────────────────────────

    def _ensure_session(self) -> bool:
        """Ensure the global ``clord`` tmux session exists.

        Returns True if the session is available (already existed or was
        created), False on failure.
        """
        result = _run(["tmux", "has-session", "-t", self.session_name])
        if result.returncode == 0:
            return True

        # Create with a temporary first window that will be replaced
        result = _run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.session_name,
            ]
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to create tmux session %s: %s",
                self.session_name,
                result.stderr.strip(),
            )
            return False

        logger.info("Created global tmux session: %s", self.session_name)
        return True

    def _find_window_for_thread(self, thread_id: int) -> str | None:
        """Find the window name for a thread by checking ``@thread_id`` options.

        Returns the window name or None if not found.
        """
        # Check in-memory cache first
        cached = self._thread_to_window.get(thread_id)
        if cached is not None:
            # Verify the window still exists
            result = _run(
                [
                    "tmux",
                    "show-option",
                    "-w",
                    "-v",
                    "-t",
                    f"{self.session_name}:{cached}",
                    "@thread_id",
                ]
            )
            if result.returncode == 0 and result.stdout.strip() == str(thread_id):
                return cached
            # Stale cache entry
            del self._thread_to_window[thread_id]

        # Fallback: scan all windows
        self._rebuild_mapping()
        return self._thread_to_window.get(thread_id)

    def _next_window_name(self) -> str:
        """Generate the next ``work{N}`` name and increment the counter."""
        name = f"{WINDOW_PREFIX}{self._next_work_id}"
        self._next_work_id += 1
        return name

    def _rebuild_mapping(self) -> None:
        """Rebuild ``_thread_to_window`` from live tmux state.

        Also updates ``_next_work_id`` to be one past the highest existing
        work window number.
        """
        self._thread_to_window.clear()

        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_name}",
            ]
        )
        if result.returncode != 0:
            return

        max_id = 0
        for window_name in result.stdout.strip().splitlines():
            if not window_name:
                continue

            # Read the @thread_id option for this window
            opt_result = _run(
                [
                    "tmux",
                    "show-option",
                    "-w",
                    "-v",
                    "-t",
                    f"{self.session_name}:{window_name}",
                    "@thread_id",
                ]
            )
            if opt_result.returncode == 0:
                tid_str = opt_result.stdout.strip()
                if tid_str.isdigit():
                    self._thread_to_window[int(tid_str)] = window_name

            # Track highest work ID
            if window_name.startswith(WINDOW_PREFIX):
                suffix = window_name[len(WINDOW_PREFIX) :]
                if suffix.isdigit():
                    max_id = max(max_id, int(suffix))

        self._next_work_id = max_id + 1

    # ── Public API ────────────────────────────────────────────────────

    def create_session(self, thread_id: int, working_dir: str) -> str:
        """Create a tmux window for a thread inside the global session.

        Returns the window name.  Re-uses an existing window if one is
        already mapped to this thread.
        """
        if not self._check_available():
            return f"{WINDOW_PREFIX}0"

        # Check for existing window
        existing = self._find_window_for_thread(thread_id)
        if existing is not None:
            logger.debug("tmux window already exists for thread %d: %s", thread_id, existing)
            return existing

        if not self._ensure_session():
            return f"{WINDOW_PREFIX}0"

        window_name = self._next_window_name()

        result = _run(
            [
                "tmux",
                "new-window",
                "-t",
                self.session_name,
                "-n",
                window_name,
                "-c",
                working_dir,
            ]
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to create tmux window %s: %s",
                window_name,
                result.stderr.strip(),
            )
            return window_name

        # Store thread_id as a window option
        _run(
            [
                "tmux",
                "set-option",
                "-w",
                "-t",
                f"{self.session_name}:{window_name}",
                "@thread_id",
                str(thread_id),
            ]
        )

        self._thread_to_window[thread_id] = window_name
        logger.info(
            "Created tmux window: %s (thread=%d, dir=%s)",
            window_name,
            thread_id,
            working_dir,
        )
        return window_name

    def remap_window(self, thread_id: int, window_name: str) -> bool:
        """Remap an existing tmux window to a new thread ID.

        Updates the ``@thread_id`` window option and the in-memory cache.
        Any previous thread mapping to this window is removed.

        Returns True on success, False if the window does not exist or
        tmux is unavailable.
        """
        if not self._check_available():
            return False

        # Check the window exists by reading its @thread_id option.
        # (tmux has no ``has-window`` command; ``show-option -w`` fails
        # with "no such window" when the target does not exist.)
        result = _run(
            [
                "tmux",
                "show-option",
                "-w",
                "-t",
                f"{self.session_name}:{window_name}",
                "@thread_id",
            ]
        )
        if result.returncode != 0:
            logger.debug("remap_window: window %s not found", window_name)
            return False

        # Update the @thread_id option
        _run(
            [
                "tmux",
                "set-option",
                "-w",
                "-t",
                f"{self.session_name}:{window_name}",
                "@thread_id",
                str(thread_id),
            ]
        )

        # Update cache: remove any old thread→window mapping for this window
        old_threads = [tid for tid, wname in self._thread_to_window.items() if wname == window_name]
        for tid in old_threads:
            del self._thread_to_window[tid]
        self._thread_to_window[thread_id] = window_name

        logger.info("Remapped window %s → thread %d", window_name, thread_id)
        return True

    def session_exists(self, thread_id: int) -> bool:
        """Return True if a tmux window exists for the given thread."""
        if not self._check_available():
            return False

        return self._find_window_for_thread(thread_id) is not None

    def kill_session(self, thread_id: int) -> bool:
        """Kill the tmux window for a thread. Returns True if killed."""
        if not self._check_available():
            return False

        window_name = self._find_window_for_thread(thread_id)
        if window_name is None:
            logger.debug("No tmux window found for thread %d", thread_id)
            return False

        result = _run(
            [
                "tmux",
                "kill-window",
                "-t",
                f"{self.session_name}:{window_name}",
            ]
        )
        if result.returncode == 0:
            self._thread_to_window.pop(thread_id, None)
            logger.info("Killed tmux window: %s (thread=%d)", window_name, thread_id)
            return True
        else:
            logger.debug("tmux window %s not found or already dead", window_name)
            return False

    def list_sessions(self) -> list[dict[str, str]]:
        """List all windows in the ``clord`` tmux session.

        Returns a list of dicts with ``window_name``, ``working_dir``, and
        ``thread_id`` keys.
        """
        if not self._check_available():
            return []

        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_name}:#{pane_current_path}",
            ]
        )
        if result.returncode != 0:
            return []

        windows: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split(":", 1)
            window_name = parts[0]
            working_dir = parts[1] if len(parts) > 1 else ""

            # Read the @thread_id option
            opt_result = _run(
                [
                    "tmux",
                    "show-option",
                    "-w",
                    "-v",
                    "-t",
                    f"{self.session_name}:{window_name}",
                    "@thread_id",
                ]
            )
            tid = opt_result.stdout.strip() if opt_result.returncode == 0 else ""

            windows.append(
                {
                    "window_name": window_name,
                    "working_dir": working_dir,
                    "thread_id": tid,
                }
            )

        return windows

    # ── Claude execution API ────────────────────────────────────────

    def start_claude(
        self,
        thread_id: int,
        prompt: str,
        model: str = "sonnet",
        *,
        permission_mode: str = "acceptEdits",
        dangerously_skip_permissions: bool = False,
    ) -> bool:
        """Start Claude Code inside the tmux window for *thread_id*.

        Sends ``claude --model {model} 'prompt'`` via ``send-keys``.
        The window must already exist (call ``create_session`` first).

        The prompt is passed as a CLI argument so Claude starts a new
        conversation immediately (without a prompt, ``claude`` tries to
        resume and exits with "No conversation found to continue").

        The command is prefixed with ``unalias claude`` and
        ``env -u CLAUDECODE`` to bypass any shell aliases (e.g.
        ``--continue``) and to prevent the nested-session guard from
        blocking startup.

        Returns True if the command was sent successfully.
        """
        if not self._check_available():
            return False

        window = self._find_window_for_thread(thread_id)
        if window is None:
            logger.warning("start_claude: no window for thread %d", thread_id)
            return False

        target = f"{self.session_name}:{window}"
        cmd_parts = ["env", "-u", "CLAUDECODE", "claude", "--model", model]
        if dangerously_skip_permissions:
            cmd_parts.append("--dangerously-skip-permissions")
        else:
            cmd_parts.extend(["--permission-mode", permission_mode])

        # Escape single quotes in the prompt for shell safety.
        safe_prompt = prompt.replace("'", "'\\''")
        cmd_parts.append(f"'{safe_prompt}'")
        # Prefix with unalias to bypass any shell alias (e.g. --continue).
        cmd = f"unalias claude 2>/dev/null; {' '.join(cmd_parts)}"

        result = _run(["tmux", "send-keys", "-t", target, cmd, "Enter"])
        if result.returncode != 0:
            logger.warning("start_claude: send-keys failed: %s", result.stderr.strip())
            return False

        logger.info("start_claude: sent command to %s", target)
        return True

    def send_input(self, thread_id: int, text: str) -> bool:
        """Send text to the Claude process in the tmux window via ``send-keys -l``.

        Uses ``-l`` (literal) to prevent tmux from interpreting special
        characters in the text.  Sends Enter afterwards to submit.

        Returns True on success.
        """
        if not self._check_available():
            return False

        window = self._find_window_for_thread(thread_id)
        if window is None:
            logger.warning("send_input: no window for thread %d", thread_id)
            return False

        target = f"{self.session_name}:{window}"

        # Send the text literally (no tmux key interpretation)
        result = _run(["tmux", "send-keys", "-l", "-t", target, text])
        if result.returncode != 0:
            logger.warning("send_input: send-keys -l failed: %s", result.stderr.strip())
            return False

        # Press Enter to submit
        result = _run(["tmux", "send-keys", "-t", target, "Enter"])
        return result.returncode == 0

    def capture_pane(self, thread_id: int, history_lines: int = 500) -> str:
        """Capture the current pane text from the tmux window.

        Uses ``tmux capture-pane -p -J`` to retrieve the visible and scrollback
        text with wrapped lines joined.

        Args:
            thread_id: The Discord thread ID.
            history_lines: Number of scrollback lines to capture (default 100).

        Returns:
            The pane text, or empty string on failure.
        """
        if not self._check_available():
            return ""

        window = self._find_window_for_thread(thread_id)
        if window is None:
            return ""

        target = f"{self.session_name}:{window}"
        result = _run(
            [
                "tmux",
                "capture-pane",
                "-p",
                "-J",
                "-t",
                target,
                "-S",
                f"-{history_lines}",
            ]
        )
        if result.returncode != 0:
            return ""

        return result.stdout

    def send_interrupt(self, thread_id: int) -> bool:
        """Send C-c (SIGINT) to the tmux window.

        Returns True on success.
        """
        if not self._check_available():
            return False

        window = self._find_window_for_thread(thread_id)
        if window is None:
            logger.warning("send_interrupt: no window for thread %d", thread_id)
            return False

        target = f"{self.session_name}:{window}"
        result = _run(["tmux", "send-keys", "-t", target, "C-c"])
        return result.returncode == 0

    def is_claude_running(self, thread_id: int) -> bool:
        """Check if the pane's current process is ``claude``.

        Uses ``tmux list-panes -F '#{pane_current_command}'`` to inspect the
        foreground command.

        Returns True if claude is the active foreground process.
        """
        if not self._check_available():
            return False

        window = self._find_window_for_thread(thread_id)
        if window is None:
            return False

        target = f"{self.session_name}:{window}"
        result = _run(
            [
                "tmux",
                "list-panes",
                "-t",
                target,
                "-F",
                "#{pane_current_command}",
            ]
        )
        if result.returncode != 0:
            return False

        command = result.stdout.strip()
        return "claude" in command.lower()

    # ── Cleanup ──────────────────────────────────────────────────────

    def cleanup_orphaned(self, active_thread_ids: set[int]) -> int:
        """Kill tmux windows whose threads are no longer active.

        Returns the number of windows killed.
        """
        if not self._check_available():
            return 0

        killed = 0
        for window in self.list_sessions():
            tid_str = window.get("thread_id", "")
            if not tid_str.isdigit():
                continue
            thread_id = int(tid_str)
            if thread_id not in active_thread_ids and self.kill_session(thread_id):
                killed += 1

        return killed
