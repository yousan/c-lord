"""Session Status board: what a failing Discord call is allowed to cost (#720 / #930).

The board is decoration (#632). #930's AC1 table sorted every Discord call in
``thread_dashboard.py``; the decisions it recorded are pinned here:

* ``_adopt`` / ``_refresh_dashboard`` edit and re-post — **飾り**: they run from
  ``initialize()``, which ``on_ready`` calls unguarded, so a DNS blip or a
  timeout used to take the slash-command sync down with it. They now catch
  ``DISCORD_CALL_FAILURES``.
* old-board ``delete`` in the sweep — **既に広い**: "直下の except Exception で止まる
  （独立タスク。通信例外は失敗回数に数えられない）".
* completion mention ``send`` — **既に広い**: "set_state は safe_set_state
  （except Exception）経由でしか呼ばれず、後続処理も無い".

Plus the #720 sweep rules (NotFound counts as gone; give up after
``_SWEEP_MAX_FAILURES`` refused deletes) and the discord.py < 2.6 ``pins()``
shape (a coroutine returning a list) that #720 relies on to find the boards
holding the pin slots hostage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

from c_lord.discord_ui.thread_dashboard import (
    _SWEEP_MAX_FAILURES,
    DASHBOARD_TITLE,
    ThreadState,
    ThreadStatusDashboard,
    safe_set_state,
)

_BOT_ID = 4242
_NETWORK_ERRORS = [
    pytest.param(
        lambda: aiohttp.ClientConnectionError("Temporary failure in name resolution"), id="dns"
    ),
    pytest.param(asyncio.TimeoutError, id="timeout"),
    pytest.param(lambda: OSError("Connection refused"), id="oserror"),
]


@pytest.fixture(autouse=True)
def _board_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLORD_SESSION_STATUS_BOARD", "1")


async def _aiter(items: list[MagicMock]) -> AsyncIterator[MagicMock]:
    for item in items:
        yield item


def _board(msg_id: int, *, pinned: bool = True) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.author = MagicMock()
    msg.author.id = _BOT_ID
    msg.pinned = pinned
    embed = MagicMock(spec=discord.Embed)
    embed.title = DASHBOARD_TITLE
    msg.embeds = [embed]
    msg.pin = AsyncMock()
    msg.edit = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _channel(history: list[MagicMock] | None = None, pins: object = None) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 1
    channel.send = AsyncMock(return_value=_board(999_000))
    existing = list(history or [])
    channel.history = MagicMock(side_effect=lambda **_kw: _aiter(existing))
    if pins is None:
        channel.pins = MagicMock(side_effect=lambda **_kw: _aiter([]))
    else:
        channel.pins = pins
    return channel


def _http(status: int = 403) -> discord.HTTPException:
    return discord.HTTPException(MagicMock(status=status), "Missing Permissions")


def _not_found() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404), "Unknown Message")


async def _swept(d: ThreadStatusDashboard) -> None:
    if d._sweep_task is not None:
        await asyncio.wait_for(d._sweep_task, 2)


# ── finding the boards behind 📌 (#720) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_pins_as_a_coroutine_still_finds_the_hostage_board() -> None:
    """discord.py < 2.6: ``pins()`` is awaited for a list (c-lord supports >=2.4).

    #720: 📌 pointed at 2026-02-24 boards far older than any history scan — the
    pin list is the only place they are found.
    """
    live = _board(300)
    hostage = _board(100)
    channel = _channel(history=[live], pins=AsyncMock(return_value=[hostage]))
    d = ThreadStatusDashboard(channel, bot_user_id=_BOT_ID)
    await d.initialize()
    await _swept(d)
    assert d._dashboard_message is live
    hostage.delete.assert_awaited_once()
    live.delete.assert_not_awaited()
    channel.send.assert_not_awaited()
    await d.aclose()


# ── #930: network failures in the initialize() path ─────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("make_exc", _NETWORK_ERRORS)
async def test_a_board_that_cannot_be_taken_over_offline_is_treated_as_dead(make_exc) -> None:
    """#930: ``_adopt`` edit on a network blip → a fresh board, never a raise."""
    old = _board(100)
    old.edit = AsyncMock(side_effect=make_exc())
    channel = _channel(history=[old])
    d = ThreadStatusDashboard(channel, bot_user_id=_BOT_ID)
    await d.initialize()  # on_ready calls this unguarded — it must not raise
    await _swept(d)
    channel.send.assert_awaited_once()
    assert d._dashboard_message is channel.send.return_value
    old.delete.assert_awaited_once()
    await d.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("make_exc", _NETWORK_ERRORS)
async def test_a_board_edit_that_fails_offline_does_not_break_set_state(make_exc) -> None:
    """#930 (thread_dashboard.py:648): the board edit is decoration."""
    channel = _channel()
    d = ThreadStatusDashboard(channel, bot_user_id=_BOT_ID)
    await d.initialize()
    board = d._dashboard_message
    assert board is not None
    board.edit = AsyncMock(side_effect=make_exc())
    await d.set_state(1, ThreadState.PROCESSING, "x")
    assert d._threads[1].state == ThreadState.PROCESSING
    assert d._dashboard_message is board  # a failed edit is not "deleted"
    await d.aclose()


@pytest.mark.asyncio
async def test_a_deleted_board_is_reposted_on_the_next_change() -> None:
    channel = _channel()
    d = ThreadStatusDashboard(channel, bot_user_id=_BOT_ID)
    await d.initialize()
    gone = d._dashboard_message
    assert gone is not None
    gone.edit = AsyncMock(side_effect=_not_found())
    reposted = _board(999_001)
    channel.send = AsyncMock(return_value=reposted)
    await d.set_state(1, ThreadState.PROCESSING, "x")
    channel.send.assert_awaited_once()
    assert d._dashboard_message is reposted
    await d.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("make_exc", _NETWORK_ERRORS)
async def test_a_repost_that_fails_offline_is_logged_not_raised(
    make_exc, caplog: pytest.LogCaptureFixture
) -> None:
    """#930 (thread_dashboard.py:646): the re-post is decoration too."""
    channel = _channel()
    d = ThreadStatusDashboard(channel, bot_user_id=_BOT_ID)
    await d.initialize()
    gone = d._dashboard_message
    assert gone is not None
    gone.edit = AsyncMock(side_effect=_not_found())
    channel.send = AsyncMock(side_effect=make_exc())
    with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.thread_dashboard"):
        await d.set_state(1, ThreadState.PROCESSING, "x")
    assert any("re-post" in r.getMessage() for r in caplog.records)
    await d.aclose()


# ── #720 sweep rules ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_board_someone_else_already_deleted_counts_as_swept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    live = _board(300)
    raced = _board(200)
    raced.delete = AsyncMock(side_effect=_not_found())
    dead = _board(100)
    d = ThreadStatusDashboard(_channel(history=[live, raced, dead]), bot_user_id=_BOT_ID)
    with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.thread_dashboard"):
        await d.initialize()
        await _swept(d)
    summary = [r.getMessage() for r in caplog.records if "sweep: deleted" in r.getMessage()]
    assert summary == [
        "Session Status sweep: deleted 2 dead board(s), 0 failed, 0 left for the next start"
    ]
    await d.aclose()


@pytest.mark.asyncio
async def test_the_sweep_stops_after_repeated_refusals(caplog: pytest.LogCaptureFixture) -> None:
    """Missing Manage Messages: stop after a few refusals instead of 400 doomed calls."""
    live = _board(1000)
    dead = [_board(900 - i) for i in range(_SWEEP_MAX_FAILURES + 3)]
    for m in dead:
        m.delete = AsyncMock(side_effect=_http(403))
    d = ThreadStatusDashboard(_channel(history=[live, *dead]), bot_user_id=_BOT_ID)
    with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.thread_dashboard"):
        await d.initialize()
        await _swept(d)
    attempted = [m for m in dead if m.delete.await_count]
    assert len(attempted) == _SWEEP_MAX_FAILURES
    assert any("Stopping the Session Status sweep" in r.getMessage() for r in caplog.records)
    await d.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("make_exc", _NETWORK_ERRORS)
async def test_a_network_failure_aborts_the_sweep_and_leaves_the_live_board(
    make_exc, caplog: pytest.LogCaptureFixture
) -> None:
    """#930 table: the sweep's delete is "既に広い" — a network error aborts it.

    It is a background task, so the live board and the caller are untouched;
    what is left over is the next start's job.
    """
    live = _board(300)
    first = _board(200)
    first.delete = AsyncMock(side_effect=make_exc())
    second = _board(100)
    d = ThreadStatusDashboard(_channel(history=[live, first, second]), bot_user_id=_BOT_ID)
    with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.thread_dashboard"):
        await d.initialize()
        await _swept(d)
    second.delete.assert_not_awaited()
    assert d._dashboard_message is live
    assert any("sweep aborted unexpectedly" in r.getMessage() for r in caplog.records)
    await d.aclose()


# ── #761: board off — retire once per process ────────────────────────────────


@pytest.mark.asyncio
async def test_board_off_retires_old_boards_only_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_ready`` fires again on every reconnect; the retire scan must not."""
    monkeypatch.setenv("CLORD_SESSION_STATUS_BOARD", "0")
    old = _board(100)
    channel = _channel(history=[old])
    d = ThreadStatusDashboard(channel, bot_user_id=_BOT_ID)
    await d.initialize()
    await _swept(d)
    await d.initialize()  # a reconnect
    assert channel.history.call_count == 1
    old.delete.assert_awaited_once()
    channel.send.assert_not_awaited()


# ── the completion mention relies on safe_set_state (#930 "既に広い") ────────


@pytest.mark.asyncio
@pytest.mark.parametrize("make_exc", _NETWORK_ERRORS)
async def test_a_completion_ping_lost_to_the_network_does_not_escape_safe_set_state(
    make_exc, caplog: pytest.LogCaptureFixture
) -> None:
    """The mention ``send`` only catches HTTPException; ``safe_set_state`` is the net.

    The state itself has already moved to WAITING_INPUT — the turn is over even
    though its ping did not go out.
    """
    d = ThreadStatusDashboard(_channel(), bot_user_id=_BOT_ID)
    thread = MagicMock(spec=discord.Thread)
    thread.id = 9300
    thread.send = AsyncMock(side_effect=make_exc())
    await safe_set_state(d, 9300, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=7)
    with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.thread_dashboard"):
        await safe_set_state(
            d, 9300, ThreadState.WAITING_INPUT, "x", thread=thread, notify_user_id=7
        )
    thread.send.assert_awaited_once()
    assert d._threads[9300].state == ThreadState.WAITING_INPUT
    assert any("continuing the turn" in r.getMessage() for r in caplog.records)
