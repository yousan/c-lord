"""Tests for the 終了 (closed) session lifecycle — #512.

``/close-workspace`` used to be invisible to the user: the thread name kept its
now-meaningless ``W<N> │`` prefix and a later message silently woke Claude back
up via the ``--continue`` crash-recovery path (#270).  #512 makes "終了" a real,
persisted state:

* the thread is renamed ``[停止] …`` (#574 renamed it from ``[終了]``)
* a message in a closed thread is **not** forwarded to Claude; a notice with a
  「▶️ 再開する」button is posted instead
* reopening clears the flag, restores the name, and runs the held message
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.database.repository import SessionRecord
from c_lord.thread_name import CLOSED_MARK


def _record(thread_id: int = 555, *, closed_at: str | None = None) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id="sess-abc",
        working_dir="/tmp/x",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-08-18 10:00:00",
        last_used_at="2026-08-18 11:00:00",
        topic="認証リファクタ",
        issue_ref="404",
        closed_at=closed_at,
    )


def _thread(thread_id: int = 555) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = 999
    thread.name = "W3 │ #404 認証リファクタ"
    thread.edit = AsyncMock()
    thread.send = AsyncMock()
    return thread


# ── /close-workspace marks + renames ─────────────────────────────────────────


class TestCloseMarksThread:
    def _cog(self, record: SessionRecord | None):
        from c_lord.cogs.session_manage import SessionManageCog

        bot = MagicMock()
        bot.channel_id = 999
        bot.get_cog = MagicMock(return_value=None)
        repo = MagicMock()
        repo.get = AsyncMock(return_value=record)
        repo.set_closed = AsyncMock()
        cog = SessionManageCog(bot=bot, repo=repo)
        tmux_mgr = MagicMock()
        tmux_mgr.kill_session = MagicMock(return_value=True)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)
        cog._resolve_session_dir_manager = AsyncMock(return_value=None)
        return cog

    def _ctx(self, thread: MagicMock) -> MagicMock:
        ctx = MagicMock()
        ctx.send = AsyncMock()
        ctx.channel = thread
        return ctx

    @pytest.mark.asyncio
    async def test_close_persists_closed_flag(self) -> None:
        """#512 AC4: the intentional close is recorded, not inferred from a dead pane."""
        cog = self._cog(_record())
        thread = _thread()
        await cog.close_workspace_text.callback(cog, self._ctx(thread))
        # #574: reason rides along for the notice wording; the state itself is
        # still decided by closed_at alone.
        cog.repo.set_closed.assert_awaited_once_with(555, True, reason="manual")

    @pytest.mark.asyncio
    async def test_close_renames_thread_with_marker(self) -> None:
        """#512 AC4: the thread name gains the :data:`CLOSED_MARK` marker on stop.

        Asserted through the constant: #574 renamed it and these tests should
        track the marker, not re-encode its wording.
        """
        cog = self._cog(_record())
        thread = _thread()
        await cog.close_workspace_text.callback(cog, self._ctx(thread))

        names = [c.kwargs.get("name") for c in thread.edit.call_args_list]
        assert f"{CLOSED_MARK} #404 認証リファクタ" in names

    @pytest.mark.asyncio
    async def test_close_still_archives_thread(self) -> None:
        """#271 must keep working: the rename does not replace the archive."""
        cog = self._cog(_record())
        thread = _thread()
        await cog.close_workspace_text.callback(cog, self._ctx(thread))

        assert any(c.kwargs.get("archived") is True for c in thread.edit.call_args_list)

    @pytest.mark.asyncio
    async def test_close_renames_and_archives_in_one_patch(self) -> None:
        """#512: one PATCH, not two.

        Discord allows ~2 thread renames per 10 minutes and refuses to rename an
        archived thread at all (code 50083). Splitting this into
        ``edit(name=…)`` + ``edit(archived=True)`` spends the allowance twice and
        orders badly — observed live on staging as a 429 that silently dropped
        the marker while the archive went through.
        """
        cog = self._cog(_record())
        thread = _thread()
        await cog.close_workspace_text.callback(cog, self._ctx(thread))

        assert thread.edit.await_count == 1
        kwargs = thread.edit.await_args.kwargs
        assert kwargs.get("name") == f"{CLOSED_MARK} #404 認証リファクタ"
        assert kwargs.get("archived") is True

    @pytest.mark.asyncio
    async def test_close_rename_failure_does_not_block_archive(self) -> None:
        """A 403 on rename (no Manage Threads) must not cost us the archive."""
        cog = self._cog(_record())
        thread = _thread()

        calls: list[dict] = []

        async def edit(**kwargs):
            calls.append(kwargs)
            if "name" in kwargs:
                raise discord.HTTPException(MagicMock(status=403), "no perms")

        thread.edit = AsyncMock(side_effect=edit)
        await cog.close_workspace_text.callback(cog, self._ctx(thread))

        assert any(c.get("archived") is True for c in calls)


# ── /reopen-workspace clears the flag ────────────────────────────────────────


class TestReopenCommand:
    def _cog(self, record: SessionRecord | None):
        from c_lord.cogs.session_manage import SessionManageCog

        bot = MagicMock()
        bot.channel_id = 999
        bot.get_cog = MagicMock(return_value=None)
        repo = MagicMock()
        repo.get = AsyncMock(return_value=record)
        repo.set_closed = AsyncMock()
        return SessionManageCog(bot=bot, repo=repo)

    def _ctx(self, thread: MagicMock) -> MagicMock:
        ctx = MagicMock()
        ctx.send = AsyncMock()
        ctx.channel = thread
        return ctx

    @pytest.mark.asyncio
    async def test_reopen_clears_flag_and_restores_name(self) -> None:
        """#512 AC8: /reopen-workspace un-closes the session and drops the marker."""
        cog = self._cog(_record(closed_at="2026-08-18 12:00:00"))
        thread = _thread()
        thread.name = f"{CLOSED_MARK} #404 認証リファクタ"

        await cog.reopen_workspace_text.callback(cog, self._ctx(thread))

        cog.repo.set_closed.assert_awaited_once_with(555, False)
        names = [c.kwargs.get("name") for c in thread.edit.call_args_list if "name" in c.kwargs]
        assert names and not names[-1].startswith(CLOSED_MARK)

    @pytest.mark.asyncio
    async def test_reopen_unarchives_in_the_same_patch(self) -> None:
        """#512: reopening must clear ``archived`` — Discord rejects edits to an
        archived thread (code 50083), so a rename that does not un-archive in the
        same PATCH is refused outright."""
        cog = self._cog(_record(closed_at="2026-08-18 12:00:00"))
        thread = _thread()
        thread.name = f"{CLOSED_MARK} #404 認証リファクタ"

        await cog.reopen_workspace_text.callback(cog, self._ctx(thread))

        assert thread.edit.await_count == 1
        assert thread.edit.await_args.kwargs.get("archived") is False

    @pytest.mark.asyncio
    async def test_reopen_on_open_thread_is_a_noop_notice(self) -> None:
        """Reopening a thread that was never closed says so instead of renaming."""
        cog = self._cog(_record(closed_at=None))
        thread = _thread()

        await cog.reopen_workspace_text.callback(cog, self._ctx(thread))

        cog.repo.set_closed.assert_not_awaited()


# ── a message in a closed thread is held, not run ────────────────────────────


class TestClosedThreadBlocksMessages:
    def _cog(self, record: SessionRecord | None):
        from c_lord.cogs.claude_chat import ClaudeChatCog

        bot = MagicMock()
        bot.channel_id = 999
        bot.settings_repo = None
        bot.get_cog = MagicMock(return_value=None)
        repo = MagicMock()
        repo.get = AsyncMock(return_value=record)
        repo.set_closed = AsyncMock()
        runner = MagicMock()
        runner.clone = MagicMock(return_value=MagicMock())
        cog = ClaudeChatCog(bot=bot, repo=repo, runner=runner)
        cog._run_claude = AsyncMock()
        return cog

    def _message(self, thread: MagicMock) -> MagicMock:
        msg = MagicMock(spec=discord.Message)
        msg.id = 1
        msg.channel = thread
        msg.content = "こんにちは"
        msg.attachments = []
        msg.reference = None
        msg.author = MagicMock()
        msg.author.bot = False
        return msg

    @pytest.mark.asyncio
    async def test_closed_thread_does_not_run_claude(self) -> None:
        """#512 AC6: the held message never reaches Claude."""
        cog = self._cog(_record(closed_at="2026-08-18 12:00:00"))
        thread = _thread()

        await cog._handle_thread_reply(self._message(thread))

        cog._run_claude.assert_not_called()

    @pytest.mark.asyncio
    async def test_closed_thread_posts_notice_with_reopen_button(self) -> None:
        """#512 AC6: the user is told it is closed and how to resume."""
        cog = self._cog(_record(closed_at="2026-08-18 12:00:00"))
        thread = _thread()

        await cog._handle_thread_reply(self._message(thread))

        thread.send.assert_awaited_once()
        kwargs = thread.send.call_args.kwargs
        embed = kwargs.get("embed")
        assert embed is not None
        assert "終了" in embed.title
        assert "再開" in (embed.description or "")
        assert kwargs.get("view") is not None

    @pytest.mark.asyncio
    async def test_open_thread_is_unaffected(self) -> None:
        """#512 AC9: a session that was never closed still runs normally (#270 intact)."""
        cog = self._cog(_record(closed_at=None))
        thread = _thread()

        await cog._handle_thread_reply(self._message(thread))

        assert cog._run_claude.called
        thread.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reopen_button_clears_flag_and_runs_held_message(self) -> None:
        """#512 AC7: pressing 再開する resumes and executes the message that was held."""
        record = _record(closed_at="2026-08-18 12:00:00")
        cog = self._cog(record)
        thread = _thread()
        message = self._message(thread)

        await cog._handle_thread_reply(message)
        view = thread.send.call_args.kwargs["view"]

        # Simulate the button press.
        interaction = MagicMock(spec=discord.Interaction)
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        # After reopening, the DB row is no longer closed.
        cog.repo.get = AsyncMock(return_value=_record(closed_at=None))

        await view.reopen_button.callback(interaction)

        cog.repo.set_closed.assert_awaited_once_with(555, False)
        assert cog._run_claude.called


class TestStopEndsTheRunningTurn:
    """#909: stopping the workspace mid-turn ends that turn as a user stop.

    The teardown killed the window but left ``run_claude`` polling it; five
    minutes later the turn ended as "Claude exited without producing a
    response (possible startup failure or crash)", with ❌ and a failure ping
    for a stop the user asked for.
    """

    def _cog(self, chat: object):
        from c_lord.cogs.session_manage import SessionManageCog

        bot = MagicMock()
        bot.channel_id = 999
        bot.get_cog = MagicMock(side_effect=lambda name: chat if name == "ClaudeChatCog" else None)
        repo = MagicMock()
        repo.get = AsyncMock(return_value=_record())
        repo.set_closed = AsyncMock()
        cog = SessionManageCog(bot=bot, repo=repo)
        order: list[str] = []
        tmux_mgr = MagicMock()
        tmux_mgr.kill_session = MagicMock(side_effect=lambda *_a: order.append("kill"))
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)
        cog._resolve_session_dir_manager = AsyncMock(return_value=None)
        return cog, order

    def _ctx(self, thread: MagicMock) -> MagicMock:
        ctx = MagicMock()
        ctx.send = AsyncMock()
        ctx.channel = thread
        return ctx

    @pytest.mark.asyncio
    async def test_the_turn_ends_before_the_window_goes_and_is_over_before_the_archive(
        self,
    ) -> None:
        """The final 🟡 must land before the archive: Discord refuses reactions in
        an archived thread (50083), which left the request with no lamp at all."""
        chat = MagicMock()
        cog, order = self._cog(chat)
        chat.end_turn_for_workspace_stop = MagicMock(side_effect=lambda _t: order.append("end"))

        async def wait(_t: int, **_kw: object) -> None:
            order.append("turn over")

        chat.wait_for_turn_end = AsyncMock(side_effect=wait)
        thread = _thread()
        thread.edit = AsyncMock(side_effect=lambda **_kw: order.append("archive"))
        await cog.close_workspace_text.callback(cog, self._ctx(thread))
        chat.end_turn_for_workspace_stop.assert_called_once_with(555)
        assert order == ["end", "kill", "turn over", "archive"]

    @pytest.mark.asyncio
    async def test_no_chat_cog_still_stops(self) -> None:
        cog, order = self._cog(None)
        await cog.close_workspace_text.callback(cog, self._ctx(_thread()))
        assert order == ["kill"]


class TestChatCogEndsTheRun:
    """#909: the chat cog half — the turn's runner is told it was stopped."""

    def _chat(self):
        from c_lord.cogs.claude_chat import ClaudeChatCog

        bot = MagicMock()
        bot.channel_id = 999
        return ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())

    def test_a_running_turn_is_withdrawn_as_a_user_stop(self) -> None:
        chat = self._chat()
        runner = MagicMock()
        chat._active_runners[555] = runner
        assert chat.end_turn_for_workspace_stop(555) is True
        runner.withdraw.assert_called_once_with(silent=False)

    def test_nothing_running_is_a_no_op(self) -> None:
        assert self._chat().end_turn_for_workspace_stop(555) is False

    @pytest.mark.asyncio
    async def test_waiting_returns_once_the_turn_task_is_done(self) -> None:
        import asyncio

        chat = self._chat()
        done = asyncio.Event()

        async def turn() -> None:
            await done.wait()

        chat._active_tasks[555] = asyncio.create_task(turn())
        asyncio.get_running_loop().call_later(0.05, done.set)
        await asyncio.wait_for(chat.wait_for_turn_end(555, timeout=2.0), 1.0)
        assert chat._active_tasks[555].done()

    @pytest.mark.asyncio
    async def test_waiting_gives_up_after_the_timeout(self) -> None:
        import asyncio

        chat = self._chat()
        chat._active_tasks[555] = asyncio.create_task(asyncio.sleep(10))
        await asyncio.wait_for(chat.wait_for_turn_end(555, timeout=0.05), 1.0)
        chat._active_tasks[555].cancel()

    @pytest.mark.asyncio
    async def test_waiting_with_nothing_running_returns(self) -> None:
        await self._chat().wait_for_turn_end(555, timeout=0.05)
