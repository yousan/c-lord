"""Tests for the 復元可否 verdict shared by the hint and the message path — #538.

c-lord used to tell a stopped session's owner **「このスレッドにメッセージを送れば
自動で復元し、続きから再開します」** without ever checking whether that was true.
The receiving side (``ClaudeChatCog.on_message``) accepted a message only when a
``sessions`` row existed and otherwise dropped it **with no reply and no log** —
so a thread whose row was missing swallowed every message while the bot kept
promising it would resume.

These tests pin the fix: one verdict function (:mod:`c_lord.session_resume`) that
both the hint wording and the acceptance check go through, a log line + a reply
for the dropped case, and no second copy of the wording anywhere in the tree.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.database.repository import SessionRecord
from c_lord.session_resume import (
    UNTRACKED_NOTICE,
    UNTRACKED_REACTION,
    ThreadResume,
    accepts_message,
    classify,
    hint_for_thread,
    stopped_hint,
)

_SRC = Path(__file__).resolve().parents[1] / "c_lord"


def _said(mock: AsyncMock) -> str:
    """The first positional argument of ``mock``'s last await."""
    assert mock.await_args is not None, "nothing was sent"
    return str(mock.await_args.args[0])


def _record(*, closed_at: str | None = None) -> SessionRecord:
    return SessionRecord(
        thread_id=555,
        session_id="sess-abc",
        working_dir="/tmp/x",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-08-20 10:00:00",
        last_used_at="2026-08-20 11:00:00",
        closed_at=closed_at,
    )


# ── the verdict itself ───────────────────────────────────────────────────────


class TestClassify:
    def test_no_row_is_untracked(self) -> None:
        assert classify(None) is ThreadResume.UNTRACKED

    def test_open_row_resumes(self) -> None:
        assert classify(_record()) is ThreadResume.RESUMES

    def test_closed_row_is_closed(self) -> None:
        assert classify(_record(closed_at="2026-08-20 12:00:00")) is ThreadResume.CLOSED

    def test_accepts_message_matches_the_receiving_side(self) -> None:
        """A plain message is acted on for exactly the verdicts that have a row."""
        assert accepts_message(ThreadResume.RESUMES) is True
        assert accepts_message(ThreadResume.CLOSED) is True
        assert accepts_message(ThreadResume.UNTRACKED) is False


# ── AC3: the hint tells the truth for each verdict ───────────────────────────


class TestStoppedHint:
    def test_resumable_hint_promises_auto_resume(self) -> None:
        text = stopped_hint(ThreadResume.RESUMES)
        assert "自動で復元" in text

    def test_untracked_hint_does_not_promise_auto_resume(self) -> None:
        """The bug: this case used to get the 「送れば復元します」 wording."""
        text = stopped_hint(ThreadResume.UNTRACKED)
        assert "自動で復元" not in text
        assert "/clord" in text  # …and names the next step instead

    def test_closed_hint_points_at_the_reopen_path(self) -> None:
        text = stopped_hint(ThreadResume.CLOSED)
        assert "自動で復元" not in text
        assert "再開" in text

    def test_every_verdict_has_a_hint(self) -> None:
        for verdict in ThreadResume:
            assert stopped_hint(verdict).strip()

    @pytest.mark.asyncio
    async def test_hint_for_thread_reads_the_row(self) -> None:
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        assert await hint_for_thread(repo, 555) == stopped_hint(ThreadResume.UNTRACKED)

        repo.get = AsyncMock(return_value=_record())
        assert await hint_for_thread(repo, 555) == stopped_hint(ThreadResume.RESUMES)

    @pytest.mark.asyncio
    async def test_hint_for_thread_never_raises(self) -> None:
        """A DB hiccup must not break the command that shows the hint."""
        repo = MagicMock()
        repo.get = AsyncMock(side_effect=RuntimeError("db is gone"))
        assert await hint_for_thread(repo, 555) == stopped_hint(ThreadResume.RESUMES)


# ── AC4: the wording and the acceptance check are defined once ───────────────


class TestSingleDefinition:
    def test_auto_resume_wording_lives_only_in_session_resume(self) -> None:
        """No second copy of the promise — that drift is what #538 is."""
        owners = [
            path.relative_to(_SRC).as_posix()
            for path in _SRC.rglob("*.py")
            if "自動で復元" in path.read_text(encoding="utf-8")
        ]
        assert owners == ["session_resume.py"], owners

    def test_cogs_go_through_the_shared_verdict(self) -> None:
        for name in ("cogs/session_manage.py", "cogs/claude_chat.py"):
            source = (_SRC / name).read_text(encoding="utf-8")
            assert "session_resume" in source, f"{name} does not use the shared verdict"


# ── AC1 / AC2: a message with no session row is answered, not swallowed ──────


def _make_cog():
    from c_lord.cogs.claude_chat import ClaudeChatCog

    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    ctx = MagicMock()
    ctx.valid = False
    bot.get_context = AsyncMock(return_value=ctx)
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner)


def _make_message(*, thread_id: int = 42, parent_id: int = 999):
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.send = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.channel = thread
    message.content = "hi"
    message.attachments = []
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = 7
    message.webhook_id = None
    message.type = discord.MessageType.default
    message.add_reaction = AsyncMock()
    return message, thread


class TestUntrackedThreadIsAnswered:
    @pytest.mark.asyncio
    async def test_logs_one_line(self, caplog: pytest.LogCaptureFixture) -> None:
        """AC1: the drop is greppable by thread id instead of being invisible."""
        cog = _make_cog()
        message, _ = _make_message()

        with caplog.at_level(logging.INFO, logger="c_lord.cogs.claude_chat"):
            await cog.on_message(message)

        hits = [r for r in caplog.records if "thread=42" in r.getMessage()]
        assert hits, caplog.text
        assert any("#538" in r.getMessage() for r in hits), caplog.text

    @pytest.mark.asyncio
    async def test_posts_the_untracked_notice(self) -> None:
        """AC2: the user is told the message did not run, and what to do next."""
        cog = _make_cog()
        message, thread = _make_message()

        await cog.on_message(message)

        thread.send.assert_awaited_once()
        posted = _said(thread.send)
        assert posted == UNTRACKED_NOTICE
        assert "/clord" in posted

    @pytest.mark.asyncio
    async def test_does_not_run_claude(self) -> None:
        cog = _make_cog()
        cog._handle_thread_reply = AsyncMock()
        message, _ = _make_message()

        await cog.on_message(message)

        cog._handle_thread_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_notice_is_posted_once_but_every_message_is_marked(self) -> None:
        """Repeat messages get a ⚠️ reaction rather than a repeated wall of text."""
        cog = _make_cog()
        first, thread = _make_message()
        second, _ = _make_message()
        second.channel = thread

        await cog.on_message(first)
        await cog.on_message(second)

        assert thread.send.await_count == 1
        first.add_reaction.assert_awaited_once_with(UNTRACKED_REACTION)
        second.add_reaction.assert_awaited_once_with(UNTRACKED_REACTION)

    @pytest.mark.asyncio
    async def test_quiet_in_an_unbound_channel_that_is_not_ours(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#522: several c-lord instances share a guild — do not all shout."""
        cog = _make_cog()
        message, thread = _make_message(parent_id=7777)  # != bot.channel_id

        with caplog.at_level(logging.INFO, logger="c_lord.cogs.claude_chat"):
            await cog.on_message(message)

        thread.send.assert_not_awaited()
        message.add_reaction.assert_not_awaited()
        # …and it does not flood the log at INFO either.
        assert not [r for r in caplog.records if r.levelno >= logging.INFO], caplog.text

    @pytest.mark.asyncio
    async def test_speaks_in_a_bound_channel_that_is_not_ours(self) -> None:
        """A channel bound with /clord-init is ours even when it is not DISCORD_CHANNEL_ID."""
        cog = _make_cog()
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
        message, thread = _make_message(parent_id=7777)

        await cog.on_message(message)

        thread.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tracked_thread_is_untouched(self) -> None:
        """A thread with a row keeps running Claude and gets no notice."""
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=_record())
        cog._handle_thread_reply = AsyncMock()
        message, thread = _make_message()

        await cog.on_message(message)

        cog._handle_thread_reply.assert_awaited_once_with(message)
        thread.send.assert_not_awaited()
        message.add_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notice_failure_is_not_fatal(self) -> None:
        cog = _make_cog()
        message, thread = _make_message()
        thread.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "nope"))

        await cog.on_message(message)  # must not raise


# ── AC3 wired into the commands that show the hint ───────────────────────────


def _manage_cog():
    from c_lord.cogs.session_manage import SessionManageCog

    bot = MagicMock()
    bot.channel_id = 999
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    return SessionManageCog(bot=bot, repo=repo)


class TestCommandsCheckResumability:
    @pytest.mark.asyncio
    async def test_screenshot_untracked_thread_gets_the_honest_hint(self) -> None:
        cog = _manage_cog()
        tmux = MagicMock()
        tmux._find_window_for_thread = MagicMock(return_value=None)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.parent_id = 999
        respond, ack = AsyncMock(), AsyncMock()

        await cog._screenshot_impl(channel=thread, respond=respond, ack=ack)

        assert _said(respond) == stopped_hint(ThreadResume.UNTRACKED)

    @pytest.mark.asyncio
    async def test_screenshot_resumable_thread_keeps_the_resume_hint(self) -> None:
        cog = _manage_cog()
        cog.repo.get = AsyncMock(return_value=_record())
        tmux = MagicMock()
        tmux._find_window_for_thread = MagicMock(return_value=None)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.parent_id = 999
        respond, ack = AsyncMock(), AsyncMock()

        await cog._screenshot_impl(channel=thread, respond=respond, ack=ack)

        assert _said(respond) == stopped_hint(ThreadResume.RESUMES)

    @pytest.mark.asyncio
    async def test_resync_untracked_thread_gets_the_honest_hint(self) -> None:
        cog = _manage_cog()
        cog._find_thread_window = AsyncMock(return_value=(None, None))
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.parent_id = 999
        respond, ack = AsyncMock(), AsyncMock()

        await cog._resync_impl(channel=thread, scope="thread", respond=respond, ack=ack)

        assert _said(respond) == stopped_hint(ThreadResume.UNTRACKED)

    @pytest.mark.asyncio
    async def test_resync_closed_thread_points_at_reopen(self) -> None:
        cog = _manage_cog()
        cog.repo.get = AsyncMock(return_value=_record(closed_at="2026-08-20 12:00:00"))
        cog._find_thread_window = AsyncMock(return_value=(None, None))
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.parent_id = 999
        respond, ack = AsyncMock(), AsyncMock()

        await cog._resync_impl(channel=thread, scope="thread", respond=respond, ack=ack)

        assert _said(respond) == stopped_hint(ThreadResume.CLOSED)
