"""#761 — 📊 Session Status is opt-in; by default a starting bot posts nothing.

yousan (2026-09-16): 「C-lordの再接続時、ステータスを出すってのをオプショナルにして
デフォルトオフにして欲しい」. Development restarts the bot six times a day, and
every start used to post or rewrite the board in the channel.

Two things must stay true while the board is off:

* **the turn-end ping still fires** — ``🟡 Claude has finished … @poster`` is
  sent by the same class, but it is a different feature (#481);
* **no frozen board is left behind** — a board nobody updates any more is the
  #754 lie ("0s ago" for days), so a start with the board off retires the
  boards earlier starts left (the #720 sweep, same rules).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.discord_ui.thread_dashboard import (
    DASHBOARD_TITLE,
    ThreadState,
    ThreadStatusDashboard,
    board_enabled,
)

_BOT_ID = 4242


async def _aiter(items: list[MagicMock]) -> AsyncIterator[MagicMock]:
    for item in items:
        yield item


def _board_message(msg_id: int) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.author = MagicMock()
    msg.author.id = _BOT_ID
    msg.pinned = True
    embed = MagicMock(spec=discord.Embed)
    embed.title = DASHBOARD_TITLE
    msg.embeds = [embed]
    msg.edit = AsyncMock()
    msg.pin = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _channel(history: list[MagicMock] | None = None) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 1
    channel.send = AsyncMock(return_value=_board_message(999))
    existing = list(history or [])
    channel.history = MagicMock(side_effect=lambda **_kw: _aiter(existing))
    channel.pins = MagicMock(side_effect=lambda **_kw: _aiter([]))
    return channel


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLORD_SESSION_STATUS_BOARD", raising=False)
    monkeypatch.delenv("CLORD_DASHBOARD_SWEEP", raising=False)


class TestSwitch:
    def test_off_by_default(self) -> None:
        assert board_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", " ON "])
    def test_opt_in_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("CLORD_SESSION_STATUS_BOARD", value)
        assert board_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_everything_else_is_off(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("CLORD_SESSION_STATUS_BOARD", value)
        assert board_enabled() is False


class TestOffByDefault:
    async def test_start_posts_no_board(self) -> None:
        channel = _channel()
        dashboard = ThreadStatusDashboard(channel=channel, bot_user_id=_BOT_ID)

        await dashboard.initialize()

        channel.send.assert_not_awaited()

    async def test_state_changes_do_not_touch_the_channel(self) -> None:
        channel = _channel()
        dashboard = ThreadStatusDashboard(channel=channel, bot_user_id=_BOT_ID)
        await dashboard.initialize()

        await dashboard.set_state(1, ThreadState.PROCESSING, "hi")
        await dashboard.set_state(1, ThreadState.WAITING_INPUT, "hi")

        channel.send.assert_not_awaited()

    async def test_turn_end_ping_still_fires(self) -> None:
        """The #481 ping is not the board — turning the board off must not mute it."""
        dashboard = ThreadStatusDashboard(channel=_channel(), bot_user_id=_BOT_ID)
        await dashboard.initialize()
        thread = MagicMock(spec=discord.Thread)
        thread.send = AsyncMock()

        await dashboard.set_state(7, ThreadState.PROCESSING, "hi", thread=thread)
        await dashboard.set_state(
            7, ThreadState.WAITING_INPUT, "hi", thread=thread, notify_user_id=123
        )

        thread.send.assert_awaited_once()
        assert "<@123>" in thread.send.call_args.args[0]

    async def test_board_left_by_an_earlier_start_is_retired_not_rewritten(self) -> None:
        old = _board_message(500)
        dashboard = ThreadStatusDashboard(channel=_channel([old]), bot_user_id=_BOT_ID)

        await dashboard.initialize()
        assert dashboard._sweep_task is not None
        await dashboard._sweep_task

        old.edit.assert_not_awaited()
        old.delete.assert_awaited_once()

    async def test_sweep_opt_out_keeps_the_old_board(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_DASHBOARD_SWEEP", "0")
        old = _board_message(500)
        dashboard = ThreadStatusDashboard(channel=_channel([old]), bot_user_id=_BOT_ID)

        await dashboard.initialize()
        if dashboard._sweep_task is not None:
            await dashboard._sweep_task

        old.delete.assert_not_awaited()

    async def test_reconnect_does_not_rescan(self) -> None:
        channel = _channel([_board_message(500)])
        dashboard = ThreadStatusDashboard(channel=channel, bot_user_id=_BOT_ID)

        await dashboard.initialize()
        await dashboard.initialize()

        assert channel.history.call_count == 1


class TestOptIn:
    async def test_opted_in_start_posts_the_board(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_SESSION_STATUS_BOARD", "1")
        channel = _channel()
        dashboard = ThreadStatusDashboard(channel=channel, bot_user_id=_BOT_ID)

        await dashboard.initialize()

        channel.send.assert_awaited_once()

    async def test_explicit_constructor_argument_wins(self) -> None:
        channel = _channel()
        dashboard = ThreadStatusDashboard(channel=channel, bot_user_id=_BOT_ID, board=True)

        await dashboard.initialize()

        channel.send.assert_awaited_once()
