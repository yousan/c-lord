"""AI Lounge (the ``lounge_messages`` table).

A short shared notice board: Claude sessions running side by side leave each
other one-line notes. Only the newest :data:`_MAX_STORED_MESSAGES` are kept.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields

import aiosqlite

from . import _sqlite

logger = logging.getLogger(__name__)

#: Notes kept in the table; older ones go with every new post.
_MAX_STORED_MESSAGES = 200

_TABLE = "lounge_messages"


@dataclass
class LoungeMessage:
    """One note, as stored. ``posted_at`` is local time, ``YYYY-MM-DD HH:MM:SS``.

    The fields are the table's columns, in the table's order.
    """

    id: int
    label: str
    message: str
    posted_at: str


_COLUMNS = ", ".join(f.name for f in fields(LoungeMessage))


def _fitted(text: str | None, *, limit: int, otherwise: str = "") -> str:
    """*text* cut to *limit* characters; *otherwise* when there is no text."""
    return (text or otherwise)[:limit]


async def _add(db: aiosqlite.Connection, label: str, message: str) -> int | None:
    """Step 1 — store the note (committed) and say which id it got."""
    added = await db.execute(
        _sqlite.insert_statement(_TABLE, ("label", "message")), (label, message)
    )
    await db.commit()
    return added.lastrowid


async def _stored(db: aiosqlite.Connection, note_id: int | None) -> LoungeMessage | None:
    """Step 2 — the note as the table now has it (SQLite chose ``posted_at``)."""
    found = await db.execute(f"SELECT {_COLUMNS} FROM {_TABLE} WHERE id = ?", (note_id,))
    row = await found.fetchone()
    return None if row is None else LoungeMessage(*row)


async def _drop_oldest(db: aiosqlite.Connection) -> None:
    """Step 3 — keep the newest :data:`_MAX_STORED_MESSAGES` only (committed)."""
    await db.execute(
        f"DELETE FROM {_TABLE} WHERE id NOT IN (SELECT id FROM {_TABLE} ORDER BY id DESC LIMIT ?)",
        (_MAX_STORED_MESSAGES,),
    )
    await db.commit()


class LoungeRepository:
    """Async access to ``lounge_messages``."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def post(self, message: str, label: str = "AI") -> LoungeMessage:
        """Store a note and return it as stored (with the time SQLite gave it).

        Over-long input is cut, not refused; a missing label becomes ``AI``.
        Rejecting an empty message is the API's business, not this method's.
        """
        label = _fitted(label, limit=50, otherwise="AI")
        message = _fitted(message, limit=1000)
        async with _sqlite.connection(self._db_path) as db:
            note_id = await _add(db, label, message)
            note = await _stored(db, note_id)
            await _drop_oldest(db)
        if note is None:
            raise RuntimeError(f"Failed to retrieve lounge message id={note_id}")
        logger.info("Lounge message posted by %r (id=%s)", label, note_id)
        return note

    async def get_recent(self, limit: int = 10) -> list[LoungeMessage]:
        """The newest *limit* notes, oldest of them first."""
        newest = f"SELECT {_COLUMNS} FROM {_TABLE} ORDER BY id DESC LIMIT ?"
        rows = await _sqlite.read_all(
            self._db_path, f"SELECT {_COLUMNS} FROM ({newest}) ORDER BY id", (limit,)
        )
        return [LoungeMessage(*row) for row in rows]

    async def count(self) -> int:
        """How many notes are stored."""
        return int(
            await _sqlite.read_value(self._db_path, f"SELECT COUNT(*) FROM {_TABLE}", default=0)
        )
