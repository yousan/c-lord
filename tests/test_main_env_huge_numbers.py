"""#1256: a number too long for ``int()`` in the .env must not crash the start.

``int()`` of more than 4300 digits raises ``ValueError``. #1237 fixed that for
``CLORD_API_PORT`` and ``CLORD_TRUSTED_BOT_IDS``, but ``MAX_CONCURRENT_SESSIONS``,
``SESSION_TIMEOUT_SECONDS`` and ``COORDINATION_CHANNEL_ID`` still died with a
traceback that named no setting (#1119 promises the setting's name). Like the
port and the trusted ids, such a value is now a WARNING that names the setting,
and the default is used instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from c_lord.coordination import env as cenv
from c_lord.main import load_config

_DEFAULTS = {"MAX_CONCURRENT_SESSIONS": "3", "SESSION_TIMEOUT_SECONDS": "300"}
_CONFIG_KEY = {"MAX_CONCURRENT_SESSIONS": "max_concurrent", "SESSION_TIMEOUT_SECONDS": "timeout"}


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "123")
    for key in ("MAX_CONCURRENT_SESSIONS", "SESSION_TIMEOUT_SECONDS", "COORDINATION_CHANNEL_ID"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("key", ["MAX_CONCURRENT_SESSIONS", "SESSION_TIMEOUT_SECONDS"])
def test_numeric_limit_with_5000_digits_warns_and_uses_the_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    key: str,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv(key, "9" * 5000)
    env = tmp_path / ".env"
    env.write_text("")
    with caplog.at_level(logging.WARNING):
        config = load_config(env)  # must not raise
    assert config[_CONFIG_KEY[key]] == _DEFAULTS[key]
    assert int(config[_CONFIG_KEY[key]]) >= 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(key in m for m in warnings), warnings
    # The 5000 digits are not echoed into the log.
    assert all(len(m) < 400 for m in warnings), [len(m) for m in warnings]


@pytest.mark.parametrize("key", ["MAX_CONCURRENT_SESSIONS", "SESSION_TIMEOUT_SECONDS"])
def test_leading_zeros_do_not_count_as_digits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv(key, "0" * 5000 + "7")
    env = tmp_path / ".env"
    env.write_text("")
    assert int(load_config(env)[_CONFIG_KEY[key]]) == 7


def test_coordination_channel_5000_digits() -> None:
    with patch.dict("os.environ", {"COORDINATION_CHANNEL_ID": "9" * 5000}):
        assert cenv.coordination_channel_id_from_env() is None  # must not raise


def test_coordination_channel_20_digits_still_read() -> None:
    with patch.dict("os.environ", {"COORDINATION_CHANNEL_ID": "1" + "0" * 19}):
        assert cenv.coordination_channel_id_from_env() == 10**19


def test_load_config_coordination_5000_digits_warns_and_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("COORDINATION_CHANNEL_ID", "9" * 5000)
    env = tmp_path / ".env"
    env.write_text("")
    with caplog.at_level(logging.WARNING):
        config = load_config(env)
    # main() does int(config["coordination_channel_id"]) when it is set.
    assert config["coordination_channel_id"] == ""
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("COORDINATION_CHANNEL_ID" in m for m in warnings), warnings
