"""#1218: ``CLORD_TRUSTED_BOT_IDS`` / ``CLORD_API_PORT`` with ``²`` raised instead of being refused.

Both were read with ``isdigit()`` then ``int()``. ``²`` is ``isdigit()`` but not
``int()``-able, so one stray character in the trusted-bot list made every bot
message raise ``ValueError`` (the correct bot 123 was ignored too), and a ``²``
port crashed the start with a traceback that named no setting. Like the other
IDs (#1084 / #1194), only ASCII digits count; anything else is ignored with a
WARNING that names the setting — never an exception.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_trusted_bot_ids_superscript(monkeypatch: pytest.MonkeyPatch) -> None:
    from c_lord.command_gate import _trusted_bot_ids

    monkeypatch.setenv("CLORD_TRUSTED_BOT_IDS", "123,²")
    assert _trusted_bot_ids() == {123}


def test_trusted_bot_ids_fullwidth(monkeypatch: pytest.MonkeyPatch) -> None:
    from c_lord.command_gate import _trusted_bot_ids

    monkeypatch.setenv("CLORD_TRUSTED_BOT_IDS", "１２３")
    assert _trusted_bot_ids() in (set(), {123})  # int("１２３") == 123; record no raise


@pytest.mark.parametrize("value", ["²", " 8099"])
async def test_api_port_env_odd_values_do_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from c_lord.main import build_api_server

    monkeypatch.setenv("CLORD_API_PORT", value)
    srv = await build_api_server(MagicMock(), default_channel_id=1, data_dir=tmp_path)
    assert srv is not None


async def test_api_port_with_spaces_is_the_pinned_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``KEY=" 8099"`` pins 8099, as spaces around the IDs are ignored (#1194)."""
    from c_lord.main import build_api_server

    monkeypatch.setenv("CLORD_API_PORT", " 8099")
    srv = await build_api_server(MagicMock(), default_channel_id=1, data_dir=tmp_path)
    assert srv is not None
    assert srv.port == 8099


async def test_bad_api_port_is_ignored_with_a_warning_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from c_lord.main import DEFAULT_API_PORT, build_api_server

    monkeypatch.setenv("CLORD_API_PORT", "²")
    with caplog.at_level(logging.WARNING, logger="c_lord.main"):
        srv = await build_api_server(MagicMock(), default_channel_id=1, data_dir=tmp_path)
    assert srv is not None and srv.port == DEFAULT_API_PORT
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "CLORD_API_PORT" in joined and repr("²") in joined


def test_load_config_warns_about_a_bad_trusted_bot_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from c_lord.main import load_config

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "999")
    monkeypatch.setenv("CLORD_TRUSTED_BOT_IDS", "123, ²")
    with caplog.at_level(logging.WARNING, logger="c_lord.main"):
        load_config()  # starts: the good id still works
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "CLORD_TRUSTED_BOT_IDS" in joined and repr("²") in joined
