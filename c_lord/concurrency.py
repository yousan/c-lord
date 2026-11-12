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
    """One session that is running a turn right now."""

    thread_id: int
    description: str
    working_dir: str | None = None


class SessionRegistry:
    """Which threads have a turn in flight — at most one entry per thread.

    Entries are kept in the order they were registered. Every operation takes
    the same lock, so the registry may be used from more than one OS thread.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._by_thread: dict[int, ActiveSession] = {}

    def register(self, thread_id: int, description: str, working_dir: str | None = None) -> None:
        """Record *thread_id* as running; a second call replaces the first."""
        entry = ActiveSession(thread_id, description, working_dir)
        with self._guard:
            self._by_thread[thread_id] = entry

    def unregister(self, thread_id: int) -> None:
        """Forget *thread_id*. Unknown ids are ignored."""
        with self._guard:
            self._by_thread.pop(thread_id, None)

    def update(
        self,
        thread_id: int,
        *,
        description: str | None = None,
        working_dir: str | None = None,
    ) -> None:
        """Change the given fields of a registered session.

        ``None`` means "leave as is", so a field cannot be reset to ``None``
        this way. Unknown ids are ignored.
        """
        changes = {
            name: value
            for name, value in (("description", description), ("working_dir", working_dir))
            if value is not None
        }
        with self._guard:
            entry = self._by_thread.get(thread_id)
            if entry is not None:
                for name, value in changes.items():
                    setattr(entry, name, value)

    def list_active(self) -> list[ActiveSession]:
        """Every registered session, as a list the caller owns."""
        with self._guard:
            return list(self._by_thread.values())

    def list_others(self, thread_id: int) -> list[ActiveSession]:
        """Every registered session except the one in *thread_id*."""
        with self._guard:
            return [s for tid, s in self._by_thread.items() if tid != thread_id]
