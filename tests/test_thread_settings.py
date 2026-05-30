"""Tests for thread auto-archive duration settings."""

from __future__ import annotations

import pytest

from c_lord.database.models import init_db
from c_lord.database.settings_repo import SettingsRepository
from c_lord.thread_settings import (
    DEFAULT_AUTO_ARCHIVE_DURATION,
    SETTING_THREAD_AUTO_ARCHIVE,
    VALID_DURATIONS,
    resolve_auto_archive_duration,
)


@pytest.fixture
async def repo(tmp_path):
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)
    return SettingsRepository(db_path)


class TestConstants:
    def test_default_is_three_days(self):
        # 3 days == 4320 minutes
        assert DEFAULT_AUTO_ARCHIVE_DURATION == 4320

    def test_default_is_a_valid_discord_duration(self):
        assert DEFAULT_AUTO_ARCHIVE_DURATION in VALID_DURATIONS

    def test_valid_durations_match_discord_api(self):
        assert set(VALID_DURATIONS) == {60, 1440, 4320, 10080}


class TestResolveAutoArchiveDuration:
    async def test_default_when_settings_repo_is_none(self):
        result = await resolve_auto_archive_duration(None)
        assert result == DEFAULT_AUTO_ARCHIVE_DURATION

    async def test_default_when_unset(self, repo):
        result = await resolve_auto_archive_duration(repo)
        assert result == DEFAULT_AUTO_ARCHIVE_DURATION

    async def test_honors_valid_stored_value(self, repo):
        await repo.set(SETTING_THREAD_AUTO_ARCHIVE, "10080")
        result = await resolve_auto_archive_duration(repo)
        assert result == 10080

    async def test_falls_back_on_invalid_stored_value(self, repo):
        # A value Discord would reject must not leak through.
        await repo.set(SETTING_THREAD_AUTO_ARCHIVE, "999")
        result = await resolve_auto_archive_duration(repo)
        assert result == DEFAULT_AUTO_ARCHIVE_DURATION

    async def test_falls_back_on_non_numeric_stored_value(self, repo):
        await repo.set(SETTING_THREAD_AUTO_ARCHIVE, "garbage")
        result = await resolve_auto_archive_duration(repo)
        assert result == DEFAULT_AUTO_ARCHIVE_DURATION
