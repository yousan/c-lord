"""Tests for c_lord.main path/config resolution.

Issue #202: ``c-lord start --env <path>`` must resolve data files (sessions.db,
notifications.db) and load the .env relative to the given path, not the CWD.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from c_lord.main import acquire_token_lock, load_config, resolve_data_dir


class TestTokenLock:
    """Issue #325: host-global flock keyed on the bot TOKEN, not the data dir.

    The #212 data-dir flock cannot stop a same-token double-connect from a
    different clone (different data dir -> different lock file). The real
    invariant is "one process per token"; this lock enforces it before the
    Discord gateway is ever touched.
    """

    def test_same_token_second_acquire_exits(self, tmp_path: Path) -> None:
        import sys
        from unittest.mock import patch

        first = acquire_token_lock("tok-A", lock_dir=tmp_path)
        assert first is not None
        with patch.object(sys, "exit") as mock_exit:
            acquire_token_lock("tok-A", lock_dir=tmp_path)
        mock_exit.assert_called_with(1)
        first.close()

    def test_different_tokens_both_acquire(self, tmp_path: Path) -> None:
        a = acquire_token_lock("tok-A", lock_dir=tmp_path)
        b = acquire_token_lock("tok-B", lock_dir=tmp_path)
        assert a is not None and b is not None
        a.close()
        b.close()

    def test_no_plaintext_token_on_disk(self, tmp_path: Path) -> None:
        token = "super-secret-token-value"
        handle = acquire_token_lock(token, lock_dir=tmp_path)
        assert handle is not None
        try:
            for f in tmp_path.iterdir():
                assert token not in f.name
                assert token not in f.read_text(errors="replace")
        finally:
            handle.close()

    def test_refusal_reports_holder_pid_and_cwd(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        import sys
        from unittest.mock import patch

        first = acquire_token_lock("tok-A", lock_dir=tmp_path)
        assert first is not None
        with (
            caplog.at_level(logging.ERROR, logger="c_lord.main"),
            patch.object(sys, "exit"),
        ):
            acquire_token_lock("tok-A", lock_dir=tmp_path)
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert str(os.getpid()) in joined  # holder pid
        assert str(Path.cwd()) in joined  # holder cwd
        first.close()

    def test_multi_instance_escape_hatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLORD_ALLOW_MULTI_INSTANCE", "1")
        assert acquire_token_lock("tok-A", lock_dir=tmp_path) is None


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


class TestDotenvOverride:
    """Issue #324: keys present in the .env file must beat inherited process env.

    A Claude session spawned by the prod bot inherits prod's DISCORD_* vars;
    with the python-dotenv default (override=False) those silently win over
    the staging .env and the "staging" bot boots as production (#322 根因A).
    Directory == identity: what the .env file says is what the bot is.
    """

    def test_env_file_beats_inherited_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "inherited-prod-tok")
        monkeypatch.setenv("DISCORD_CHANNEL_ID", "111")
        env_path = tmp_path / ".env"
        env_path.write_text(
            "DISCORD_BOT_TOKEN=file-tok\nDISCORD_CHANNEL_ID=222\n", encoding="utf-8"
        )
        config = load_config(env_path)
        assert config["token"] == "file-tok"
        assert config["channel_id"] == "222"

    def test_keys_absent_from_file_fall_back_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env-var-only setups (no key in .env) keep working — override only
        applies to keys the file actually defines."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
        monkeypatch.setenv("DISCORD_CHANNEL_ID", "1")
        monkeypatch.setenv("CLAUDE_MODEL", "model-from-env")
        env_path = tmp_path / ".env"
        env_path.write_text(
            "DISCORD_BOT_TOKEN=file-tok\nDISCORD_CHANNEL_ID=222\n", encoding="utf-8"
        )
        config = load_config(env_path)
        assert config["claude_model"] == "model-from-env"

    def test_override_applies_to_cwd_dotenv_without_explicit_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bare `python -m c_lord.main` launch (env_path=None) is the exact
        path the staging incidents used — it must also prefer the CWD .env."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "inherited-prod-tok")
        monkeypatch.setenv("DISCORD_CHANNEL_ID", "111")
        (tmp_path / ".env").write_text(
            "DISCORD_BOT_TOKEN=cwd-file-tok\nDISCORD_CHANNEL_ID=333\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        config = load_config()
        assert config["token"] == "cwd-file-tok"
        assert config["channel_id"] == "333"
