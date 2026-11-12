"""スリープからの無言復元 — Issue #572。

スリープの目的は「利用者が気づかない」こと。**眠らせたことに気づかれないなら、
起こしたことにも気づかれてはいけない。** だから次の投稿は:

* 「復元しますか?」も聞かない（ダイアログもボタンも出さない）
* 「復元しました」も言わない

一方、**落ちた**のと**眠らせた**のは別物で、落ちたときは黙ってはいけない（#464:
無言で復元すると、前ターンの出力が再生されて「壊れた」ように見える）。同じ
「ペインが死んでいる」状態から違う文言を選ぶので、その判断は1つの関数に集める。

**唯一の饒舌な場面は失敗したとき。** 利用者は何もしていないのに c-lord が勝手に
畳んだのだから、そこで無言で捨てるのは #538 の再演になる。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner
from c_lord.claude.types import MessageType
from c_lord.database.repository import SessionRecord
from c_lord.session_resume import resume_notice


def _record(*, slept_at: str | None = None, closed_at: str | None = None) -> SessionRecord:
    return SessionRecord(
        thread_id=555,
        session_id="sess-abc",
        working_dir="/tmp/x",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-08-31 06:00:00",
        last_used_at="2026-08-31 07:00:00",
        closed_at=closed_at,
        slept_at=slept_at,
    )


def _thread() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = 555
    thread.parent_id = 999
    thread.name = "W3 │ #404 認証リファクタ"
    thread.edit = AsyncMock()
    thread.send = AsyncMock()
    return thread


class TestTheWordingIsDecidedInOnePlace:
    """落ちた / 眠らせた / 明示的に再開した の3つを1つの関数が捌く。"""

    def test_a_slept_workspace_says_nothing(self) -> None:
        assert resume_notice(slept=True, reopened=False) is None

    def test_a_crashed_workspace_still_explains_itself(self) -> None:
        """#464: 黙って復元すると前ターンの出力の再生が「壊れた」に見える。"""
        notice = resume_notice(slept=False, reopened=False)
        assert notice is not None
        assert "落ちていた" in notice

    def test_a_deliberate_reopen_is_not_called_a_crash(self) -> None:
        notice = resume_notice(slept=False, reopened=True)
        assert notice is not None
        assert "落ちていた" not in notice

    def test_a_deliberate_reopen_wins_over_a_stale_sleep(self) -> None:
        """4時間で眠り、7日で停止し、利用者が「再開する」を押した順路。

        押したのは利用者なので、無言で戻るのはむしろ不親切。
        """
        assert resume_notice(slept=True, reopened=True) is not None


class TestSilentRestoreOnTheNextMessage:
    def _cog(self, record: SessionRecord, *, pane_alive: bool = False):
        from c_lord.cogs.claude_chat import ClaudeChatCog

        bot = MagicMock()
        bot.channel_id = 999
        bot.settings_repo = None
        bot.get_cog = MagicMock(return_value=None)

        repo = MagicMock()
        repo.get = AsyncMock(return_value=record)
        repo.set_closed = AsyncMock()
        repo.set_slept = AsyncMock()

        runner = MagicMock()
        runner.clone = MagicMock(return_value=MagicMock())

        cog = ClaudeChatCog(bot=bot, repo=repo, runner=runner)
        cog._run_claude = AsyncMock()

        tmux = MagicMock()
        tmux.is_claude_running = MagicMock(return_value=pane_alive)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        return cog

    def _message(self, thread: MagicMock) -> MagicMock:
        msg = MagicMock(spec=discord.Message)
        msg.id = 1
        msg.channel = thread
        msg.content = "続きお願い"
        msg.attachments = []
        msg.reference = None
        msg.author = MagicMock()
        msg.author.bot = False
        return msg

    @pytest.mark.asyncio
    async def test_a_slept_workspace_resumes_without_a_word(self) -> None:
        """#572 AC: ダイアログもボタンも出さずに会話の続きから再開する。"""
        cog = self._cog(_record(slept_at="2026-08-31 11:00:00"))
        thread = _thread()

        await cog._handle_thread_reply(self._message(thread))

        thread.send.assert_not_awaited()
        assert cog._run_claude.called
        assert cog._run_claude.call_args.kwargs["try_continue"] is True

    @pytest.mark.asyncio
    async def test_a_crashed_workspace_still_announces_the_recovery(self) -> None:
        """#464 の回帰ガード。スリープの無言化で落ちたときまで黙ってはいけない。"""
        cog = self._cog(_record(slept_at=None))
        thread = _thread()

        await cog._handle_thread_reply(self._message(thread))

        thread.send.assert_awaited_once()
        assert "落ちていた" in thread.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_the_sleep_mark_is_cleared_once_consumed(self) -> None:
        """残したままだと、次に本当に落ちたとき無言で復元してしまう。"""
        cog = self._cog(_record(slept_at="2026-08-31 11:00:00"))

        await cog._handle_thread_reply(self._message(_thread()))

        cog.repo.set_slept.assert_awaited_once_with(555, False)

    @pytest.mark.asyncio
    async def test_a_live_pane_is_untouched(self) -> None:
        """眠っていないワークスペースは今までどおり send_input で続く。"""
        cog = self._cog(_record(slept_at=None), pane_alive=True)
        thread = _thread()

        await cog._handle_thread_reply(self._message(thread))

        thread.send.assert_not_awaited()
        assert cog._run_claude.call_args.kwargs["try_continue"] is False


class TestRestoreFailureIsNeverSilent:
    """#572 AC: 復元**失敗**時はエラーを出す（無言で捨てない）。

    自動スリープは利用者が頼んでいない。頼んでいない片付けの失敗を黙って捨てると
    「案内どおりにしたのに無反応」(#538) より理不尽な体験になる。
    """

    @pytest.mark.asyncio
    async def test_a_resume_that_cannot_start_claude_yields_an_error(self) -> None:
        tmux = MagicMock()
        tmux.capture_pane.return_value = ""
        tmux.is_claude_running.return_value = False
        tmux.start_claude.return_value = False  # --continue も fresh も起動できない
        tmux.send_input.return_value = True

        runner = TmuxClaudeRunner(
            tmux_manager=tmux,
            thread_id=12345,
            model="sonnet",
            timeout_seconds=10,
            try_continue=True,
        )

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
            patch("c_lord.claude.tmux_runner._CONTINUE_CHECK_DELAY", 0.01),
        ):
            async for event in runner.run("続きお願い"):
                events.append(event)

        errors = [e for e in events if e.message_type == MessageType.RESULT and e.error]
        assert errors, "復元に失敗したのに何も返っていない"
        # 「失敗した」で終わらせず、次の手まで書く。利用者は何も操作していないので
        # 「どうすればいいか」が分からないまま置き去りにされるのが最悪。
        assert "/claude-restart" in (errors[-1].error or "")
