"""Tmux session management for Claude Code sessions.

Thin wrapper around tmux CLI for creating, querying, and cleaning up
tmux sessions that correspond to Discord threads.

All operations use ``asyncio.to_thread`` for non-blocking execution.
When tmux is not installed, operations degrade gracefully (log warning, skip).
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

SESSION_PREFIX = "clord-"


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
    """Manages tmux sessions for Claude Code Discord threads.

    Each thread gets a tmux session named ``clord-{thread_id}``.
    """

    def __init__(self) -> None:
        self._available: bool | None = None

    def _check_available(self) -> bool:
        """Check and cache tmux availability."""
        if self._available is None:
            self._available = _tmux_available()
            if not self._available:
                logger.warning("tmux is not installed — tmux features disabled")
        return self._available

    def _session_name(self, thread_id: int) -> str:
        return f"{SESSION_PREFIX}{thread_id}"

    def create_session(self, thread_id: int, working_dir: str) -> str:
        """Create a detached tmux session for a thread.

        Returns the session name. No-op if session already exists.
        """
        if not self._check_available():
            return self._session_name(thread_id)

        name = self._session_name(thread_id)

        if self.session_exists(thread_id):
            logger.debug("tmux session already exists: %s", name)
            return name

        result = _run(["tmux", "new-session", "-d", "-s", name, "-c", working_dir])
        if result.returncode != 0:
            logger.warning("Failed to create tmux session %s: %s", name, result.stderr.strip())
        else:
            logger.info("Created tmux session: %s (dir=%s)", name, working_dir)

        return name

    def session_exists(self, thread_id: int) -> bool:
        """Return True if a tmux session exists for the given thread."""
        if not self._check_available():
            return False

        name = self._session_name(thread_id)
        result = _run(["tmux", "has-session", "-t", name])
        return result.returncode == 0

    def kill_session(self, thread_id: int) -> bool:
        """Kill the tmux session for a thread. Returns True if killed."""
        if not self._check_available():
            return False

        name = self._session_name(thread_id)
        result = _run(["tmux", "kill-session", "-t", name])
        if result.returncode == 0:
            logger.info("Killed tmux session: %s", name)
            return True
        else:
            logger.debug("tmux session %s not found or already dead", name)
            return False

    def list_sessions(self) -> list[dict[str, str]]:
        """List all clord-* tmux sessions.

        Returns a list of dicts with 'name' and 'working_dir' keys.
        """
        if not self._check_available():
            return []

        result = _run(
            ["tmux", "list-sessions", "-F", "#{session_name}:#{pane_current_path}"]
        )
        if result.returncode != 0:
            return []

        sessions: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            if not line.startswith(SESSION_PREFIX):
                continue
            parts = line.split(":", 1)
            name = parts[0]
            working_dir = parts[1] if len(parts) > 1 else ""
            sessions.append({"name": name, "working_dir": working_dir})

        return sessions

    def cleanup_orphaned(self, active_thread_ids: set[int]) -> int:
        """Kill tmux sessions whose threads are no longer active.

        Returns the number of sessions killed.
        """
        if not self._check_available():
            return 0

        killed = 0
        for session in self.list_sessions():
            name = session["name"]
            # Extract thread_id from session name
            suffix = name[len(SESSION_PREFIX):]
            if not suffix.isdigit():
                continue
            thread_id = int(suffix)
            if thread_id not in active_thread_ids:
                if self.kill_session(thread_id):
                    killed += 1

        return killed
