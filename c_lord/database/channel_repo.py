"""ChannelRepository — CRUD for channel_repo_bindings table.

Maps Discord channel IDs to source repositories, enabling per-channel
session directory management via /clord-init.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)

CHANNEL_REPO_SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_repo_bindings (
    channel_id         INTEGER PRIMARY KEY,
    source_repo        TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def derive_session_name(source_repo: str) -> str:
    """Derive a tmux session name from a repository URL or path.

    Examples::

        'https://github.com/org/my-project.git' → 'my-project'
        'git@github.com:org/my-project.git'     → 'my-project'
        '/home/user/repos/my-project'           → 'my-project'
    """
    name = source_repo.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "clord"


class ChannelRepository:
    """Async CRUD for channel_repo_bindings table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init_db(self) -> None:
        """Initialize the channel_repo_bindings schema."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(CHANNEL_REPO_SCHEMA)
            await db.commit()
        logger.info("Channel repo DB initialized at %s", self.db_path)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get(self, channel_id: int) -> dict | None:
        """Return a binding for the given channel, or None."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM channel_repo_bindings WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def list_all(self) -> list[dict]:
        """Return all channel-repo bindings."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM channel_repo_bindings ORDER BY created_at")
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def save(
        self,
        channel_id: int,
        source_repo: str,
    ) -> None:
        """Insert or replace a channel-repo binding."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO channel_repo_bindings
                   (channel_id, source_repo,
                    created_at, updated_at)
                   VALUES (?, ?, datetime('now', 'localtime'),
                           datetime('now', 'localtime'))""",
                (channel_id, source_repo),
            )
            await db.commit()
        logger.info("Saved channel binding: channel=%d repo=%s", channel_id, source_repo)

    async def delete(self, channel_id: int) -> bool:
        """Delete a binding. Returns True if a row was deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM channel_repo_bindings WHERE channel_id = ?",
                (channel_id,),
            )
            await db.commit()
            return cursor.rowcount > 0
