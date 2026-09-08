"""捨てたことが通常運用のログに残るか — #678.

#556 は正しい: 監視 webhook が `sessions` 行の無いスレッドに流し込むたびに ⚠️ と
案内文を返したら、インシデント中の唯一読めるスレッドが埋まる。だから Discord には
**何も返さない**。変えるのはそこではなく、**ログの見え方だけ**（`no-user-visible-change`）。

2026-09-02、#617 の切り分けでこの穴に落ちた。webhook で probe を投げ、Discord も無反応・
INFO ログも 1 行も無し。「bot が落ちたのか / webhook が壊れたのか / スレッドが対象外なのか」を
区別できず、コードを読むまで原因に辿り着けなかった:

    $ grep 1517285514368122881 /tmp/clord-bot-c-lord.log
    （1行も出ない）

ここで固定するのは Issue が選んだ案 (b): **INFO に出すが、同じスレッドについては一定時間に
1 回だけ**（以降は DEBUG）。1 回きりの切り分けは必ず救われ、chatty な webhook でも
ログは溢れない。
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.log_sampler import LogSampler

CHANNEL_ID = 999
THREAD_ID = 1517285514368122881  # the thread from the 2026-09-02 dead end


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_cog() -> ClaudeChatCog:
    bot = MagicMock()
    bot.channel_id = CHANNEL_ID
    bot.settings_repo = None
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)  # no sessions row — the #538/#556 path
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner)


def _webhook_message(thread_id: int = THREAD_ID):
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = CHANNEL_ID
    thread.send = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.channel = thread
    message.content = "probe"
    message.attachments = []
    message.author = MagicMock()
    message.author.bot = True
    message.webhook_id = 123456789
    message.type = discord.MessageType.default
    message.add_reaction = AsyncMock()
    return message, thread


def _info_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]


class TestTheDropIsVisibleAtInfo:
    async def test_one_line_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """AC1: 通常運用のログレベルで痕跡が残る。RED ではここが 0 行だった。"""
        cog = _make_cog()
        message, thread = _webhook_message()

        with caplog.at_level(logging.INFO, logger="c_lord.cogs.claude_chat"):
            await cog._handle_untracked_thread(message, thread)

        assert len(_info_lines(caplog)) == 1

    async def test_it_is_greppable_by_thread_id(self, caplog: pytest.LogCaptureFixture) -> None:
        """AC3: `grep "thread=<id>"` が効く（log_ctx 経由・CLAUDE.md のログ規約）。"""
        cog = _make_cog()
        message, thread = _webhook_message()

        with caplog.at_level(logging.INFO, logger="c_lord.cogs.claude_chat"):
            await cog._handle_untracked_thread(message, thread)

        assert f"thread={THREAD_ID}" in "\n".join(_info_lines(caplog))

    async def test_it_says_why_it_was_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        """1 行で「webhook だから黙って捨てた」と分かること — それが読めないなら
        2026-09-02 と同じで、結局コードを読むことになる。"""
        cog = _make_cog()
        message, thread = _webhook_message()

        with caplog.at_level(logging.INFO, logger="c_lord.cogs.claude_chat"):
            await cog._handle_untracked_thread(message, thread)

        line = "\n".join(_info_lines(caplog))
        assert "webhook" in line
        assert "no session row" in line


class TestAChattyWebhookDoesNotFloodTheLog:
    async def test_fifty_messages_produce_one_info_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC2: #556 が懸念した chatty webhook の再現。50 通で INFO は 1 行。"""
        cog = _make_cog()
        clock = _Clock()
        cog._untracked_webhook_log = LogSampler(window=600.0, clock=clock)

        with caplog.at_level(logging.INFO, logger="c_lord.cogs.claude_chat"):
            for _ in range(50):
                clock.advance(2.0)  # 100s of alerts — inside one window
                message, thread = _webhook_message()
                await cog._handle_untracked_thread(message, thread)

        assert len(_info_lines(caplog)) == 1

    async def test_the_suppressed_ones_are_still_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """うるさくしないだけで、DEBUG の追跡性は #556 のまま落とさない。"""
        cog = _make_cog()
        cog._untracked_webhook_log = LogSampler(window=600.0, clock=_Clock())

        with caplog.at_level(logging.DEBUG, logger="c_lord.cogs.claude_chat"):
            for _ in range(5):
                message, thread = _webhook_message()
                await cog._handle_untracked_thread(message, thread)

        debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(debug) == 4

    async def test_the_next_window_reports_what_was_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """溢れさせない代わりに、次の 1 行が「その間に何通あったか」を持つ。"""
        cog = _make_cog()
        clock = _Clock()
        cog._untracked_webhook_log = LogSampler(window=600.0, clock=clock)

        with caplog.at_level(logging.INFO, logger="c_lord.cogs.claude_chat"):
            for _ in range(10):
                message, thread = _webhook_message()
                await cog._handle_untracked_thread(message, thread)
            clock.advance(600.0)
            message, thread = _webhook_message()
            await cog._handle_untracked_thread(message, thread)

        assert "(+9 suppressed in the last 600s)" in _info_lines(caplog)[-1]

    async def test_another_thread_is_not_silenced(self, caplog: pytest.LogCaptureFixture) -> None:
        """レート制限はスレッド単位。1 本の chatty スレッドが他の切り分けを潰さない。"""
        cog = _make_cog()
        cog._untracked_webhook_log = LogSampler(window=600.0, clock=_Clock())
        other_thread_id = 1544536646215802980

        with caplog.at_level(logging.INFO, logger="c_lord.cogs.claude_chat"):
            for _ in range(20):
                message, thread = _webhook_message()
                await cog._handle_untracked_thread(message, thread)
            message, thread = _webhook_message(other_thread_id)
            await cog._handle_untracked_thread(message, thread)

        assert f"thread={other_thread_id}" in "\n".join(_info_lines(caplog))


class TestDiscordBehaviourIsUnchanged:
    """AC4: `no-user-visible-change`。#556 の判断（黙って捨てる）は維持する。"""

    async def test_nothing_is_posted_to_the_thread(self) -> None:
        cog = _make_cog()
        message, thread = _webhook_message()

        await cog._handle_untracked_thread(message, thread)

        thread.send.assert_not_awaited()

    async def test_no_reaction_is_added(self) -> None:
        cog = _make_cog()
        message, thread = _webhook_message()

        await cog._handle_untracked_thread(message, thread)

        message.add_reaction.assert_not_awaited()

    async def test_that_holds_for_the_chatty_case_too(self) -> None:
        """INFO を 1 行出す回でも Discord には触らない（ここが混ざると #556 の再発）。"""
        cog = _make_cog()
        posted: list[object] = []

        for _ in range(3):
            message, thread = _webhook_message()
            await cog._handle_untracked_thread(message, thread)
            posted += thread.send.await_args_list + message.add_reaction.await_args_list

        assert posted == []
