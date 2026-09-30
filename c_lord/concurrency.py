"""In-memory registry of the Claude Code sessions that are running a turn.

``/workspace-cleanup`` reads it to skip workspaces that are in use.

History: this module also built a "concurrency notice" meant to be injected into
every session (#52). Since the tmux TUI (#53) there is no per-turn channel for it,
so it was built and dropped (#758); #766 removed the builder.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Active Session Registry
# ---------------------------------------------------------------------------


@dataclass
class ActiveSession:
    """Tracks a single active Claude Code session."""

    thread_id: int
    description: str
    working_dir: str | None = None


class SessionRegistry:
    """Thread-safe registry of active Claude Code sessions.

    Designed to be shared across all Cogs in a single bot instance.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, ActiveSession] = {}
        self._lock = threading.Lock()

    def register(
        self,
        thread_id: int,
        description: str,
        working_dir: str | None = None,
    ) -> None:
        """Register or replace an active session."""
        with self._lock:
            self._sessions[thread_id] = ActiveSession(
                thread_id=thread_id,
                description=description,
                working_dir=working_dir,
            )

    def unregister(self, thread_id: int) -> None:
        """Remove a session from the registry."""
        with self._lock:
            self._sessions.pop(thread_id, None)

    def update(
        self,
        thread_id: int,
        *,
        description: str | None = None,
        working_dir: str | None = None,
    ) -> None:
        """Update fields of an existing session. No-op if not registered."""
        with self._lock:
            session = self._sessions.get(thread_id)
            if session is None:
                return
            if description is not None:
                session.description = description
            if working_dir is not None:
                session.working_dir = working_dir

    def list_active(self) -> list[ActiveSession]:
        """Return all active sessions."""
        with self._lock:
            return list(self._sessions.values())

    def list_others(self, thread_id: int) -> list[ActiveSession]:
        """Return all active sessions except the given thread."""
        with self._lock:
            return [s for s in self._sessions.values() if s.thread_id != thread_id]
