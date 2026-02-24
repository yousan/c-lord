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

    def __init__(self) -> None:
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
        result = _run(["tmux", "has-session", "-t", SESSION_NAME])
        if result.returncode == 0:
            return True

        # Create with a temporary first window that will be replaced
        result = _run([
            "tmux", "new-session", "-d", "-s", SESSION_NAME,
        ])
        if result.returncode != 0:
            logger.warning(
                "Failed to create tmux session %s: %s",
                SESSION_NAME, result.stderr.strip(),
            )
            return False

        logger.info("Created global tmux session: %s", SESSION_NAME)
        return True

    def _find_window_for_thread(self, thread_id: int) -> str | None:
        """Find the window name for a thread by checking ``@thread_id`` options.

        Returns the window name or None if not found.
        """
        # Check in-memory cache first
        cached = self._thread_to_window.get(thread_id)
        if cached is not None:
            # Verify the window still exists
            result = _run([
                "tmux", "show-option", "-w", "-v",
                "-t", f"{SESSION_NAME}:{cached}", "@thread_id",
            ])
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

        result = _run([
            "tmux", "list-windows", "-t", SESSION_NAME,
            "-F", "#{window_name}",
        ])
        if result.returncode != 0:
            return

        max_id = 0
        for window_name in result.stdout.strip().splitlines():
            if not window_name:
                continue

            # Read the @thread_id option for this window
            opt_result = _run([
                "tmux", "show-option", "-w", "-v",
                "-t", f"{SESSION_NAME}:{window_name}", "@thread_id",
            ])
            if opt_result.returncode == 0:
                tid_str = opt_result.stdout.strip()
                if tid_str.isdigit():
                    self._thread_to_window[int(tid_str)] = window_name

            # Track highest work ID
            if window_name.startswith(WINDOW_PREFIX):
                suffix = window_name[len(WINDOW_PREFIX):]
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

        result = _run([
            "tmux", "new-window",
            "-t", SESSION_NAME,
            "-n", window_name,
            "-c", working_dir,
        ])
        if result.returncode != 0:
            logger.warning(
                "Failed to create tmux window %s: %s",
                window_name, result.stderr.strip(),
            )
            return window_name

        # Store thread_id as a window option
        _run([
            "tmux", "set-option", "-w",
            "-t", f"{SESSION_NAME}:{window_name}",
            "@thread_id", str(thread_id),
        ])

        self._thread_to_window[thread_id] = window_name
        logger.info(
            "Created tmux window: %s (thread=%d, dir=%s)",
            window_name, thread_id, working_dir,
        )
        return window_name

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

        result = _run([
            "tmux", "kill-window",
            "-t", f"{SESSION_NAME}:{window_name}",
        ])
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

        result = _run([
            "tmux", "list-windows", "-t", SESSION_NAME,
            "-F", "#{window_name}:#{pane_current_path}",
        ])
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
            opt_result = _run([
                "tmux", "show-option", "-w", "-v",
                "-t", f"{SESSION_NAME}:{window_name}", "@thread_id",
            ])
            tid = opt_result.stdout.strip() if opt_result.returncode == 0 else ""

            windows.append({
                "window_name": window_name,
                "working_dir": working_dir,
                "thread_id": tid,
            })

        return windows

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
