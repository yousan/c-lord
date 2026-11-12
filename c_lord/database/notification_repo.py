"""Notifications to be sent later (the ``scheduled_notifications`` table).

Registered through the REST API and delivered when their time comes. A row moves
from ``pending`` to exactly one of ``sent`` / ``failed`` / ``cancelled``.
``scheduled_at`` is stored as the caller gave it and compared as text. The table
is created by :meth:`NotificationRepository.init_db`, not by ``models.init_db``.
"""

from __future__ import annotations

import logging

from . import _sqlite

logger = logging.getLogger(__name__)

NOTIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message       TEXT    NOT NULL,
    title         TEXT,
    color         INTEGER DEFAULT 49151,
    scheduled_at  TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'api',
    channel_id    INTEGER,
    status        TEXT    NOT NULL DEFAULT 'pending',
    sent_at       TEXT,
    error_message TEXT,
    created_at    TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_notif_status_scheduled
    ON scheduled_notifications(status, scheduled_at);
"""

_TABLE = "scheduled_notifications"


class NotificationRepository:
    """Async CRUD for scheduled notifications."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init_db(self) -> None:
        """Create the table and its index if they are not there yet."""
        await _sqlite.run_script(self.db_path, NOTIFICATION_SCHEMA)
        logger.info("Notification DB initialized at %s", self.db_path)

    async def create(
        self,
        message: str,
        scheduled_at: str,
        *,
        title: str | None = None,
        color: int = 0x00BFFF,
        source: str = "api",
        channel_id: int | None = None,
    ) -> int:
        """Queue a notification for *scheduled_at* and return its id."""
        notification_id = await _sqlite.insert(
            self.db_path,
            _TABLE,
            {
                "message": message,
                "title": title,
                "color": color,
                "scheduled_at": scheduled_at,
                "source": source,
                "channel_id": channel_id,
            },
        )
        logger.info("Notification scheduled: id=%s, at=%s", notification_id, scheduled_at)
        return notification_id

    async def get_pending(self, before: str | None = None) -> list[dict]:
        """Pending notifications, earliest first — only those due at or before
        *before* when one is given."""
        conditions, bound = ["status = 'pending'"], []
        if before:
            conditions.append("scheduled_at <= ?")
            bound.append(before)
        return await _sqlite.read_dicts(
            self.db_path,
            f"SELECT * FROM {_TABLE} WHERE {' AND '.join(conditions)} ORDER BY scheduled_at",
            bound,
        )

    async def _move(self, notification_id: int, assignments: str, *bound: str) -> None:
        """Take a notification out of the queue, whatever state it is in."""
        await _sqlite.write(
            self.db_path,
            f"UPDATE {_TABLE} SET {assignments} WHERE id = ?",
            (*bound, notification_id),
        )

    async def mark_sent(self, notification_id: int) -> None:
        """Record that the notification went out, and when."""
        await self._move(notification_id, "status = 'sent', sent_at = datetime('now', 'localtime')")

    async def mark_failed(self, notification_id: int, error: str) -> None:
        """Record that sending failed, and with what."""
        await self._move(notification_id, "status = 'failed', error_message = ?", error)

    async def cancel(self, notification_id: int) -> bool:
        """Cancel a notification that is still pending. ``False`` when there is
        no such notification or it is no longer pending."""
        done = await _sqlite.write(
            self.db_path,
            f"UPDATE {_TABLE} SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (notification_id,),
        )
        return done.changed
