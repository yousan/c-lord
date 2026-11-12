"""Key → value settings (the ``settings`` table).

Both sides are plain strings. :mod:`c_lord.model_scopes` reads and writes the
same table with SQL of its own, so nothing here encodes or wraps a value.
"""

from __future__ import annotations

import logging

from . import _sqlite

logger = logging.getLogger(__name__)


class SettingsRepository:
    """Async access to ``settings``. The table itself comes from ``models.init_db``."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def get(self, key: str, *, default: str | None = None) -> str | None:
        """The value stored under *key*; *default* only when there is no row.

        A stored empty string is a value and is returned as such.
        """
        return await _sqlite.read_value(
            self.db_path, "SELECT value FROM settings WHERE key = ?", (key,), default=default
        )

    async def set(self, key: str, value: str) -> None:
        """Store *value* under *key*, replacing what was there."""
        await _sqlite.write(
            self.db_path,
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def delete(self, key: str) -> bool:
        """Remove *key*. ``True`` when there was something to remove."""
        done = await _sqlite.write(self.db_path, "DELETE FROM settings WHERE key = ?", (key,))
        return done.changed

    async def get_all(self) -> dict[str, str]:
        """Every setting, in key order."""
        pairs = await _sqlite.read_all(self.db_path, "SELECT key, value FROM settings ORDER BY key")
        return dict(map(tuple, pairs))
