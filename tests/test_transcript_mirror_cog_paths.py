"""``TranscriptMirrorCog``: the edges of its sinks and the menu bridge.

The mirror is the only delivery path (#712), so what each sink does when the
thread cannot be found, when Discord refuses a post, or when a file cannot be
read decides whether the reader is told or left waiting. These tests cover those
edges, plus the ask bridge that turns a menu raised outside a Discord turn into
buttons (#232).
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs import transcript_mirror as tm_mod
from c_lord.cogs.channel_repo import ChannelRepoCog
from c_lord.cogs.transcript_mirror import TranscriptMirrorCog
from c_lord.transcript.mirror import UserFileRequest

THREAD_ID = 8080


def _http_error(status: int = 500) -> discord.HTTPException:
    return discord.HTTPException(MagicMock(status=status), "boom")


def _cog(bot: object | None = None, repo: object | None = None) -> TranscriptMirrorCog:
    return TranscriptMirrorCog(bot or MagicMock(), session_repo=repo or MagicMock())  # type: ignore[arg-type]


def _bot_without_channel() -> MagicMock:
    bot = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    return bot


def _bot_with(channel: object) -> MagicMock:
    bot = MagicMock()
    bot.get_channel.return_value = channel
    return bot


class TestTriggerMessage:
    def test_a_running_mirror_starts_its_wait_clock_at_the_trigger(self) -> None:
        """#539: the earliest point a turn is known to start."""
        cog = _cog()
        mirror = MagicMock()
        cog._mirrors[THREAD_ID] = mirror

        cog.set_trigger_message(THREAD_ID, 99)

        mirror.note_turn_started.assert_called_once_with(trigger_id=99)


class TestOnReadyFailures:
    async def test_a_failed_recovery_still_starts_the_mirror(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude" / "projects" / "-w").mkdir(parents=True)
        row = SimpleNamespace(thread_id=THREAD_ID, working_dir="/w", closed_at=None)
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[row])
        cog = _cog(repo=repo)
        monkeypatch.setattr(cog, "_recover_final_answer", AsyncMock(side_effect=OSError("io")))
        monkeypatch.setattr(cog, "_notify_host_restart_stops", AsyncMock())
        try:
            with caplog.at_level(logging.WARNING, logger="c_lord.cogs.transcript_mirror"):
                await cog.on_ready()

            assert THREAD_ID in cog._mirrors
            assert any("final-answer recovery failed" in r.getMessage() for r in caplog.records)
        finally:
            await cog.cog_unload()

    async def test_a_failed_host_restart_notice_is_logged_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#807: a courtesy notice must not take the mirrors down with it."""
        monkeypatch.setattr(tm_mod, "live_claude_panes", MagicMock(side_effect=OSError("tmux")))
        cog = _cog()

        with caplog.at_level(logging.WARNING, logger="c_lord.cogs.transcript_mirror"):
            await cog._notify_host_restart_stops([(THREAD_ID, "/w")])

        assert any("host-restart notice failed" in r.getMessage() for r in caplog.records)


class TestRelease:
    def test_releasing_a_thread_without_a_mirror_is_a_no_op(self) -> None:
        cog = _cog()

        cog._release(THREAD_ID)

        assert cog._releasing == set()


class TestProgressAndFold:
    async def test_progress_post_without_a_thread_posts_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLORD_TURN_PROGRESS", raising=False)
        progress = _cog(_bot_without_channel())._make_progress(THREAD_ID)
        assert progress is not None

        assert await progress._post("x") is None

    async def test_progress_delete_of_an_already_deleted_line_is_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#930: already gone is done, not a reason to retry."""
        monkeypatch.delenv("CLORD_TURN_PROGRESS", raising=False)
        progress = _cog()._make_progress(THREAD_ID)
        assert progress is not None
        handle = MagicMock()
        handle.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))

        await progress._delete(handle)

        handle.delete.assert_awaited_once()

    async def test_fold_post_without_a_thread_posts_nothing(self) -> None:
        post, _edit = _cog(_bot_without_channel())._make_fold(THREAD_ID)

        assert await post("x") is None


class TestCursorSink:
    async def test_a_cursor_that_cannot_be_saved_does_not_break_delivery(self) -> None:
        """#215: the cursor is bookkeeping; a DB error must not reach the mirror."""
        repo = MagicMock()
        repo.set_mirror_replied_uuid = AsyncMock(side_effect=RuntimeError("db locked"))
        sink = _cog(repo=repo)._make_cursor_sink(THREAD_ID)

        await sink("uuid-1")

        repo.set_mirror_replied_uuid.assert_awaited_once_with(THREAD_ID, "uuid-1")


class TestSinksWithoutAThread:
    async def test_sink_drops_the_post_when_the_thread_is_gone(self) -> None:
        await _cog(_bot_without_channel())._make_sink(THREAD_ID)("hello")

    async def test_sink_warns_when_the_channel_cannot_send(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = SimpleNamespace()  # e.g. a category channel: no .send
        with caplog.at_level(logging.WARNING, logger="c_lord.cogs.transcript_mirror"):
            await _cog(_bot_with(channel))._make_sink(THREAD_ID)("hello")

        assert any("has no .send" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("channel", [None, SimpleNamespace()])
    async def test_reply_and_file_sinks_post_nothing_without_a_sendable_thread(
        self, channel: object | None, tmp_path: Path
    ) -> None:
        bot = _bot_without_channel() if channel is None else _bot_with(channel)
        cog = _cog(bot)
        progress = tmp_path / "progress.txt"
        progress.write_text("log")

        await cog._make_reply_sink(THREAD_ID)("answer")
        await cog._make_file_sink(THREAD_ID)("answer", str(progress))

    async def test_reply_sink_logs_a_refused_answer(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("CLORD_REPLY_TO_TRIGGER", "0")
        channel = MagicMock()
        channel.send = AsyncMock(side_effect=_http_error(403))
        record = MagicMock()
        monkeypatch.setattr("c_lord.skills.reply_tracker.record_reply_message", record)

        with caplog.at_level(logging.WARNING, logger="c_lord.cogs.transcript_mirror"):
            await _cog(_bot_with(channel))._make_reply_sink(THREAD_ID)("answer")

        assert any(
            "reply_sink failed" in r.getMessage() and "status=403" in r.getMessage()
            for r in caplog.records
        )
        record.assert_not_called()


class TestFileSinkFallback:
    async def test_the_plain_text_fallback_is_tracked_as_the_reply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The fallback message is the answer, so it is what the 🟡 ping follows."""
        monkeypatch.setenv("CLORD_REPLY_TO_TRIGGER", "0")
        fallback_msg = MagicMock()
        channel = MagicMock()
        channel.send = AsyncMock(side_effect=[_http_error(413), fallback_msg])
        record = MagicMock()
        monkeypatch.setattr("c_lord.skills.reply_tracker.record_reply_message", record)
        progress = tmp_path / "progress.txt"
        progress.write_text("log")

        await _cog(_bot_with(channel))._make_file_sink(THREAD_ID)("answer", str(progress))

        record.assert_called_once_with(THREAD_ID, fallback_msg)
        assert "files" not in channel.send.await_args_list[1].kwargs


class TestUserFiles:
    async def test_a_caption_with_no_sendable_file_is_still_posted_before_the_warning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CLORD_SEND_USER_FILE", raising=False)
        channel = MagicMock()
        channel.send = AsyncMock()
        sink = _cog(_bot_with(channel))._make_user_file_sink(THREAD_ID)
        assert sink is not None

        await sink(
            UserFileRequest(
                tool_use_id="t1", paths=[str(tmp_path / "missing.png")], caption="下に貼ります"
            )
        )

        contents = [c.kwargs["content"] for c in channel.send.await_args_list]
        assert contents[0] == "下に貼ります"
        assert "添付できませんでした" in contents[1]
        assert "missing.png" in contents[1]

    def test_a_file_that_cannot_be_stat_ed_is_a_problem_line(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("x")
        real_stat = Path.stat

        def failing_stat(self: Path, **kwargs: object):
            if self == target and not kwargs:
                raise PermissionError(13, "Permission denied")
            return real_stat(self, **kwargs)  # type: ignore[arg-type]

        with patch.object(Path, "stat", failing_stat):
            sendable, problems = TranscriptMirrorCog._vet_user_files([str(target)])

        assert sendable == []
        assert problems == ["`a.txt` — 読めませんでした (Permission denied)"]


class TestAskBridge:
    """#232: a menu raised outside a Discord-driven turn still becomes buttons."""

    @pytest.fixture
    def bridge(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        bridge = AsyncMock()
        monkeypatch.setattr("c_lord.discord_ui.ask_handler.bridge_pane_ask", bridge)
        monkeypatch.setattr(
            "c_lord.discord_ui.ask_bus.ask_bus", SimpleNamespace(is_active=lambda _t: False)
        )
        return bridge

    def _thread(self) -> MagicMock:
        thread = MagicMock(spec=discord.Thread)
        thread.id = THREAD_ID
        thread.parent_id = 70
        return thread

    async def test_a_menu_the_turn_already_bridged_is_left_to_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = AsyncMock()
        monkeypatch.setattr("c_lord.discord_ui.ask_handler.bridge_pane_ask", bridge)
        monkeypatch.setattr(
            "c_lord.discord_ui.ask_bus.ask_bus", SimpleNamespace(is_active=lambda _t: True)
        )

        await _cog(_bot_with(self._thread()))._make_ask_bridge(THREAD_ID)(object())

        bridge.assert_not_awaited()

    async def test_a_channel_that_is_not_a_thread_gets_no_buttons(self, bridge: AsyncMock) -> None:
        await _cog(_bot_with(MagicMock()))._make_ask_bridge(THREAD_ID)(object())

        bridge.assert_not_awaited()

    async def test_without_a_tmux_manager_it_says_so(
        self, bridge: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        bot = _bot_with(self._thread())
        bot.get_cog.return_value = None
        bot.tmux_manager = None

        with caplog.at_level(logging.WARNING, logger="c_lord.cogs.transcript_mirror"):
            await _cog(bot)._make_ask_bridge(THREAD_ID)(object())

        bridge.assert_not_awaited()
        assert any("no tmux manager" in r.getMessage() for r in caplog.records)

    async def test_the_threads_own_session_drives_the_menu_and_the_owner_is_pinged(
        self, bridge: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#427: resolve the session with the thread id; #480: ping the owner."""
        thread = self._thread()
        bot = _bot_with(thread)
        manager = MagicMock()
        channel_cog = MagicMock(spec=ChannelRepoCog)
        channel_cog.resolve_tmux_manager = AsyncMock(return_value=manager)
        bot.get_cog.return_value = channel_cog
        monkeypatch.setattr(tm_mod, "owner_notify_id", lambda _bot, kind: 4242)
        runner_cls = MagicMock()
        monkeypatch.setattr("c_lord.claude.tmux_runner.TmuxClaudeRunner", runner_cls)
        question = object()

        await _cog(bot)._make_ask_bridge(THREAD_ID)(question)

        channel_cog.resolve_tmux_manager.assert_awaited_once_with(70, thread_id=THREAD_ID)
        runner_cls.assert_called_once_with(tmux_manager=manager, thread_id=THREAD_ID)
        assert bridge.await_args is not None
        args, kwargs = bridge.await_args
        assert args == (thread, question, runner_cls.return_value)
        assert kwargs["notify_user_id"] == 4242
        assert kwargs["authorizer"] is bot.authorizer
        assert kwargs["ask_repo"] is bot.ask_repo

    async def test_without_a_channel_binding_the_bots_manager_is_used(
        self, bridge: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bot = _bot_with(self._thread())
        bot.get_cog.return_value = None
        runner_cls = MagicMock()
        monkeypatch.setattr("c_lord.claude.tmux_runner.TmuxClaudeRunner", runner_cls)

        await _cog(bot)._make_ask_bridge(THREAD_ID)(object())

        runner_cls.assert_called_once_with(tmux_manager=bot.tmux_manager, thread_id=THREAD_ID)
        bridge.assert_awaited_once()
