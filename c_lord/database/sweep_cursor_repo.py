"""How far the startup sweep for dead buttons has read each thread (#752).

The sweep used to re-read a fixed window — the newest 100 messages of the 200
most recently used threads — on every start. Residue that sank below that window
under later messages, or sat in the 201st thread, was never looked at again:
production carried a ⏹ Stop with 164 messages on top of it and 139 threads the
sweep had never visited.

A per-thread cursor turns that into "read everything once, then only what is
new": each startup resumes after the last message the previous sweep examined,
so a thread costs one request when nothing happened in it and none of its
history is ever skipped.
"""

from __future__ import annotations

import aiosqlite


class SweepCursorRepository:
    """Async SQLite access to ``ui_sweep_cursors``."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def get_all(self) -> dict[int, int]:
        """``{thread_id: last examined message id}`` for every swept thread."""
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute("SELECT thread_id, last_message_id FROM ui_sweep_cursors") as cursor,
        ):
            rows = await cursor.fetchall()
        return {int(row[0]): int(row[1]) for row in rows}

    async def set(self, thread_id: int, message_id: int) -> None:
        """Record that every message of *thread_id* up to *message_id* was examined."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO ui_sweep_cursors (thread_id, last_message_id) VALUES (?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET "
                "last_message_id = excluded.last_message_id, "
                "swept_at = datetime('now', 'localtime')",
                (thread_id, message_id),
            )
            await db.commit()
