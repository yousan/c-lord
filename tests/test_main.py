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
