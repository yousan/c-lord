"""#1237: an out-of-range ``CLORD_API_PORT`` or a huge digit string stopped the bot.

#1218 made the ID / port settings read ASCII digits only, but never looked at the
range. ``CLORD_API_PORT=99999`` (one typo) raised ``OverflowError`` from the bind,
which ``start_api_server`` did not catch, so the whole bot failed to start and every
thread went silent. A digit string longer than Python's int limit (4300 digits)
raised ``ValueError`` both for the port (start crashed) and for an entry of
``CLORD_TRUSTED_BOT_IDS`` (every bot message raised, so even the correctly listed
bot was ignored). Like #1218: a WARNING naming the setting, the value ignored, the
bot keeps running.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_trusted_bot_ids_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    from c_lord.command_gate import _trusted_bot_ids

    monkeypatch.setenv("CLORD_TRUSTED_BOT_IDS", " , 0 ,-5, 1_0,+7, 123 ," + "9" * 5000)
    ids = _trusted_bot_ids()  # no raise (int string limit is 4300 digits)
    assert 123 in ids


@pytest.mark.parametrize(
    "value",
    ["", "  ", "0", "-1", "99999", "9" * 5000, " 8099 ", "\t"],
    ids=lambda v: repr(v) if len(v) < 20 else f"{len(v)}-digits",
)
async def test_api_port_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from c_lord.main import build_api_server

    monkeypatch.setenv("CLORD_API_PORT", value)
    srv = await build_api_server(MagicMock(), default_channel_id=1, data_dir=tmp_path)
    assert srv is not None


@pytest.mark.parametrize("value", ["99999", "65536"])
async def test_out_of_range_api_port_does_not_crash_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """CLORD_API_PORT=99999 (ASCII digits) passes the #1218 check; start must not raise."""
    from c_lord.main import build_api_server, start_api_server

    monkeypatch.setenv("CLORD_API_PORT", value)
    monkeypatch.setenv("CLORD_API_HOST", "127.0.0.1")
    srv = await build_api_server(MagicMock(), default_channel_id=1, data_dir=tmp_path)
    try:
        ok = await start_api_server(srv)  # expected: False + WARNING, no raise
        assert ok is False
    finally:
        if srv is not None:
            await srv.stop()
