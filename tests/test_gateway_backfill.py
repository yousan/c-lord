"""Messages that arrived while the Discord gateway was down are picked up (#745).

On 2026-09-16 the production bot lost its gateway for 6h35m (host DNS). Two
messages posted into c-lord threads in that window never reached ``on_message``
— no reaction, no reply, no log line — and the sender re-posted them word for
word 4h36m later. Nothing on reconnect went back for them.

These tests pin the two halves of the fix:

* the pure bookkeeping in :mod:`c_lord.gateway_backfill` (when was the gateway
  last known to deliver, where to start reading, what to tell the thread), and
* the cog wiring: a reconnect reads the thread's history, runs what the gateway
  never delivered through the ordinary reply path — **once** (AC6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.database.repository import SessionRecord
from c_lord.discord_ui.authorization import Authorizer
from c_lord.gateway_backfill import (
    MARGIN,
    GatewayWatch,
    MissedEntry,
    SeenMessages,
    merge_missed_prompt,
    missed_notice,
)

UTC = timezone.utc  # noqa: UP017 — datetime.UTC is 3.11+, we support 3.10
T0 = datetime(2026, 9, 15, 20, 0, 0, tzinfo=UTC)  # 05:00 JST


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# ── SeenMessages ────────────────────────────────────────────────────────────


class TestSeenMessages:
    def test_add_reports_whether_the_id_is_new(self) -> None:
        seen = SeenMessages()
        assert seen.add(1) is True
        assert seen.add(1) is False
        assert 1 in seen
        assert 2 not in seen

    def test_capacity_evicts_the_oldest_first(self) -> None:
        seen = SeenMessages(capacity=3)
        for i in range(4):
            seen.add(i)
        assert 0 not in seen
        assert {1, 2, 3} <= {i for i in range(4) if i in seen}
        assert len(seen) == 3


# ── GatewayWatch ────────────────────────────────────────────────────────────


class TestGatewayWatch:
    def test_first_connect_is_not_an_outage(self) -> None:
        watch = GatewayWatch()
        assert watch.connected(_at(0)) is None

    def test_disconnect_before_the_first_connect_is_ignored(self) -> None:
        """Failed initial connects: we were never up, so nothing was missed."""
        watch = GatewayWatch()
        assert watch.disconnected(_at(0)) is False
        assert watch.connected(_at(1)) is None

    def test_reconnect_after_a_disconnect_reports_the_outage(self) -> None:
        watch = GatewayWatch()
        watch.connected(_at(0))
        watch.seen(_at(10))
        assert watch.disconnected(_at(16)) is True
        outage = watch.connected(_at(400))
        assert outage is not None
        assert outage.down_at == _at(16)
        assert outage.back_at == _at(400)
        assert outage.last_alive_at == _at(10)

    def test_only_the_first_disconnect_of_a_streak_counts(self) -> None:
        """discord.py dispatches ``disconnect`` on every failed retry (40 in the
        incident). The outage started at the first one, not the last."""
        watch = GatewayWatch()
        watch.connected(_at(0))
        assert watch.disconnected(_at(16)) is True
        assert watch.disconnected(_at(21)) is False
        assert watch.disconnected(_at(300)) is False
        outage = watch.connected(_at(400))
        assert outage is not None
        assert outage.down_at == _at(16)

    def test_outage_is_reported_once(self) -> None:
        watch = GatewayWatch()
        watch.connected(_at(0))
        watch.disconnected(_at(16))
        assert watch.connected(_at(400)) is not None
        assert watch.connected(_at(401)) is None

    def test_reading_starts_a_margin_before_the_last_sign_of_life(self) -> None:
        watch = GatewayWatch()
        watch.connected(_at(0))
        watch.seen(_at(10))
        watch.disconnected(_at(16))
        outage = watch.connected(_at(400))
        assert outage is not None
        assert outage.since == _at(10) - MARGIN

    def test_a_frozen_process_reads_from_before_the_freeze(self) -> None:
        """Host suspend / SIGSTOP: the disconnect is only *noticed* after the
        thaw, long after the gateway stopped delivering. Reading from the
        notice time would skip everything posted during the freeze."""
        watch = GatewayWatch()
        watch.connected(_at(0))
        watch.seen(_at(10))
        watch.disconnected(_at(300))  # noticed only after the thaw
        outage = watch.connected(_at(301))
        assert outage is not None
        assert outage.since == _at(10) - MARGIN

    def test_replayed_messages_do_not_move_the_start_forward(self) -> None:
        """A RESUME replays what was missed before ``on_resumed``; those
        messages were created during the outage and must not narrow it."""
        watch = GatewayWatch()
        watch.connected(_at(0))
        watch.seen(_at(10))
        watch.disconnected(_at(16))
        watch.seen(_at(200))  # replayed after reconnect, before on_resumed
        outage = watch.connected(_at(400))
        assert outage is not None
        assert outage.since == _at(10) - MARGIN

    def test_never_reads_from_before_this_process_came_up(self) -> None:
        """Older messages belonged to the previous process — re-running them
        would be a double execution."""
        watch = GatewayWatch()
        watch.connected(_at(0))
        watch.disconnected(_at(1))
        outage = watch.connected(_at(30))
        assert outage is not None
        assert outage.since == _at(0)


# ── wording ─────────────────────────────────────────────────────────────────


class TestMissedNotice:
    def test_single_message_run(self) -> None:
        text = missed_notice(
            down_at=_at(16),
            back_at=_at(396),
            first_missed_at=_at(146),
            count=1,
            outcome="run",
            tz=timezone(timedelta(hours=9)),
        )
        assert text.startswith("-# 🔌 ")
        assert "05:16〜11:36" in text
        assert "この依頼を受け取れていませんでした" in text
        assert "いまから処理します" in text

    def test_several_messages_run_together(self) -> None:
        text = missed_notice(
            down_at=_at(16),
            back_at=_at(396),
            first_missed_at=_at(146),
            count=3,
            outcome="run",
            tz=timezone(timedelta(hours=9)),
        )
        assert "3 件" in text
        assert "まとめて" in text

    def test_start_is_never_after_the_first_missed_message(self) -> None:
        """A frozen process notices the disconnect late; the line must not say
        the connection was fine when the message was posted."""
        text = missed_notice(
            down_at=_at(300),
            back_at=_at(301),
            first_missed_at=_at(146),
            count=1,
            outcome="run",
            tz=timezone(timedelta(hours=9)),
        )
        assert "07:26〜" in text

    def test_held_message_does_not_promise_to_run(self) -> None:
        text = missed_notice(
            down_at=_at(16),
            back_at=_at(396),
            first_missed_at=_at(146),
            count=1,
            outcome="held",
            tz=timezone(timedelta(hours=9)),
        )
        assert "いまから処理します" not in text
        assert "受け取れていませんでした" in text

    def test_superseded_says_it_did_not_run_and_what_to_do(self) -> None:
        text = missed_notice(
            down_at=_at(16),
            back_at=_at(396),
            first_missed_at=_at(146),
            count=2,
            outcome="superseded",
            tz=timezone(timedelta(hours=9)),
        )
        assert "実行していません" in text
        assert "もう一度" in text

    def test_outage_across_days_shows_the_date(self) -> None:
        text = missed_notice(
            down_at=_at(16),
            back_at=_at(16 + 26 * 60),
            first_missed_at=_at(146),
            count=1,
            outcome="run",
            tz=timezone(timedelta(hours=9)),
        )
        assert "9/16 05:16〜9/17 07:16" in text


class TestMergeMissedPrompt:
    def test_lists_every_message_oldest_first(self) -> None:
        entries = [
            MissedEntry(created_at=_at(146), author="yousan", text="報告どうなってる？"),
            MissedEntry(
                created_at=_at(150),
                author="yousan",
                text="ついでにこれも",
                attachments=("a.png <https://cdn.example/a.png>",),
            ),
            MissedEntry(created_at=_at(160), author="yousan", text="最後の依頼"),
        ]
        prompt = merge_missed_prompt(entries, tz=timezone(timedelta(hours=9)))
        assert prompt.index("報告どうなってる？") < prompt.index("ついでにこれも")
        assert prompt.index("ついでにこれも") < prompt.index("最後の依頼")
        assert "3 件" in prompt
        assert "07:26" in prompt
        assert "a.png <https://cdn.example/a.png>" in prompt


# ── cog wiring ──────────────────────────────────────────────────────────────


def _record(thread_id: int, **overrides: object) -> SessionRecord:
    fields: dict[str, object] = {
        "thread_id": thread_id,
        "session_id": "sess-1",
        "working_dir": None,
        "model": None,
        "origin": "discord",
        "summary": None,
        "created_at": "2026-09-15 10:00:00",
        "last_used_at": "2026-09-15 10:00:00",
    }
    fields.update(overrides)
    return SessionRecord(**fields)  # type: ignore[arg-type]


def _message(
    thread: MagicMock,
    created_at: datetime,
    *,
    content: str = "報告どうなってる？",
    bot_author: bool = False,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = discord.utils.time_snowflake(created_at)
    msg.created_at = created_at
    msg.channel = thread
    msg.type = discord.MessageType.default
    msg.webhook_id = None
    msg.content = content
    msg.attachments = []
    msg.author = MagicMock()
    msg.author.bot = bot_author
    msg.author.id = 42
    msg.author.display_name = "yousan"
    return msg


def _thread(thread_id: int, history: list[MagicMock]) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = 999_000
    thread.send = AsyncMock()

    def _history(**kwargs: object) -> object:
        after = kwargs.get("after")

        async def _gen():  # type: ignore[no-untyped-def]
            for m in history:
                if isinstance(after, datetime) and m.created_at <= after:
                    continue
                yield m

        return _gen()

    thread.history = MagicMock(side_effect=_history)
    thread.last_message_id = history[-1].id if history else None
    return thread


class _Harness:
    """A ClaudeChatCog on a fake bot whose one guild holds *threads*."""

    def __init__(self, threads: list[MagicMock], records: dict[int, SessionRecord]) -> None:
        bot = MagicMock()
        bot.channel_id = 999
        bot.settings_repo = None
        bot.resume_repo = None
        ctx = MagicMock()
        ctx.valid = False
        bot.get_context = AsyncMock(return_value=ctx)
        bot.get_cog = MagicMock(return_value=None)
        guild = MagicMock()
        guild.id = 1
        guild.active_threads = AsyncMock(return_value=threads)
        bot.guilds = [guild]
        repo = MagicMock()
        repo.get = AsyncMock(side_effect=lambda tid: records.get(tid))
        self.cog = ClaudeChatCog(
            bot=bot,
            repo=repo,
            runner=MagicMock(),
            authorizer=Authorizer(allow_anyone=True),
        )
        self.cog._run_startup_recovery = AsyncMock()  # type: ignore[method-assign]
        self.cog._handle_thread_reply = AsyncMock()  # type: ignore[method-assign]
        self.clock = [_at(0)]
        self.cog._gateway = GatewayWatch(clock=lambda: self.clock[0])

    async def connect(self, minutes: float) -> None:
        self.clock[0] = _at(minutes)
        await self.cog.on_ready()
        await self.settle()

    async def resume(self, minutes: float) -> None:
        self.clock[0] = _at(minutes)
        # getattr: before #745 the cog had no such listener, and the RED this
        # file records is "the message never ran", not an AttributeError.
        await getattr(self.cog, "on_resumed", _noop)()
        await self.settle()

    async def disconnect(self, minutes: float) -> None:
        self.clock[0] = _at(minutes)
        await getattr(self.cog, "on_disconnect", _noop)()

    async def settle(self) -> None:
        """Wait for the pick-up, which runs as its own task off ``on_ready``."""
        task = getattr(self.cog, "_backfill_task", None)
        if task is not None:
            await task


async def _noop() -> None:
    return None


class TestReconnectPicksUpMissedMessages:
    @pytest.mark.asyncio
    async def test_message_sent_while_down_is_run_after_ready(self) -> None:
        """AC1/AC4: the incident, in miniature. RED before #745: nothing reads
        the thread on reconnect, so the reply path is never reached."""
        thread = MagicMock()
        missed = _message(thread, _at(146))
        thread = _thread(501, [missed])
        missed.channel = thread
        h = _Harness([thread], {501: _record(501)})

        await h.connect(0)
        await h.disconnect(16)
        await h.connect(396)

        h.cog._handle_thread_reply.assert_awaited_once()
        assert h.cog._handle_thread_reply.await_args.args[0] is missed

    @pytest.mark.asyncio
    async def test_message_sent_while_down_is_run_after_resume(self) -> None:
        thread = _thread(501, [])
        missed = _message(thread, _at(3))
        thread = _thread(501, [missed])
        missed.channel = thread
        h = _Harness([thread], {501: _record(501)})

        await h.connect(0)
        await h.disconnect(2)
        await h.resume(4)

        h.cog._handle_thread_reply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_thread_with_missed_message_gets_one_line_why(self) -> None:
        """AC2: the delay is explained, once, in the thread that had it."""
        quiet = _thread(502, [])
        busy = _thread(501, [])
        missed = _message(busy, _at(146))
        busy = _thread(501, [missed])
        missed.channel = busy
        h = _Harness([busy, quiet], {501: _record(501), 502: _record(502)})

        await h.connect(0)
        await h.disconnect(16)
        await h.connect(396)

        busy.send.assert_awaited_once()
        assert busy.send.await_args.args[0].startswith("-# 🔌 ")
        quiet.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_threads_that_are_not_ours_are_left_alone(self) -> None:
        """A shared guild holds other instances' threads: no row, no action."""
        thread = _thread(777, [])
        missed = _message(thread, _at(146))
        thread = _thread(777, [missed])
        missed.channel = thread
        h = _Harness([thread], {})

        await h.connect(0)
        await h.disconnect(16)
        await h.connect(396)

        h.cog._handle_thread_reply.assert_not_awaited()
        thread.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bot_messages_are_not_picked_up(self) -> None:
        thread = _thread(501, [])
        own = _message(thread, _at(146), bot_author=True)
        thread = _thread(501, [own])
        own.channel = thread
        h = _Harness([thread], {501: _record(501)})

        await h.connect(0)
        await h.disconnect(16)
        await h.connect(396)

        h.cog._handle_thread_reply.assert_not_awaited()
        thread.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_several_missed_messages_run_as_one_turn(self) -> None:
        """Replaying them one by one would make each interrupt the one before
        it (⚡) — the first ones would never reach Claude."""
        thread = _thread(501, [])
        first = _message(thread, _at(146), content="一つ目")
        second = _message(thread, _at(150), content="二つ目")
        thread = _thread(501, [first, second])
        first.channel = second.channel = thread
        h = _Harness([thread], {501: _record(501)})

        await h.connect(0)
        await h.disconnect(16)
        await h.connect(396)

        h.cog._handle_thread_reply.assert_awaited_once()
        call = h.cog._handle_thread_reply.await_args
        assert call.args[0] is second
        assert list(call.kwargs["earlier"]) == [first]

    @pytest.mark.asyncio
    async def test_a_newer_live_message_wins(self) -> None:
        """If the sender already posted again after the reconnect, running the
        old one now would interrupt the new one. Say so instead of running."""
        thread = _thread(501, [])
        old = _message(thread, _at(146), content="古い依頼")
        new = _message(thread, _at(397), content="新しい依頼")
        thread = _thread(501, [old, new])
        old.channel = new.channel = thread
        h = _Harness([thread], {501: _record(501)})

        await h.connect(0)
        await h.disconnect(16)
        h.clock[0] = _at(396)
        await h.cog.on_message(new)  # delivered live right after reconnect
        h.cog._handle_thread_reply.reset_mock()
        await h.cog.on_ready()
        await h.settle()

        h.cog._handle_thread_reply.assert_not_awaited()
        thread.send.assert_awaited_once()
        assert "実行していません" in thread.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_closed_thread_is_answered_not_promised(self) -> None:
        """A /workspace-stop'd thread holds messages (#512): the line must not
        say 「いまから処理します」 in front of the closed notice."""
        thread = _thread(501, [])
        missed = _message(thread, _at(146))
        thread = _thread(501, [missed])
        missed.channel = thread
        closed = _record(501, closed_at="2026-09-15 09:00:00", closed_reason="manual")
        h = _Harness([thread], {501: closed})

        await h.connect(0)
        await h.disconnect(16)
        await h.connect(396)

        h.cog._handle_thread_reply.assert_awaited_once()
        assert "いまから処理します" not in thread.send.await_args.args[0]


class TestNoDoubleExecution:
    """AC6 — every message runs at most once, whichever way it arrived."""

    @pytest.mark.asyncio
    async def test_message_the_gateway_delivered_is_not_picked_up_again(self) -> None:
        """(a) Posted just before the drop and delivered live: the margin reads
        it again, and it must be recognised — only the truly missed one runs,
        and it runs alone (the delivered one is not merged into it)."""
        thread = _thread(501, [])
        live = _message(thread, _at(15), content="届いた依頼")
        missed = _message(thread, _at(146), content="届かなかった依頼")
        thread = _thread(501, [live, missed])
        live.channel = missed.channel = thread
        h = _Harness([thread], {501: _record(501)})

        await h.connect(0)
        h.clock[0] = _at(15)
        await h.cog.on_message(live)
        assert h.cog._handle_thread_reply.await_count == 1
        await h.disconnect(16)
        await h.connect(396)

        assert h.cog._handle_thread_reply.await_count == 2
        call = h.cog._handle_thread_reply.await_args
        assert call.args[0] is missed
        assert not list(call.kwargs.get("earlier", ()))

    @pytest.mark.asyncio
    async def test_message_replayed_by_resume_is_not_picked_up_again(self) -> None:
        """(a) A RESUME replays missed events before ``on_resumed``."""
        thread = _thread(501, [])
        replayed = _message(thread, _at(3))
        thread = _thread(501, [replayed])
        replayed.channel = thread
        h = _Harness([thread], {501: _record(501)})

        await h.connect(0)
        await h.disconnect(2)
        h.clock[0] = _at(4)
        await h.cog.on_message(replayed)
        await h.resume(4)

        assert h.cog._handle_thread_reply.await_count == 1
        thread.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_late_gateway_delivery_after_pickup_does_not_run_twice(self) -> None:
        """(b) Read from history first, then the gateway delivers it too."""
        thread = _thread(501, [])
        missed = _message(thread, _at(146))
        thread = _thread(501, [missed])
        missed.channel = thread
        h = _Harness([thread], {501: _record(501)})

        await h.connect(0)
        await h.disconnect(16)
        await h.connect(396)
        assert h.cog._handle_thread_reply.await_count == 1  # picked up
        await h.cog.on_message(missed)

        assert h.cog._handle_thread_reply.await_count == 1

    @pytest.mark.asyncio
    async def test_repeated_reconnects_do_not_pick_it_up_twice(self) -> None:
        """(c) A second outage reads overlapping history."""
        thread = _thread(501, [])
        missed = _message(thread, _at(146))
        thread = _thread(501, [missed])
        missed.channel = thread
        h = _Harness([thread], {501: _record(501)})

        await h.connect(0)
        await h.disconnect(16)
        await h.connect(396)
        await h.disconnect(397)
        await h.resume(398)

        assert h.cog._handle_thread_reply.await_count == 1
        assert thread.send.await_count == 1


class TestOutageIsLogged:
    @pytest.mark.asyncio
    async def test_start_end_and_count_are_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """AC3: 「いつ何通拾ったか」 is answerable from INFO alone (#678)."""
        import logging

        thread = _thread(501, [])
        missed = _message(thread, _at(146))
        thread = _thread(501, [missed])
        missed.channel = thread
        h = _Harness([thread], {501: _record(501)})

        with caplog.at_level(logging.INFO, logger="c_lord"):
            await h.connect(0)
            await h.disconnect(16)
            await h.connect(396)

        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("gateway disconnected" in m for m in infos)
        summary = [m for m in infos if "Gateway outage" in m and "picked up 1 message(s)" in m]
        assert summary, infos
        # Logged in the host's local time, like every other timestamp in the log.
        assert _at(16).astimezone().strftime("%Y-%m-%d %H:%M") in summary[0]
        assert _at(396).astimezone().strftime("%Y-%m-%d %H:%M") in summary[0]
        assert "thread=501" in "\n".join(infos)
