"""Threads to pick up again after a restart (the ``pending_resumes`` table).

Before c-lord restarts itself it leaves a mark for each thread whose work should
continue; the next process reads the marks, resumes those threads and removes
them. A mark is only good for a few minutes — one that is older belongs to a
restart that never came back, and resuming it later would start work nobody is
waiting for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields

from . import _sqlite

logger = logging.getLogger(__name__)

#: How long a mark stays valid, in minutes.
DEFAULT_TTL_MINUTES = 5

_TABLE = "pending_resumes"


@dataclass
class PendingResume:
    """One mark: resume *thread_id* (as *session_id*, saying *resume_prompt*).

    The fields are the table's columns, in the table's order.
    """

    id: int
    thread_id: int
    session_id: str | None
    reason: str
    resume_prompt: str | None
    created_at: str


_SELECT = f"SELECT {', '.join(f.name for f in fields(PendingResume))} FROM {_TABLE}"

# Cut-off for a mark's ``created_at``: the TTL is taken off the clock first and
# the result is then put into local time, the form ``created_at`` is stored in.
_EXPIRY = "datetime('now', ?, 'localtime')"


class PendingResumeRepository:
    """Async access to ``pending_resumes``. At most one mark per thread."""

    def __init__(self, db_path: str, ttl_minutes: int = DEFAULT_TTL_MINUTES) -> None:
        self._db_path = db_path
        self._ttl_minutes = ttl_minutes

    async def mark(
        self,
        thread_id: int,
        *,
        session_id: str | None = None,
        reason: str = "self_restart",
        resume_prompt: str | None = None,
    ) -> int:
        """Mark *thread_id* for resume and return the mark's id.

        A mark the thread already had is replaced: the new one has a new id and
        a fresh ``created_at`` (both are SQLite's to give).
        """
        row_id = await _sqlite.insert(
            self._db_path,
            _TABLE,
            {
                "thread_id": thread_id,
                "session_id": session_id,
                "reason": reason,
                "resume_prompt": resume_prompt,
            },
            replace=True,
        )
        logger.info("Marked thread %s for resume (reason=%s, row_id=%s)", thread_id, reason, row_id)
        return row_id

    async def get_pending(self) -> list[PendingResume]:
        """The marks still valid, oldest first.

        Marks at or past the TTL are deleted first (SQLite does the clock
        arithmetic). The ones returned stay in the table — the caller deletes
        each as it handles it.
        """
        async with _sqlite.connection(self._db_path) as db:
            await db.execute(
                f"DELETE FROM {_TABLE} WHERE created_at <= {_EXPIRY}",
                (f"-{self._ttl_minutes} minutes",),
            )
            await db.commit()
            valid = await db.execute(f"{_SELECT} ORDER BY created_at")
            return [PendingResume(*row) for row in await valid.fetchall()]

    async def _forget(self, column: str, value: int) -> None:
        await _sqlite.write(self._db_path, f"DELETE FROM {_TABLE} WHERE {column} = ?", (value,))

    async def delete(self, row_id: int) -> None:
        """Remove one mark by its id. A missing one is not an error."""
        await self._forget("id", row_id)

    async def delete_by_thread(self, thread_id: int) -> None:
        """Remove the mark of *thread_id*, if it has one."""
        await self._forget("thread_id", thread_id)
