"""Tests for the ThreadStatusDashboard — live session status embed."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.discord_ui.thread_dashboard import (
    DASHBOARD_TITLE,
    ThreadState,
    ThreadStatusDashboard,
    _elapsed_text,
    board_turn,
    dashboard_of,
    safe_set_state,
)

#: Seconds after which a row counts as stale (the board says "after 4h").
_FOUR_HOURS = 4 * 3600

#: The bot's own Discord user id in these tests.
_BOT_ID = 4242


@pytest.fixture(autouse=True)
def _board_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about the board itself, which is opt-in since #761."""
    monkeypatch.setenv("CLORD_SESSION_STATUS_BOARD", "1")


async def _aiter(items: list[MagicMock]) -> AsyncIterator[MagicMock]:
    """Async iterator over *items* — stands in for ``channel.history()``."""
    for item in items:
        yield item


def _make_message(
    msg_id: int,
    *,
    author_id: int = _BOT_ID,
    title: str | None = DASHBOARD_TITLE,
    pinned: bool = False,
) -> MagicMock:
    """Return a mocked ``discord.Message`` (a dashboard board by default)."""
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.pinned = pinned
    if title is None:
        msg.embeds = []
    else:
        embed = MagicMock(spec=discord.Embed)
        embed.title = title
        msg.embeds = [embed]
    msg.pin = AsyncMock()
    msg.unpin = AsyncMock()
    msg.edit = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _make_channel(
    history: list[MagicMock] | None = None,
    pins: list[MagicMock] | None = None,
) -> MagicMock:
    """Return a mocked discord.TextChannel.

    *history* / *pins* seed what the channel already holds — e.g. the boards a
    previous bot process left behind (#720).
    """
    channel = MagicMock(spec=discord.TextChannel)
    msg = _make_message(999_000)
    channel.send = AsyncMock(return_value=msg)
    existing = list(history or [])
    pinned = list(pins or [])
    channel.history = MagicMock(side_effect=lambda **_kw: _aiter(existing))
    channel.pins = MagicMock(side_effect=lambda **_kw: _aiter(pinned))
    return channel


def _make_dashboard(
    owner_id: int | None = None,
    *,
    history: list[MagicMock] | None = None,
    pins: list[MagicMock] | None = None,
) -> tuple[ThreadStatusDashboard, MagicMock]:
    """A dashboard on a mocked channel; the channel comes back for assertions."""
    channel = _make_channel(history=history, pins=pins)
    dashboard = ThreadStatusDashboard(channel=channel, owner_id=owner_id, bot_user_id=_BOT_ID)
    return dashboard, channel


def _make_thread() -> MagicMock:
    """A mocked ``discord.Thread`` the turn-end ping can be sent into."""
    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock()
    return thread


def _board_embed(message: MagicMock) -> discord.Embed:
    """The embed the board message was last painted with."""
    return message.edit.await_args.kwargs["embed"]


def _age(dashboard: ThreadStatusDashboard, thread_id: int, seconds: float) -> None:
    """Make *thread_id*'s row look as if its state last changed *seconds* ago."""
    dashboard._threads[thread_id].state_changed_at = time.monotonic() - seconds


# ---------------------------------------------------------------------------
# initialize()
# ---------------------------------------------------------------------------


class TestFirstBoard:
    """An empty channel gets one board, pinned, and startup survives a failed pin."""

    @pytest.mark.asyncio
    async def test_empty_channel_gets_one_board(self) -> None:
        dashboard, channel = _make_dashboard()

        await dashboard.initialize()

        channel.send.assert_awaited_once()
        embed = channel.send.await_args.kwargs["embed"]
        assert embed.title == DASHBOARD_TITLE
        assert embed.description == "No active sessions."
        assert dashboard._dashboard_message is channel.send.return_value

    @pytest.mark.asyncio
    async def test_new_board_is_pinned(self) -> None:
        dashboard, channel = _make_dashboard()

        await dashboard.initialize()

        channel.send.return_value.pin.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refused_pin_does_not_cost_the_board(self) -> None:
        """Missing Manage Messages costs the 📌, not the board."""
        dashboard, channel = _make_dashboard()
        board = channel.send.return_value
        board.pin.side_effect = discord.Forbidden(MagicMock(), "Missing Permissions")

        await dashboard.initialize()  # must not raise

        assert dashboard._dashboard_message is board

    @pytest.mark.asyncio
    async def test_channel_id_is_the_channel_the_board_lives_in(self) -> None:
        dashboard, channel = _make_dashboard()
        channel.id = 31337
        assert dashboard.channel_id == 31337


# ---------------------------------------------------------------------------
# One board per channel (#720)
# ---------------------------------------------------------------------------


class TestSingleBoard:
    """The channel holds exactly ONE Session Status board, whatever happens.

    #720: ``initialize()`` used to ``channel.send()`` unconditionally, and
    ``bot.on_ready`` calls it on every start — 369 dead boards in the
    production channel, 77% of everything ever posted there.
    """

    @pytest.mark.asyncio
    async def test_second_initialize_reuses_the_same_board(self) -> None:
        """AC4: two ``initialize()`` calls, one ``channel.send``."""
        dashboard, channel = _make_dashboard()

        await dashboard.initialize()
        await dashboard.initialize()

        assert channel.send.await_count == 1

    @pytest.mark.asyncio
    async def test_restart_adopts_the_board_left_by_the_previous_process(self) -> None:
        """AC1: a fresh process finds the existing board and edits it."""
        existing = _make_message(500)
        dashboard, channel = _make_dashboard(history=[existing])

        await dashboard.initialize()

        channel.send.assert_not_called()
        existing.edit.assert_awaited_once()
        assert dashboard._dashboard_message is existing

    @pytest.mark.asyncio
    async def test_restart_adopts_the_newest_board_and_deletes_the_rest(self) -> None:
        """AC1: the boards earlier restarts left behind are swept away."""
        newest = _make_message(900)
        older = _make_message(800)
        oldest = _make_message(700)
        dashboard, channel = _make_dashboard(history=[newest, older, oldest])

        await dashboard.initialize()
        await dashboard._sweep_task  # the sweep runs in the background

        channel.send.assert_not_called()
        assert dashboard._dashboard_message is newest
        newest.delete.assert_not_called()
        older.delete.assert_awaited_once()
        oldest.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stale_pinned_board_is_swept_even_when_outside_history(self) -> None:
        """The 2026-02 boards behind 📌 are found through the pin list."""
        ancient = _make_message(100, pinned=True)
        live = _make_message(900)
        dashboard, _channel = _make_dashboard(history=[live], pins=[ancient])

        await dashboard.initialize()
        await dashboard._sweep_task

        assert dashboard._dashboard_message is live
        ancient.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_other_authors_and_other_embeds_are_left_alone(self) -> None:
        """Only *our own* Session Status boards are adopted or deleted."""
        someone_else = _make_message(900, author_id=_BOT_ID + 1)
        other_embed = _make_message(800, title="🔧 Something else")
        no_embed = _make_message(700, title=None)
        dashboard, channel = _make_dashboard(history=[someone_else, other_embed, no_embed])

        await dashboard.initialize()

        channel.send.assert_awaited_once()
        for msg in (someone_else, other_embed, no_embed):
            msg.delete.assert_not_called()
            msg.edit.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_bot_identity_skips_the_scan(self) -> None:
        """Without a known bot user id we never touch anyone's messages."""
        existing = _make_message(500)
        channel = _make_channel(history=[existing])
        dashboard = ThreadStatusDashboard(channel=channel, owner_id=None, bot_user_id=None)

        await dashboard.initialize()

        channel.send.assert_awaited_once()
        existing.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_adopted_board_gets_pinned_when_it_is_not(self) -> None:
        """AC2: 📌 points at the live board, not at a half-year-old one."""
        existing = _make_message(500, pinned=False)
        dashboard, _channel = _make_dashboard(history=[existing])

        await dashboard.initialize()

        existing.pin.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_already_pinned_board_is_not_pinned_again(self) -> None:
        existing = _make_message(500, pinned=True)
        dashboard, _channel = _make_dashboard(history=[existing])

        await dashboard.initialize()

        existing.pin.assert_not_called()

    @pytest.mark.asyncio
    async def test_pin_failure_is_reported_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC2 / #678: a failed pin must not be a DEBUG-only whisper.

        The production channel's 📌 pointed at 2026-02-24 for half a year
        because the failure only ever went to DEBUG.
        """
        dashboard, channel = _make_dashboard()
        msg = channel.send.return_value
        msg.pin.side_effect = discord.HTTPException(MagicMock(), "Maximum number of pins reached")

        with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.thread_dashboard"):
            await dashboard.initialize()

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a failed pin must be visible at WARNING"
        assert "pin" in warnings[0].getMessage().lower()

    @pytest.mark.asyncio
    async def test_sweep_survives_a_board_that_cannot_be_deleted(self) -> None:
        """A dead board we may not delete must not break startup."""
        newest = _make_message(900)
        older = _make_message(800)
        older.delete.side_effect = discord.HTTPException(MagicMock(), "Missing Permissions")
        dashboard, _channel = _make_dashboard(history=[newest, older])

        await dashboard.initialize()
        await dashboard._sweep_task

        assert dashboard._dashboard_message is newest

    @pytest.mark.asyncio
    async def test_unreadable_history_still_yields_a_board(self) -> None:
        """No read_message_history? Post a board anyway — never crash on_ready."""
        channel = _make_channel()
        channel.history = MagicMock(side_effect=discord.Forbidden(MagicMock(), "no history"))
        dashboard = ThreadStatusDashboard(channel=channel, owner_id=None, bot_user_id=_BOT_ID)

        await dashboard.initialize()

        channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_board_that_cannot_be_edited_is_passed_over_for_the_next(self) -> None:
        """Taking over means editing; a board we cannot edit is dead, not live."""
        newest = _make_message(900)
        newest.edit.side_effect = discord.Forbidden(MagicMock(), "cannot edit")
        older = _make_message(800)
        dashboard, channel = _make_dashboard(history=[newest, older])

        await dashboard.initialize()
        await dashboard._sweep_task

        assert dashboard._dashboard_message is older
        channel.send.assert_not_called()
        newest.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_board_seen_in_history_and_pins_is_one_board(self) -> None:
        """The live board is usually both: it must not be swept as its own duplicate."""
        board = _make_message(900, pinned=True)
        dashboard, _channel = _make_dashboard(history=[board], pins=[board])

        await dashboard.initialize()

        assert dashboard._dashboard_message is board
        assert dashboard._sweep_task is None
        board.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_pins_given_as_an_awaitable_list_are_read_too(self) -> None:
        """discord.py < 2.6 returns a list to await from ``pins()``."""
        ancient = _make_message(100, pinned=True)
        live = _make_message(900)
        dashboard, channel = _make_dashboard(history=[live])
        channel.pins = AsyncMock(return_value=[ancient])

        await dashboard.initialize()
        await dashboard._sweep_task

        ancient.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bot_identity_falls_back_to_the_guild_member(self) -> None:
        existing = _make_message(500)
        channel = _make_channel(history=[existing])
        channel.guild.me.id = _BOT_ID
        dashboard = ThreadStatusDashboard(channel=channel, owner_id=None, bot_user_id=None)

        await dashboard.initialize()

        assert dashboard._dashboard_message is existing
        channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_can_be_switched_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``CLORD_DASHBOARD_SWEEP=0``: reuse the newest board, delete nothing."""
        monkeypatch.setenv("CLORD_DASHBOARD_SWEEP", "0")
        newest, older = _make_message(900), _make_message(800)
        dashboard, channel = _make_dashboard(history=[newest, older])

        await dashboard.initialize()
        await dashboard._sweep_task

        assert dashboard._dashboard_message is newest
        channel.send.assert_not_called()
        older.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_board_already_gone_does_not_stop_the_sweep(self) -> None:
        newest, gone, oldest = _make_message(900), _make_message(800), _make_message(700)
        gone.delete.side_effect = discord.NotFound(MagicMock(), "Unknown Message")
        dashboard, _channel = _make_dashboard(history=[newest, gone, oldest])

        await dashboard.initialize()
        await dashboard._sweep_task

        oldest.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sweep_gives_up_after_five_refused_deletes(self) -> None:
        """Repeated refusals mean "not allowed", not "try harder"."""
        dead = [_make_message(800 - i) for i in range(7)]
        for board in dead:
            board.delete.side_effect = discord.Forbidden(MagicMock(), "Missing Permissions")
        dashboard, _channel = _make_dashboard(history=[_make_message(900), *dead])

        await dashboard.initialize()
        await dashboard._sweep_task

        assert [board.delete.await_count for board in dead] == [1, 1, 1, 1, 1, 0, 0]


class TestBoardSwitchedOff:
    """#761: without the opt-in there is no board — but states and pings still work."""

    @pytest.fixture(autouse=True)
    def _board_off(self, monkeypatch: pytest.MonkeyPatch, _board_on: None) -> None:
        monkeypatch.delenv("CLORD_SESSION_STATUS_BOARD", raising=False)

    @pytest.mark.asyncio
    async def test_nothing_is_posted(self) -> None:
        dashboard, channel = _make_dashboard()

        await dashboard.initialize()

        channel.send.assert_not_called()
        assert dashboard._dashboard_message is None

    @pytest.mark.asyncio
    async def test_boards_of_earlier_starts_are_retired_once(self) -> None:
        """A board nobody updates lies ("0s ago" for days), so it goes — once per process."""
        leftover = _make_message(500)
        dashboard, channel = _make_dashboard(history=[leftover])

        await dashboard.initialize()
        await dashboard._sweep_task
        await dashboard.initialize()  # a reconnect

        leftover.delete.assert_awaited_once()
        leftover.edit.assert_not_called()
        assert channel.history.call_count == 1

    @pytest.mark.asyncio
    async def test_the_turn_end_ping_does_not_depend_on_the_board(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        thread = _make_thread()

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(
            10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=7
        )

        thread.send.assert_awaited_once()
        channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_constructor_argument_overrides_the_environment(self) -> None:
        channel = _make_channel()
        dashboard = ThreadStatusDashboard(channel=channel, bot_user_id=_BOT_ID, board=True)

        await dashboard.initialize()

        channel.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# set_state()
# ---------------------------------------------------------------------------


class TestStateChanges:
    """A state change lands in the table and on the board."""

    @pytest.mark.asyncio
    async def test_first_state_puts_the_thread_in_the_table(self) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.initialize()

        await dashboard.set_state(10, ThreadState.PROCESSING, "refactor the parser")

        row = dashboard._threads[10]
        assert row.state is ThreadState.PROCESSING
        assert row.description == "refactor the parser"

    @pytest.mark.asyncio
    async def test_state_change_repaints_the_board(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        board = channel.send.return_value

        await dashboard.set_state(10, ThreadState.PROCESSING, "refactor the parser")

        board.edit.assert_awaited_once()
        (row,) = _board_embed(board).fields
        assert row.name == "🟢 <#10>"
        assert "refactor the parser" in row.value

    @pytest.mark.asyncio
    async def test_state_before_the_board_is_up_is_only_recorded(self) -> None:
        """A turn can start before ``on_ready`` put the board up."""
        dashboard, channel = _make_dashboard()

        await dashboard.set_state(10, ThreadState.PROCESSING, "early bird")

        assert 10 in dashboard._threads
        channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_each_thread_gets_its_own_row(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()

        await dashboard.set_state(10, ThreadState.PROCESSING, "first")
        await dashboard.set_state(20, ThreadState.WAITING_INPUT, "second")
        await dashboard.set_state(30, ThreadState.PROCESSING, "third")

        assert set(dashboard._threads) == {10, 20, 30}
        assert len(_board_embed(channel.send.return_value).fields) == 3

    @pytest.mark.asyncio
    async def test_same_thread_again_moves_its_row(self) -> None:
        """The same thread again moves its row; it does not add a second one."""
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()

        await dashboard.set_state(10, ThreadState.PROCESSING, "the work")
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "the work")

        assert dashboard._threads[10].state is ThreadState.WAITING_INPUT
        (row,) = _board_embed(channel.send.return_value).fields
        assert row.name == "🟡 <#10>"

    @pytest.mark.asyncio
    async def test_empty_description_keeps_the_one_the_row_has(self) -> None:
        dashboard, _channel = _make_dashboard()

        await dashboard.set_state(10, ThreadState.PROCESSING, "what was asked")
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "")
        assert dashboard._threads[10].description == "what was asked"

        await dashboard.set_state(10, ThreadState.PROCESSING, "a new request")
        assert dashboard._threads[10].description == "a new request"

    @pytest.mark.asyncio
    async def test_touching_a_row_restarts_its_clock(self) -> None:
        """Even without a state change: the "ago" figure counts from the last touch."""
        dashboard, _channel = _make_dashboard()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w")
        _age(dashboard, 10, 600)

        await dashboard.set_state(10, ThreadState.PROCESSING, "w")

        assert time.monotonic() - dashboard._threads[10].state_changed_at < 60


# ---------------------------------------------------------------------------
# The board message disappearing or refusing an edit
# ---------------------------------------------------------------------------


class TestBoardRepair:
    """The board is decoration: losing it must never fail a state change."""

    @pytest.mark.asyncio
    async def test_a_deleted_board_is_posted_again(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        deleted = channel.send.return_value
        deleted.edit.side_effect = discord.NotFound(MagicMock(), "Unknown Message")
        replacement = _make_message(999_001)
        channel.send = AsyncMock(return_value=replacement)

        await dashboard.set_state(10, ThreadState.PROCESSING, "w")

        channel.send.assert_awaited_once()
        assert channel.send.await_args.kwargs["embed"].fields[0].name == "🟢 <#10>"
        assert dashboard._dashboard_message is replacement

        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w")
        replacement.edit.assert_awaited_once()  # later repaints go to the new one

    @pytest.mark.asyncio
    async def test_a_failed_repost_is_retried_on_the_next_change(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        channel.send.return_value.edit.side_effect = discord.NotFound(MagicMock(), "gone")
        channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no send"))

        await dashboard.set_state(10, ThreadState.PROCESSING, "w")  # must not raise
        await dashboard.set_state(20, ThreadState.PROCESSING, "w")

        assert channel.send.await_count == 2
        assert set(dashboard._threads) == {10, 20}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [discord.HTTPException(MagicMock(), "500"), TimeoutError(), OSError("dns")],
        ids=["http", "timeout", "network"],
    )
    async def test_an_edit_that_fails_otherwise_is_not_a_reason_to_repost(
        self, failure: BaseException
    ) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        board = channel.send.return_value
        board.edit.side_effect = failure

        await dashboard.set_state(10, ThreadState.PROCESSING, "w")  # must not raise

        assert channel.send.await_count == 1  # only the initial post
        assert dashboard._dashboard_message is board
        assert 10 in dashboard._threads


# ---------------------------------------------------------------------------
# Owner mention on WAITING_INPUT
# ---------------------------------------------------------------------------


class TestOwnerMention:
    """Arriving at 🟡 pings whoever is waiting — once, and never fatally."""

    @pytest.fixture(autouse=True)
    def _owner_fallback_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#525: these cover the owner *fallback* itself, so ask for it.

        The shipped default is ``blocked`` (turn-end pings only the human who
        asked), and TestOwnerFallbackPolicy below covers that side.
        """
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "all")

    @pytest.mark.asyncio
    async def test_arriving_at_waiting_pings_the_owner(self) -> None:
        dashboard, _channel = _make_dashboard(owner_id=42)
        thread = _make_thread()

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        thread.send.assert_not_called()  # working is not a reason to ping

        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_awaited_once()
        assert "<@42>" in thread.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_one_state_change_both_repaints_the_board_and_pings(self) -> None:
        """With the board up, the turn's end shows on the board *and* reaches the person."""
        dashboard, channel = _make_dashboard(owner_id=42)
        await dashboard.initialize()
        board = channel.send.return_value
        thread = _make_thread()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        board.edit.reset_mock()

        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        board.edit.assert_awaited_once()
        (row,) = _board_embed(board).fields
        assert row.name == "🟡 <#10>"
        thread.send.assert_awaited_once()
        assert "<@42>" in thread.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_mention_trails_the_text_not_leads(self) -> None:
        """#495: the @mention must trail the descriptive text.

        A Discord push preview renders the message content in order, so leading
        with ``<@id>`` shows "@you …" first. Putting the mention at the END makes
        the preview lead with "Claude has finished — your reply is needed here",
        which is the useful part. The mention still pings anywhere in content.
        """
        dashboard, _channel = _make_dashboard(owner_id=42)
        thread = _make_thread()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)

        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_awaited_once()
        sent_text = thread.send.await_args.args[0]
        mention = "<@42>"
        assert mention in sent_text, "mention must still be present so it pings"
        # The descriptive text must come BEFORE the mention.
        assert sent_text.index("Claude has finished") < sent_text.index(mention), (
            f"mention should trail the descriptive text; got: {sent_text!r}"
        )
        # And the content must not lead with the mention.
        assert not sent_text.lstrip().startswith(mention), (
            f"content must not lead with the mention; got: {sent_text!r}"
        )

    @pytest.mark.asyncio
    async def test_mentions_notify_user_over_owner(self) -> None:
        """#481: the completion mention targets the turn's poster, not a fixed owner."""
        dashboard, _channel = _make_dashboard(owner_id=42)
        thread = _make_thread()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)

        await dashboard.set_state(
            10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=999
        )

        sent_text = thread.send.await_args.args[0]
        assert "<@999>" in sent_text, f"should mention the poster (999); got: {sent_text!r}"
        assert "<@42>" not in sent_text, (
            f"should NOT mention the fixed owner (42); got: {sent_text!r}"
        )

    @pytest.mark.asyncio
    async def test_mentions_author_even_when_owner_unset(self) -> None:
        """#481: with no owner configured (e.g. another guild), the poster is still pinged."""
        dashboard, _channel = _make_dashboard(owner_id=None)
        thread = _make_thread()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)

        await dashboard.set_state(
            10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=999
        )

        thread.send.assert_awaited_once()
        sent_text = thread.send.await_args.args[0]
        assert "<@999>" in sent_text

    @pytest.mark.asyncio
    async def test_falls_back_to_owner_when_no_notify_user(self) -> None:
        """Backward compat: without notify_user_id, the owner is still mentioned."""
        dashboard, _channel = _make_dashboard(owner_id=42)
        thread = _make_thread()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)

        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_awaited_once()
        sent_text = thread.send.await_args.args[0]
        assert "<@42>" in sent_text

    @pytest.mark.asyncio
    async def test_repeating_waiting_does_not_ping_twice(self) -> None:
        """One ping per arrival at 🟡: repeating the state is not a new arrival."""
        dashboard, _channel = _make_dashboard(owner_id=42)
        thread = _make_thread()

        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_next_turn_pings_again(self) -> None:
        dashboard, _channel = _make_dashboard(owner_id=42)
        thread = _make_thread()

        for _turn in range(2):
            await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
            await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        assert thread.send.await_count == 2

    @pytest.mark.asyncio
    async def test_nobody_to_name_means_no_message(self) -> None:
        """Nobody to name — no owner, no poster — means no message at all."""
        dashboard, _channel = _make_dashboard(owner_id=None)
        thread = _make_thread()

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_thread_means_nowhere_to_ping(self) -> None:
        """Without the thread there is nowhere to post; the state still moves."""
        dashboard, channel = _make_dashboard(owner_id=42)

        await dashboard.set_state(10, ThreadState.PROCESSING, "w")
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", notify_user_id=999)

        assert dashboard._threads[10].state is ThreadState.WAITING_INPUT
        channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refused_ping_does_not_undo_the_state(self) -> None:
        """A ping Discord refuses must not undo the state change or raise."""
        dashboard, _channel = _make_dashboard(owner_id=42)
        thread = _make_thread()
        thread.send.side_effect = discord.HTTPException(MagicMock(), "Cannot send messages")

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_awaited_once()
        assert dashboard._threads[10].state is ThreadState.WAITING_INPUT

    @pytest.mark.asyncio
    async def test_a_turn_the_user_replaced_summons_nobody(self) -> None:
        """#583: they just typed the next message — they are already here."""
        dashboard, _channel = _make_dashboard(owner_id=42)
        thread = _make_thread()

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(
            10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=999, preempted=True
        )

        thread.send.assert_not_called()
        assert dashboard._threads[10].state is ThreadState.WAITING_INPUT


class TestTurnEndWording:
    """The ping names the real outcome, and always ends with the mention (#495)."""

    @staticmethod
    async def _ping(**outcome: object) -> str:
        dashboard, _channel = _make_dashboard()
        thread = _make_thread()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(
            10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=7, **outcome
        )
        return thread.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_finished(self) -> None:
        text = await self._ping()
        assert text == "🟡 Claude has finished — your reply is needed here. <@7>"

    @pytest.mark.asyncio
    async def test_logged_out_points_at_login_on_the_host(self) -> None:
        text = await self._ping(login_required=True)
        assert "`/login`" in text
        assert "Claude has finished" not in text
        assert text.endswith("<@7>")

    @pytest.mark.asyncio
    async def test_unusable_model_names_the_model(self) -> None:
        text = await self._ping(model_unavailable="claude-nope-9")
        assert "`claude-nope-9`" in text
        assert "`/model`" in text
        assert text.endswith("<@7>")

    @pytest.mark.asyncio
    async def test_api_error_carries_the_advice(self) -> None:
        text = await self._ping(api_error="しばらく待ってから送り直してください。")
        assert text.startswith("❌ Claude Code が API エラーで止まりました。")
        assert "しばらく待ってから送り直してください。" in text
        assert text.endswith("<@7>")

    @pytest.mark.asyncio
    async def test_the_limit_outranks_the_generic_no_response(self) -> None:
        from c_lord.claude.types import UsageLimit

        text = await self._ping(no_response=True, usage_limit=UsageLimit("weekly limit", None, ""))
        assert "weekly limit" in text
        assert "回復時刻は報告されませんでした" in text
        assert "応答がありません" not in text


class TestAlreadyPinged:
    """#915: the ❌ embed of a known failure already pinged — do not ping twice."""

    @staticmethod
    async def _turn_end(**outcome: object) -> MagicMock:
        dashboard, _channel = _make_dashboard()
        thread = _make_thread()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(
            10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=7, **outcome
        )
        return thread

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cause",
        [{"login_required": True}, {"model_unavailable": "m"}, {"api_error": "advice"}],
        ids=["login", "model", "api-error"],
    )
    async def test_known_cause_already_announced_to_the_same_user_is_skipped(
        self, cause: dict[str, object]
    ) -> None:
        thread = await self._turn_end(already_pinged=7, **cause)
        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_different_user_is_still_told(self) -> None:
        thread = await self._turn_end(already_pinged=8, login_required=True)
        thread.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_ordinary_finish_is_never_skipped(self) -> None:
        thread = await self._turn_end(already_pinged=7)
        thread.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# remove()
# ---------------------------------------------------------------------------


class TestTakingAThreadOff:
    @pytest.mark.asyncio
    async def test_removed_thread_leaves_the_table_and_the_board(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w")
        await dashboard.set_state(20, ThreadState.PROCESSING, "w")

        await dashboard.remove(10)

        assert set(dashboard._threads) == {20}
        assert [f.name for f in _board_embed(channel.send.return_value).fields] == ["🟢 <#20>"]

    @pytest.mark.asyncio
    async def test_removing_an_unknown_thread_changes_nothing(self) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w")

        await dashboard.remove(404)  # must not raise

        assert set(dashboard._threads) == {10}


# ---------------------------------------------------------------------------
# What the board looks like
# ---------------------------------------------------------------------------


class TestBoardLayout:
    """The embed for the state table: one field per thread, colour by urgency."""

    def test_empty_table_says_no_active_sessions(self) -> None:
        dashboard, _channel = _make_dashboard()
        embed = dashboard._build_embed()
        assert embed.title == DASHBOARD_TITLE
        assert embed.description == "No active sessions."
        assert embed.fields == []

    @pytest.mark.asyncio
    async def test_row_links_the_thread_and_shows_its_description(self) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.set_state(123456, ThreadState.PROCESSING, "write the docs")

        (row,) = dashboard._build_embed().fields
        assert row.name == "🟢 <#123456>"
        assert row.value.startswith("**Auto-processing** · ")
        assert row.value.endswith("\nwrite the docs")
        assert row.inline is False

    @pytest.mark.asyncio
    async def test_waiting_row_is_labelled_as_waiting(self) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.set_state(5, ThreadState.WAITING_INPUT, "review")

        (row,) = dashboard._build_embed().fields
        assert row.name == "🟡 <#5>"
        assert row.value.startswith("**Waiting for input** · ")

    @pytest.mark.asyncio
    async def test_one_waiting_thread_turns_the_board_yellow(self) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.set_state(1, ThreadState.PROCESSING, "a")
        await dashboard.set_state(2, ThreadState.WAITING_INPUT, "b")
        await dashboard.set_state(3, ThreadState.PROCESSING, "c")

        assert dashboard._build_embed().color.value == 0xFEE75C

    @pytest.mark.asyncio
    async def test_all_working_keeps_the_board_blurple(self) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.set_state(1, ThreadState.PROCESSING, "a")
        await dashboard.set_state(2, ThreadState.PROCESSING, "b")

        assert dashboard._build_embed().color.value == 0x5865F2

    def test_empty_board_is_grey(self) -> None:
        """Nothing listed is neither urgent nor busy — its own, quieter colour."""
        dashboard, _channel = _make_dashboard()
        assert dashboard._build_embed().color.value == 0x99AAB5

    @pytest.mark.asyncio
    async def test_rows_are_listed_in_the_order_the_threads_appeared(self) -> None:
        """A row keeps its place when its state changes — the board does not reshuffle."""
        dashboard, _channel = _make_dashboard()
        for thread_id in (30, 10, 20):
            await dashboard.set_state(thread_id, ThreadState.PROCESSING, "w")
        # Spread the start times so the order does not rest on clock resolution.
        for seconds_ago, thread_id in ((300, 30), (200, 10), (100, 20)):
            dashboard._threads[thread_id].started_at = time.monotonic() - seconds_ago

        await dashboard.set_state(30, ThreadState.WAITING_INPUT, "w")
        await dashboard.set_state(10, ThreadState.PROCESSING, "w")

        names = [f.name for f in dashboard._build_embed().fields]
        assert names == ["🟡 <#30>", "🟢 <#10>", "🟢 <#20>"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("length", "shown"),
        [(59, "d" * 59), (60, "d" * 60), (61, "d" * 60 + "…"), (500, "d" * 60 + "…")],
    )
    async def test_description_is_cut_at_60_characters(self, length: int, shown: str) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.set_state(1, ThreadState.PROCESSING, "d" * length)

        (row,) = dashboard._build_embed().fields
        assert row.value.split("\n", 1)[1] == shown

    @pytest.mark.asyncio
    async def test_row_says_how_long_ago_its_state_changed(self) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.set_state(1, ThreadState.PROCESSING, "w")
        _age(dashboard, 1, 125.5)

        (row,) = dashboard._build_embed().fields
        assert "· 2m 5s ago\n" in row.value

    @pytest.mark.asyncio
    async def test_footer_states_the_four_hour_rule(self) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.set_state(1, ThreadState.PROCESSING, "w")
        assert "4h" in dashboard._build_embed().footer.text


class TestElapsedText:
    """Seconds below a minute, then minutes and seconds — never hours."""

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [
            (0, "0s"),
            (0.9, "0s"),
            (59, "59s"),
            (59.99, "59s"),
            (60, "1m 0s"),
            (61, "1m 1s"),
            (3599, "59m 59s"),
            (3600, "60m 0s"),
            (7503, "125m 3s"),
        ],
    )
    def test_boundaries(self, seconds: float, text: str) -> None:
        assert _elapsed_text(seconds) == text


# ---------------------------------------------------------------------------
# Stale rows
# ---------------------------------------------------------------------------


class TestStaleRows:
    """A row untouched for four hours drops off; a younger one stays."""

    @pytest.mark.asyncio
    async def test_row_idle_past_four_hours_drops_off_on_the_next_repaint(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "forgotten")
        _age(dashboard, 10, _FOUR_HOURS + 60)

        await dashboard.set_state(20, ThreadState.PROCESSING, "fresh")

        assert set(dashboard._threads) == {20}
        assert [f.name for f in _board_embed(channel.send.return_value).fields] == ["🟢 <#20>"]

    @pytest.mark.asyncio
    async def test_row_idle_just_under_four_hours_stays(self) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.initialize()
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "still relevant")
        _age(dashboard, 10, _FOUR_HOURS - 60)

        await dashboard.set_state(20, ThreadState.PROCESSING, "fresh")

        assert set(dashboard._threads) == {10, 20}

    @pytest.mark.asyncio
    async def test_rows_go_stale_without_a_board_too(self) -> None:
        dashboard, _channel = _make_dashboard()
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "forgotten")
        _age(dashboard, 10, _FOUR_HOURS + 60)

        await dashboard.set_state(20, ThreadState.PROCESSING, "fresh")

        assert set(dashboard._threads) == {20}

    @pytest.mark.asyncio
    async def test_the_periodic_prune_repaints_only_when_a_row_went(self) -> None:
        """#754: the timer tick costs no Discord call on a board with nothing to drop."""
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        board = channel.send.return_value
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w")
        board.edit.reset_mock()

        await dashboard.prune_stale_rows()
        board.edit.assert_not_called()

        _age(dashboard, 10, _FOUR_HOURS + 60)
        await dashboard.prune_stale_rows()
        board.edit.assert_awaited_once()
        assert _board_embed(board).description == "No active sessions."

    @pytest.mark.asyncio
    async def test_initialize_starts_one_prune_timer_and_aclose_stops_it(self) -> None:
        dashboard, _channel = _make_dashboard()

        await dashboard.initialize()
        timer = dashboard._prune_task
        await dashboard.initialize()  # a reconnect must not start a second one
        assert dashboard._prune_task is timer
        assert timer is not None and not timer.done()

        await dashboard.aclose()
        assert timer.cancelled()
        await dashboard.aclose()  # closing twice is fine


# ---------------------------------------------------------------------------
# Turns nobody posted for (#754) and the guarded update (#632)
# ---------------------------------------------------------------------------


class TestBoardTurn:
    """``board_turn`` shows 🟢 for the duration of a block and 🟡 after, pinging nobody."""

    @pytest.mark.asyncio
    async def test_row_is_processing_inside_and_waiting_after(self) -> None:
        dashboard, _channel = _make_dashboard(owner_id=42)

        async with board_turn(dashboard, 10, "nightly sweep"):
            assert dashboard._threads[10].state is ThreadState.PROCESSING

        assert dashboard._threads[10].state is ThreadState.WAITING_INPUT
        assert dashboard._threads[10].description == "nightly sweep"

    @pytest.mark.asyncio
    async def test_row_turns_waiting_even_when_the_block_raises(self) -> None:
        dashboard, _channel = _make_dashboard()

        with pytest.raises(RuntimeError):
            async with board_turn(dashboard, 10, "doomed"):
                raise RuntimeError("the run failed")

        assert dashboard._threads[10].state is ThreadState.WAITING_INPUT

    @pytest.mark.asyncio
    async def test_label_is_flattened_and_cut_to_100_characters(self) -> None:
        dashboard, _channel = _make_dashboard()

        async with board_turn(dashboard, 10, "line one\nline two " + "x" * 200):
            description = dashboard._threads[10].description

        assert "\n" not in description
        assert description.startswith("line one line two ")
        assert len(description) == 100

    @pytest.mark.asyncio
    async def test_no_dashboard_is_fine(self, caplog: pytest.LogCaptureFixture) -> None:
        """No board is the ordinary case, not a failure — nothing is logged about it."""
        runs = 0
        with caplog.at_level(logging.DEBUG, logger="c_lord.discord_ui.thread_dashboard"):
            async with board_turn(None, 10, "x"):
                runs += 1

        assert runs == 1
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


class TestSafeSetState:
    """#632: a dashboard failure is logged, never raised into the turn."""

    @pytest.mark.asyncio
    async def test_failure_is_swallowed_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        dashboard, _channel = _make_dashboard()
        dashboard.set_state = AsyncMock(side_effect=RuntimeError("Session is closed"))  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.thread_dashboard"):
            await safe_set_state(dashboard, 10, ThreadState.PROCESSING, "w")

        assert any("thread=10" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_arguments_are_passed_through(self) -> None:
        dashboard, _channel = _make_dashboard()
        thread = _make_thread()

        await safe_set_state(
            dashboard, 10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=7
        )

        assert dashboard._threads[10].description == "w"
        thread.send.assert_awaited_once()


class TestDashboardOf:
    def test_returns_the_bots_dashboard(self) -> None:
        dashboard, _channel = _make_dashboard()
        bot = MagicMock()
        bot.thread_dashboard = dashboard
        assert dashboard_of(bot) is dashboard

    def test_anything_else_is_no_dashboard(self) -> None:
        assert dashboard_of(MagicMock()) is None
        assert dashboard_of(object()) is None


class TestOwnerFallbackPolicy:
    """#525: CLORD_OWNER_FALLBACK decides whether a turn nobody asked for pings."""

    @pytest.mark.asyncio
    async def test_default_does_not_ping_the_owner_on_turn_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default ``blocked``: a turn no human asked for finishes silently."""
        monkeypatch.delenv("CLORD_OWNER_FALLBACK", raising=False)
        dashboard, _channel = _make_dashboard(owner_id=42)
        thread = _make_thread()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)

        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_off_does_not_ping_the_owner_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "off")
        dashboard, _channel = _make_dashboard(owner_id=42)
        thread = _make_thread()
        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)

        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_real_poster_is_mentioned_in_every_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The policy governs the fallback only — never a human requester."""
        for mode in ("all", "blocked", "off"):
            monkeypatch.setenv("CLORD_OWNER_FALLBACK", mode)
            dashboard, _channel = _make_dashboard(owner_id=42)
            thread = _make_thread()
            await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)

            await dashboard.set_state(
                10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=999
            )

            sent_text = thread.send.await_args.args[0]
            assert "<@999>" in sent_text, f"mode={mode} must still mention the poster"


class TestNoResponseMention:
    """#562 AC2/AC3: the turn-end ping must say what actually happened."""

    @staticmethod
    def _dash(thread):
        from c_lord.discord_ui.thread_dashboard import ThreadStatusDashboard

        d = ThreadStatusDashboard(MagicMock(), owner_id=999)
        d._refresh_dashboard = AsyncMock()  # type: ignore[method-assign]
        return d

    @pytest.mark.asyncio
    async def test_answered_turn_still_says_finished(self) -> None:
        """AC5: the normal wording is untouched."""
        from c_lord.discord_ui.thread_dashboard import ThreadState

        thread = MagicMock()
        thread.send = AsyncMock()
        d = self._dash(thread)

        await d.set_state(1, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=7)
        await d.set_state(1, ThreadState.WAITING_INPUT, "x", thread=thread, notify_user_id=7)

        thread.send.assert_awaited_once()
        assert "Claude has finished" in thread.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_turn_with_no_response_says_so_instead(self) -> None:
        """AC2/AC3: no answer → do not claim the work finished."""
        from c_lord.discord_ui.thread_dashboard import ThreadState

        thread = MagicMock()
        thread.send = AsyncMock()
        d = self._dash(thread)

        await d.set_state(1, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=7)
        await d.set_state(
            1,
            ThreadState.WAITING_INPUT,
            "x",
            thread=thread,
            notify_user_id=7,
            no_response=True,
        )

        thread.send.assert_awaited_once()
        body = thread.send.await_args.args[0]
        assert "Claude has finished" not in body, body
        assert "応答がありません" in body, body
        assert "<@7>" in body, "the waiting user must still be told"


class TestUsageLimitMention:
    """#631 AC1/AC2: a rate-limited turn reports the limit, not "send it again"."""

    @staticmethod
    def _dash():
        from c_lord.discord_ui.thread_dashboard import ThreadStatusDashboard

        d = ThreadStatusDashboard(MagicMock(), owner_id=999)
        d._refresh_dashboard = AsyncMock()  # type: ignore[method-assign]
        return d

    @pytest.mark.asyncio
    async def test_usage_limit_ping_names_the_reset_time(self) -> None:
        """AC1: the ping carries the recovery time so the reader can just wait."""
        from c_lord.claude.types import UsageLimit
        from c_lord.discord_ui.thread_dashboard import ThreadState

        thread = MagicMock()
        thread.send = AsyncMock()
        d = self._dash()

        await d.set_state(1, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=7)
        await d.set_state(
            1,
            ThreadState.WAITING_INPUT,
            "x",
            thread=thread,
            notify_user_id=7,
            no_response=True,
            usage_limit=UsageLimit("weekly limit", "Aug 29, 4pm (Asia/Tokyo)", ""),
        )

        thread.send.assert_awaited_once()
        body = thread.send.await_args.args[0]
        assert "Aug 29, 4pm (Asia/Tokyo)" in body, body
        assert "上限" in body, body
        # AC2: sending it again cannot work until the limit resets.
        assert "もう一度送る" not in body, body
        assert "Claude has finished" not in body, body
        assert "<@7>" in body

    @pytest.mark.asyncio
    async def test_usage_limit_without_reset_time_still_says_limit(self) -> None:
        """``resetsAt`` can be absent; the ping must not invent one."""
        from c_lord.claude.types import UsageLimit
        from c_lord.discord_ui.thread_dashboard import ThreadState

        thread = MagicMock()
        thread.send = AsyncMock()
        d = self._dash()

        await d.set_state(1, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=7)
        await d.set_state(
            1,
            ThreadState.WAITING_INPUT,
            "x",
            thread=thread,
            notify_user_id=7,
            no_response=True,
            usage_limit=UsageLimit("session limit", None, ""),
        )

        body = thread.send.await_args.args[0]
        assert "上限" in body, body
        assert "もう一度送る" not in body, body
