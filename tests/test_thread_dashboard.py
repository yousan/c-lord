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
    _STALE_HOURS,
    ThreadState,
    ThreadStatusDashboard,
    _ThreadInfo,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


#: The bot's own Discord user id in these tests.
_BOT_ID = 4242


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


def _make_thread(thread_id: int = 111) -> MagicMock:
    """Return a mocked discord.Thread."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.send = AsyncMock()
    return thread


def _make_dashboard(
    owner_id: int | None = None,
    history: list[MagicMock] | None = None,
    pins: list[MagicMock] | None = None,
) -> tuple[ThreadStatusDashboard, MagicMock]:
    """Return a (dashboard, channel) pair ready for testing."""
    channel = _make_channel(history=history, pins=pins)
    dashboard = ThreadStatusDashboard(channel=channel, owner_id=owner_id, bot_user_id=_BOT_ID)
    return dashboard, channel


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_posts_embed(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        channel.send.assert_called_once()
        # Embed should be passed as keyword argument
        call_kwargs = channel.send.call_args.kwargs
        assert "embed" in call_kwargs

    @pytest.mark.asyncio
    async def test_initialize_attempts_pin(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        msg = channel.send.return_value
        msg.pin.assert_called_once()

    @pytest.mark.asyncio
    async def test_initialize_survives_pin_failure(self) -> None:
        """A failed pin (no permission) must not crash the dashboard."""
        dashboard, channel = _make_dashboard()
        msg = channel.send.return_value
        msg.pin.side_effect = discord.HTTPException(MagicMock(), "Missing Permissions")
        # Should not raise
        await dashboard.initialize()


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


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


class TestSetState:
    @pytest.mark.asyncio
    async def test_set_state_processing_adds_thread(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        thread = _make_thread(111)

        await dashboard.set_state(111, ThreadState.PROCESSING, "doing stuff", thread=thread)

        assert 111 in dashboard._threads
        assert dashboard._threads[111].state == ThreadState.PROCESSING

    @pytest.mark.asyncio
    async def test_set_state_updates_dashboard_embed(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()
        msg = channel.send.return_value

        await dashboard.set_state(222, ThreadState.PROCESSING, "work", thread=_make_thread(222))

        # Edit should have been called once after the state change
        msg.edit.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_state_without_initialize_does_not_crash(self) -> None:
        """set_state before initialize() skips dashboard edit (no message to edit)."""
        dashboard, _ = _make_dashboard()
        # No initialize() called — _dashboard_message is None
        # Should not raise
        await dashboard.set_state(1, ThreadState.PROCESSING, "test")

    @pytest.mark.asyncio
    async def test_multiple_state_updates_accumulate(self) -> None:
        dashboard, channel = _make_dashboard()
        await dashboard.initialize()

        await dashboard.set_state(1, ThreadState.PROCESSING, "task 1", thread=_make_thread(1))
        await dashboard.set_state(2, ThreadState.PROCESSING, "task 2", thread=_make_thread(2))

        assert len(dashboard._threads) == 2

    @pytest.mark.asyncio
    async def test_update_existing_thread_state(self) -> None:
        dashboard, _ = _make_dashboard()
        await dashboard.initialize()
        thread = _make_thread(5)

        await dashboard.set_state(5, ThreadState.PROCESSING, "start", thread=thread)
        await dashboard.set_state(5, ThreadState.WAITING_INPUT, "start", thread=thread)

        assert dashboard._threads[5].state == ThreadState.WAITING_INPUT


# ---------------------------------------------------------------------------
# Owner mention on WAITING_INPUT
# ---------------------------------------------------------------------------


class TestOwnerMention:
    @pytest.fixture(autouse=True)
    def _owner_fallback_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#525: these cover the owner *fallback* itself, so ask for it.

        The shipped default is ``blocked`` (turn-end pings only the human who
        asked), and TestOwnerFallbackPolicy below covers that side.
        """
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "all")

    @pytest.mark.asyncio
    async def test_mention_sent_on_waiting_input_transition(self) -> None:
        dashboard, channel = _make_dashboard(owner_id=42)
        await dashboard.initialize()
        thread = _make_thread(10)

        # First transition: PROCESSING → WAITING_INPUT
        await dashboard.set_state(10, ThreadState.PROCESSING, "working", thread=thread)
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "working", thread=thread)

        thread.send.assert_called_once()
        sent_text = thread.send.call_args.args[0]
        assert "<@42>" in sent_text

    @pytest.mark.asyncio
    async def test_mention_trails_the_text_not_leads(self) -> None:
        """#495: the @mention must trail the descriptive text.

        A Discord push preview renders the message content in order, so leading
        with ``<@id>`` shows "@you …" first. Putting the mention at the END makes
        the preview lead with "Claude has finished — your reply is needed here",
        which is the useful part. The mention still pings anywhere in content.
        """
        dashboard, _ = _make_dashboard(owner_id=42)
        await dashboard.initialize()
        thread = _make_thread(10)

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        sent_text = thread.send.call_args.args[0]
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
        dashboard, _ = _make_dashboard(owner_id=42)
        await dashboard.initialize()
        thread = _make_thread(10)

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(
            10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=999
        )

        sent_text = thread.send.call_args.args[0]
        assert "<@999>" in sent_text, f"should mention the poster (999); got: {sent_text!r}"
        assert "<@42>" not in sent_text, (
            f"should NOT mention the fixed owner (42); got: {sent_text!r}"
        )

    @pytest.mark.asyncio
    async def test_mentions_author_even_when_owner_unset(self) -> None:
        """#481: with no owner configured (e.g. another guild), the poster is still pinged."""
        dashboard, _ = _make_dashboard(owner_id=None)
        await dashboard.initialize()
        thread = _make_thread(10)

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(
            10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=999
        )

        thread.send.assert_called_once()
        sent_text = thread.send.call_args.args[0]
        assert "<@999>" in sent_text

    @pytest.mark.asyncio
    async def test_falls_back_to_owner_when_no_notify_user(self) -> None:
        """Backward compat: without notify_user_id, the owner is still mentioned."""
        dashboard, _ = _make_dashboard(owner_id=42)
        await dashboard.initialize()
        thread = _make_thread(10)

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        sent_text = thread.send.call_args.args[0]
        assert "<@42>" in sent_text

    @pytest.mark.asyncio
    async def test_mention_not_sent_if_already_waiting(self) -> None:
        """Repeated WAITING_INPUT transitions should NOT spam mentions."""
        dashboard, _ = _make_dashboard(owner_id=42)
        await dashboard.initialize()
        thread = _make_thread(10)

        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)
        thread.send.reset_mock()

        # Second WAITING_INPUT — should not mention again
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_mention_when_owner_id_not_set(self) -> None:
        dashboard, _ = _make_dashboard(owner_id=None)
        await dashboard.initialize()
        thread = _make_thread(10)

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_mention_when_thread_not_provided(self) -> None:
        """Without a thread object, the mention is silently skipped."""
        dashboard, _ = _make_dashboard(owner_id=42)
        await dashboard.initialize()

        # No thread= argument — should not crash
        await dashboard.set_state(10, ThreadState.PROCESSING, "w")
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w")

    @pytest.mark.asyncio
    async def test_mention_survives_http_error(self) -> None:
        """A failed HTTP call for the mention must not crash the dashboard."""
        dashboard, _ = _make_dashboard(owner_id=42)
        await dashboard.initialize()
        thread = _make_thread(10)
        thread.send.side_effect = discord.HTTPException(MagicMock(), "error")

        # Should not raise
        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------


class TestRemove:
    @pytest.mark.asyncio
    async def test_remove_existing_thread(self) -> None:
        dashboard, _ = _make_dashboard()
        await dashboard.initialize()
        await dashboard.set_state(77, ThreadState.PROCESSING, "task")

        await dashboard.remove(77)

        assert 77 not in dashboard._threads

    @pytest.mark.asyncio
    async def test_remove_nonexistent_is_noop(self) -> None:
        dashboard, _ = _make_dashboard()
        await dashboard.initialize()
        # Should not raise
        await dashboard.remove(9999)


# ---------------------------------------------------------------------------
# Embed building
# ---------------------------------------------------------------------------


class TestBuildEmbed:
    def test_empty_embed_shows_no_active_sessions(self) -> None:
        dashboard, _ = _make_dashboard()
        embed = dashboard._build_embed()
        assert embed.description is not None
        assert "No active sessions" in embed.description

    def test_embed_shows_thread_mention(self) -> None:
        dashboard, _ = _make_dashboard()
        dashboard._threads[123] = _thread_info(123, ThreadState.PROCESSING, "doing stuff")
        embed = dashboard._build_embed()
        field_names = [f.name for f in embed.fields]
        assert any("<#123>" in name for name in field_names)

    def test_embed_yellow_when_any_waiting(self) -> None:
        dashboard, _ = _make_dashboard()
        dashboard._threads[1] = _thread_info(1, ThreadState.PROCESSING, "p")
        dashboard._threads[2] = _thread_info(2, ThreadState.WAITING_INPUT, "w")
        embed = dashboard._build_embed()
        assert embed.color.value == 0xFEE75C  # Yellow

    def test_embed_blurple_when_all_processing(self) -> None:
        dashboard, _ = _make_dashboard()
        dashboard._threads[1] = _thread_info(1, ThreadState.PROCESSING, "p")
        embed = dashboard._build_embed()
        assert embed.color.value == 0x5865F2  # Blurple


# ---------------------------------------------------------------------------
# Stale entry pruning
# ---------------------------------------------------------------------------


class TestStalePruning:
    def test_stale_entries_pruned_on_refresh(self) -> None:
        dashboard, _ = _make_dashboard()
        info = _thread_info(55, ThreadState.WAITING_INPUT, "old")
        # Make the state_changed_at very old
        info.state_changed_at = time.monotonic() - (_STALE_HOURS * 3600 + 1)
        dashboard._threads[55] = info

        dashboard._prune_stale()

        assert 55 not in dashboard._threads

    def test_recent_entries_not_pruned(self) -> None:
        dashboard, _ = _make_dashboard()
        info = _thread_info(56, ThreadState.PROCESSING, "fresh")
        dashboard._threads[56] = info

        dashboard._prune_stale()

        assert 56 in dashboard._threads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _thread_info(
    thread_id: int,
    state: ThreadState,
    description: str,
) -> _ThreadInfo:
    return _ThreadInfo(thread_id=thread_id, description=description, state=state)


class TestOwnerFallbackPolicy:
    """#525: CLORD_OWNER_FALLBACK decides whether a turn nobody asked for pings."""

    @pytest.mark.asyncio
    async def test_default_does_not_ping_the_owner_on_turn_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default ``blocked``: a turn no human asked for finishes silently."""
        monkeypatch.delenv("CLORD_OWNER_FALLBACK", raising=False)
        dashboard, _ = _make_dashboard(owner_id=42)
        await dashboard.initialize()
        thread = _make_thread(10)

        await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
        await dashboard.set_state(10, ThreadState.WAITING_INPUT, "w", thread=thread)

        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_off_does_not_ping_the_owner_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "off")
        dashboard, _ = _make_dashboard(owner_id=42)
        await dashboard.initialize()
        thread = _make_thread(10)

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
            dashboard, _ = _make_dashboard(owner_id=42)
            await dashboard.initialize()
            thread = _make_thread(10)

            await dashboard.set_state(10, ThreadState.PROCESSING, "w", thread=thread)
            await dashboard.set_state(
                10, ThreadState.WAITING_INPUT, "w", thread=thread, notify_user_id=999
            )

            sent_text = thread.send.call_args.args[0]
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
