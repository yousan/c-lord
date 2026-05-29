"""Thread auto-archive duration settings.

Discord threads auto-archive after a period of inactivity. c-lord uses a single
global setting (stored via :class:`~c_lord.database.settings_repo.SettingsRepository`)
to control the ``auto_archive_duration`` of every thread it creates, defaulting to
3 days. This mirrors the global-model pattern in ``cogs/session_manage.py``.

This module is intentionally dependency-light (no cog imports) so every thread-
creating cog can call :func:`resolve_auto_archive_duration` without circular imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from .database.settings_repo import SettingsRepository

# Settings key for the global thread auto-archive duration (minutes).
SETTING_THREAD_AUTO_ARCHIVE = "thread_auto_archive_duration"

# Discord only accepts these four values for ``auto_archive_duration`` (minutes):
# 1 hour / 1 day / 3 days / 7 days. Any other value is rejected by the API.
# This matches discord.py's ``ThreadArchiveDuration`` literal type.
ThreadArchiveDuration = Literal[60, 1440, 4320, 10080]
VALID_DURATIONS: tuple[ThreadArchiveDuration, ...] = (60, 1440, 4320, 10080)

# New framework default: 3 days. Applied even when no setting is stored, so
# consumers get the longer window by updating the package alone (zero-config).
DEFAULT_AUTO_ARCHIVE_DURATION: ThreadArchiveDuration = 4320


async def resolve_auto_archive_duration(
    settings_repo: SettingsRepository | None,
) -> ThreadArchiveDuration:
    """Return the configured thread auto-archive duration in minutes.

    Falls back to :data:`DEFAULT_AUTO_ARCHIVE_DURATION` (3 days) when the repo is
    unavailable, the setting is unset, or the stored value is not a valid Discord
    duration. This guarantees the value passed to ``create_thread`` is always one
    the Discord API accepts.
    """
    if settings_repo is None:
        return DEFAULT_AUTO_ARCHIVE_DURATION

    stored = await settings_repo.get(SETTING_THREAD_AUTO_ARCHIVE)
    if stored is None:
        return DEFAULT_AUTO_ARCHIVE_DURATION

    try:
        value = int(stored)
    except (TypeError, ValueError):
        return DEFAULT_AUTO_ARCHIVE_DURATION

    if value not in VALID_DURATIONS:
        return DEFAULT_AUTO_ARCHIVE_DURATION
    return cast("ThreadArchiveDuration", value)
