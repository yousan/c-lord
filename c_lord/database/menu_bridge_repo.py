"""Repository for menus the #359 watchdog has already bridged to Discord (#633).

The watchdog sweeps every 60 s, so a menu that stays open is seen again on every
tick.  #600 capped the repeats with an in-memory counter — but the counter died
with the process, and production restarts the bot several times a day: each
fresh process started the stuck menu's budget over at ``attempt=1/3``.  One
thread logged 188 re-bridges and the same ``❓ 切り口`` embed reached Discord six
times over three days, every copy seconds after a ``Started menu watchdog loop``
line.

Persisting the ledger is therefore the fix, not a nicety: "already bridged" has
to be a fact about the *menu*, not about the current process's memory.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)


class MenuBridgeRepository:
    """Async SQLite repository for ``menu_bridges`` rows.

    A row means "this bot has posted this menu to this thread". It is deleted
    only when a sweep sees the thread's pane with no menu on it at all — the
    episode is over, so the next menu (even a textually identical re-ask) is a
    new decision the user has not seen yet.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def posts(self, thread_id: int, fingerprint: str) -> int:
        """How many times this menu has been bridged to this thread."""
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT posts FROM menu_bridges WHERE thread_id = ? AND fingerprint = ?",
                (thread_id, fingerprint),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def record(self, thread_id: int, fingerprint: str) -> int:
        """Count one bridge of this menu and return the new total."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO menu_bridges (thread_id, fingerprint, posts)
                VALUES (?, ?, 1)
                ON CONFLICT(thread_id, fingerprint) DO UPDATE SET
                    posts = posts + 1,
                    last_bridged_at = datetime('now', 'localtime')
                """,
                (thread_id, fingerprint),
            )
            await db.commit()
        return await self.posts(thread_id, fingerprint)

    async def forget(self, thread_id: int, fingerprint: str) -> None:
        """Undo one :meth:`record` — the bridge raised, so nothing was posted.

        #579 retries a menu whose post *failed* (an unreadable option label
        makes Discord reject the whole message), and that retry must survive the
        #633 one-post rule: the budget is spent by a menu the user can see, not
        by an attempt that never reached the thread.
        """
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "DELETE FROM menu_bridges WHERE thread_id = ? AND fingerprint = ?",
                (thread_id, fingerprint),
            )
            await db.commit()

    async def clear(self, thread_id: int) -> None:
        """Forget every menu bridged to *thread_id* — its pane shows none now."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM menu_bridges WHERE thread_id = ?", (thread_id,))
            await db.commit()
