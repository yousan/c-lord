"""ThreadRepository — CRUD for thread_repo_bindings table.

Maps Discord thread IDs to source repositories, enabling per-thread
session directory override via /clord-thread-init.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)

THREAD_REPO_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_repo_bindings (
    thread_id   INTEGER PRIMARY KEY,
    source_repo TEXT    NOT NULL,
    channel_id  INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


class ThreadRepository:
    """Async CRUD for thread_repo_bindings table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init_db(self) -> None:
        """Initialize the thread_repo_bindings schema."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(THREAD_REPO_SCHEMA)
            await db.commit()
        logger.info("Thread repo DB initialized at %s", self.db_path)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get(self, thread_id: int) -> dict | None:
        """Return a binding for the given thread, or None."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM thread_repo_bindings WHERE thread_id = ?",
                (thread_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def list_by_channel(self, channel_id: int) -> list[dict]:
        """Return all thread bindings for a given channel."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM thread_repo_bindings WHERE channel_id = ? ORDER BY created_at",
                (channel_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_all(self) -> list[dict]:
        """Return all thread-repo bindings."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM thread_repo_bindings ORDER BY created_at")
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def save(
        self,
        thread_id: int,
        source_repo: str,
        channel_id: int | None = None,
    ) -> None:
        """Insert or replace a thread-repo binding."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO thread_repo_bindings
                   (thread_id, source_repo, channel_id,
                    created_at, updated_at)
                   VALUES (?, ?, ?, datetime('now', 'localtime'),
                           datetime('now', 'localtime'))""",
                (thread_id, source_repo, channel_id),
            )
            await db.commit()
        logger.info("Saved thread binding: thread=%d repo=%s", thread_id, source_repo)

    async def delete(self, thread_id: int) -> bool:
        """Delete a binding. Returns True if a row was deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM thread_repo_bindings WHERE thread_id = ?",
                (thread_id,),
            )
            await db.commit()
            return cursor.rowcount > 0
