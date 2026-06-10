"""Tests for c_lord.main path/config resolution.

Issue #202: ``c-lord start --env <path>`` must resolve data files (sessions.db,
notifications.db) and load the .env relative to the given path, not the CWD.
"""

from __future__ import annotations

import os
from pathlib import Path

from c_lord.main import load_config, resolve_data_dir


class TestResolveDataDir:
    def test_env_path_anchors_data_dir(self, tmp_path: Path) -> None:
        env_path = tmp_path / "cfg" / ".env"
        assert resolve_data_dir(env_path) == tmp_path / "cfg" / "data"

    def test_none_falls_back_to_cwd_relative(self) -> None:
        # Standalone `python -m c_lord.main` keeps the legacy CWD-relative dir.
        assert resolve_data_dir(None) == Path("data")


class TestLoadConfigEnvPath:
    def test_loads_env_from_given_path(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(
            "DISCORD_BOT_TOKEN=tok-from-file\nDISCORD_CHANNEL_ID=999\n",
            encoding="utf-8",
        )
        # Ensure the value isn't already in the environment.
        os.environ.pop("DISCORD_BOT_TOKEN", None)
        os.environ.pop("DISCORD_CHANNEL_ID", None)
        try:
            config = load_config(env_path)
            assert config["token"] == "tok-from-file"
            assert config["channel_id"] == "999"
        finally:
            os.environ.pop("DISCORD_BOT_TOKEN", None)
            os.environ.pop("DISCORD_CHANNEL_ID", None)


class TestExpectedBotUserId:
    """Issue #323: EXPECTED_BOT_USER_ID config key for the identity fail-fast guard."""

    def _base_env(self, monkeypatch: object) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")  # type: ignore[attr-defined]
        monkeypatch.setenv("DISCORD_CHANNEL_ID", "999")  # type: ignore[attr-defined]

    def test_parsed_when_set(self, monkeypatch: object) -> None:
        self._base_env(monkeypatch)
        monkeypatch.setenv("EXPECTED_BOT_USER_ID", "1503195981142032405")  # type: ignore[attr-defined]
        config = load_config()
        assert config["expected_bot_user_id"] == "1503195981142032405"

    def test_empty_when_unset(self, monkeypatch: object) -> None:
        self._base_env(monkeypatch)
        monkeypatch.delenv("EXPECTED_BOT_USER_ID", raising=False)  # type: ignore[attr-defined]
        config = load_config()
        assert config["expected_bot_user_id"] == ""

    def test_non_numeric_value_exits(self, monkeypatch: object) -> None:
        """A garbage value must fail loudly — a guard that silently disables
        itself is worse than no guard."""
        import sys
        from unittest.mock import patch

        self._base_env(monkeypatch)
        monkeypatch.setenv("EXPECTED_BOT_USER_ID", "not-a-number")  # type: ignore[attr-defined]
        with patch.object(sys, "exit") as mock_exit:
            load_config()
        mock_exit.assert_called_with(1)
