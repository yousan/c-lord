"""``SettingsRepository`` — plain string settings by key.

``c_lord.model_scopes`` reads and writes the same table with SQL of its own, so
what matters here is that values go in and come out untouched.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from c_lord.database.models import init_db
from c_lord.database.settings_repo import SettingsRepository


@pytest.fixture
async def settings(tmp_path: Path) -> SettingsRepository:
    path = str(tmp_path / "sessions.db")
    await init_db(path)
    return SettingsRepository(path)


class TestGet:
    async def test_a_key_never_set_is_none(self, settings: SettingsRepository) -> None:
        assert await settings.get("never-set") is None

    async def test_a_key_never_set_gives_the_default(self, settings: SettingsRepository) -> None:
        assert await settings.get("never-set", default="fallback") == "fallback"

    async def test_a_stored_value_wins_over_the_default(self, settings: SettingsRepository) -> None:
        await settings.set("model", "opus")

        assert await settings.get("model", default="fallback") == "opus"

    async def test_a_stored_empty_string_is_a_value(self, settings: SettingsRepository) -> None:
        """Only a missing row falls back to the default — ``""`` was stored on purpose."""
        await settings.set("cleared", "")

        assert await settings.get("cleared", default="fallback") == ""


class TestSet:
    async def test_what_is_set_can_be_read_back(self, settings: SettingsRepository) -> None:
        await settings.set("model", "sonnet")

        assert await settings.get("model") == "sonnet"

    async def test_setting_again_replaces_the_value(self, settings: SettingsRepository) -> None:
        await settings.set("model", "sonnet")
        await settings.set("model", "opus")

        assert await settings.get("model") == "opus"
        assert await settings.get_all() == {"model": "opus"}

    async def test_keys_do_not_disturb_each_other(self, settings: SettingsRepository) -> None:
        await settings.set("a", "1")
        await settings.set("b", "2")
        await settings.set("a", "3")

        assert await settings.get("b") == "2"

    async def test_the_value_is_stored_as_plain_text(self, settings: SettingsRepository) -> None:
        """Other code reads the table directly: no encoding, no wrapping."""
        await settings.set("context_window:claude-x", "1000000")

        async with aiosqlite.connect(settings.db_path) as db:
            cursor = await db.execute("SELECT key, value FROM settings")
            rows = [tuple(row) for row in await cursor.fetchall()]

        assert rows == [("context_window:claude-x", "1000000")]


class TestDelete:
    async def test_removes_the_key_and_says_so(self, settings: SettingsRepository) -> None:
        await settings.set("doomed", "x")
        await settings.set("kept", "y")

        assert await settings.delete("doomed") is True

        assert await settings.get("doomed") is None
        assert await settings.get("kept") == "y"

    async def test_a_key_that_is_not_there_is_false(self, settings: SettingsRepository) -> None:
        assert await settings.delete("never-set") is False


class TestGetAll:
    async def test_no_settings_is_an_empty_dict(self, settings: SettingsRepository) -> None:
        assert await settings.get_all() == {}

    async def test_every_setting_in_key_order(self, settings: SettingsRepository) -> None:
        await settings.set("zeta", "26")
        await settings.set("alpha", "1")
        await settings.set("mid", "13")

        everything = await settings.get_all()

        assert everything == {"alpha": "1", "mid": "13", "zeta": "26"}
        assert list(everything) == ["alpha", "mid", "zeta"]
