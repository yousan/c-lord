"""Questions waiting for an answer (the ``pending_asks`` table).

When Claude asks something (AskUserQuestion, a menu in the pane) c-lord shows it
in Discord as buttons. What is being asked, and how far the user has got, is
kept here — one row per thread — so that the buttons still mean something after
the bot restarts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields
from typing import Any

from . import _sqlite

logger = logging.getLogger(__name__)

_TABLE = "pending_asks"


@dataclass
class PendingAskRecord:
    """The question a thread is waiting on. One field per column, same names."""

    thread_id: int
    session_id: str
    #: The list of questions, serialised as JSON.
    questions_json: str
    question_idx: int
    created_at: str
    # #671: the Discord message showing this menu, so startup can retire a menu
    # the pane has since closed instead of leaving it looking clickable. None on
    # rows written before #671 (and on any path that posts no message).
    message_id: int | None = None

    def questions(self) -> list[dict[str, Any]]:
        """The questions, parsed afresh on every call."""
        return json.loads(self.questions_json)


# The record is the single list of columns: what is read comes back in field
# order, and what is written is every field except the one SQLite fills in.
_READ = tuple(f.name for f in fields(PendingAskRecord))
_WRITTEN = tuple(name for name in _READ if name != "created_at")

_SELECT = f"SELECT {', '.join(_READ)} FROM {_TABLE}"
_REPLACE = _sqlite.insert_statement(_TABLE, _WRITTEN, replace=True)


def _ask(row: Any) -> PendingAskRecord:
    return PendingAskRecord(**dict(zip(_READ, row, strict=True)))


class PendingAskRepository:
    """Async access to ``pending_asks``."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def save(
        self,
        thread_id: int,
        session_id: str,
        questions: list[dict[str, Any]],
        question_idx: int = 0,
        message_id: int | None = None,
    ) -> None:
        """Write the thread's pending question, replacing the one it had.

        The row is replaced whole, so ``created_at`` is the time of this write.
        """
        async with _sqlite.connection(self._db_path) as db:
            row = {
                "thread_id": thread_id,
                "session_id": session_id,
                "questions_json": json.dumps(questions),
                "question_idx": question_idx,
                "message_id": message_id,
            }
            await db.execute(_REPLACE, tuple(row[column] for column in _WRITTEN))
            await db.commit()
        logger.debug(
            "PendingAskRepository: saved pending ask for thread %d (q_idx=%d)",
            thread_id,
            question_idx,
        )

    async def get(self, thread_id: int) -> PendingAskRecord | None:
        """The thread's pending question, or ``None``."""
        row = await _sqlite.read_one(self._db_path, f"{_SELECT} WHERE thread_id = ?", (thread_id,))
        return None if row is None else _ask(row)

    async def delete(self, thread_id: int) -> None:
        """Forget the thread's pending question (answered, or gone)."""
        await _sqlite.write(
            self._db_path, f"DELETE FROM {_TABLE} WHERE thread_id = ?", (thread_id,)
        )
        logger.debug("PendingAskRepository: deleted pending ask for thread %d", thread_id)

    async def list_all(self) -> list[PendingAskRecord]:
        """Every pending question, oldest first."""
        rows = await _sqlite.read_all(self._db_path, f"{_SELECT} ORDER BY created_at")
        return [_ask(row) for row in rows]

    async def cleanup_old(self, hours: int = 48) -> int:
        """Delete questions written more than *hours* ago; return how many."""
        done = await _sqlite.write(
            self._db_path,
            # localtime first, then the offset — the reverse of resume_repo, and the
            # two differ by an hour right after a DST switch.
            f"DELETE FROM {_TABLE} WHERE created_at < datetime('now', 'localtime', ?)",
            (f"-{hours} hours",),
        )
        return done.rowcount
