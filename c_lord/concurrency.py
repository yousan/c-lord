"""Concurrency awareness for multiple simultaneous Claude Code sessions.

Layer 1: Every session receives a generic concurrency warning in its prompt.
Layer 2: An in-memory registry tracks active sessions so each one knows
         what others are doing and can avoid conflicts.

See: https://github.com/yousan/c-lord/issues/52
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Layer 2: Active Session Registry
# ---------------------------------------------------------------------------


@dataclass
class ActiveSession:
    """Tracks a single active Claude Code session."""

    thread_id: int
    description: str
    working_dir: str | None = None


_BASE_CONCURRENCY_NOTICE = """\
[CONCURRENCY NOTICE — MANDATORY] You are one of MULTIPLE Claude Code sessions \
running simultaneously via Discord. Other sessions ARE active right now. \
You MUST follow these rules to avoid destroying each other's work:

1. **Git — INDEPENDENT CLONE**: You are working in an independent clone \
directory, isolated from other sessions. Always commit and push before \
finishing — uncommitted changes WILL be lost when the session ends.
2. **Files**: Another session may be working on overlapping files in a \
separate clone. Coordinate via git (commit, push, pull) to avoid conflicts.
3. **Ports & processes**: Shared network ports or lock files may already be in use.
4. **Resources**: Shared databases, APIs with rate limits, or singleton processes \
may be accessed concurrently.

CRITICAL: If your work targets the same repository as another active session, \
coordinate via git branches to avoid conflicts.\
"""

_OTHER_SESSIONS_HEADER = """
⚠️ ACTIVE SESSIONS RIGHT NOW (you MUST avoid conflicts with these):
"""


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

    def build_concurrency_notice(self, thread_id: int) -> str:
        """Build the full concurrency notice for a session.

        Combines the base Layer 1 warning with Layer 2 context about
        other active sessions.
        """
        notice = _BASE_CONCURRENCY_NOTICE
        others = self.list_others(thread_id)
        if others:
            notice += _OTHER_SESSIONS_HEADER
            for s in others:
                line = f"- {s.description}"
                if s.working_dir:
                    line += f" (working in {s.working_dir})"
                notice += line + "\n"
            notice += (
                "\nIf your work targets the same repository as any session above, "
                "coordinate via git branches to avoid conflicts.\n"
            )
        return notice
