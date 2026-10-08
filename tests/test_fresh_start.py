"""片付け済み・記録の無いスレッドは、同じスレッドで新しい会話として始まる — #862.

いままで: 30日スイープで片付けられた（#818 の墓標がある）スレッドや、DB の行ごと
消えたスレッドに投稿すると、「いま送ったメッセージは Claude に届いていません。
チャンネルで /clord を…」と断られ、利用者は新しいスレッドを立て直していた。

会話（Claude Code の transcript）は消えているので復元はできない。でもリポジトリの
紐付けは残っているので、**同じスレッドで新しく始める**ことはできる:

1. 「🧹 前の会話は残っていないので、新しい会話として始めます」の一文
2. 作業ディレクトリを作り直す（``_run_claude`` が新しいスレッドと同じ clone をする）
3. ``--continue`` せず新しい会話で起動
4. 依頼をそのまま実行。最初の依頼に「discord-read で過去のやり取りを読める」一行を添える

紐付けが無いときは何を clone するか分からないので、従来どおり案内だけ。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.database.repository import SWEPT_REASON, SessionRecord
from c_lord.session_resume import (
    FRESH_START_NOTICE,
    ThreadResume,
    fresh_start_preamble,
    stopped_hint,
)

THREAD_ID = 1538758307433676821
CHANNEL_ID = 999


def _tomb() -> SessionRecord:
    return SessionRecord(
        thread_id=THREAD_ID,
        session_id="1fcfb524-aaaa-bbbb-cccc-ddddeeeeffff",
        working_dir=f"/tmp/sessions/{CHANNEL_ID}/{THREAD_ID}",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-08-01 10:00:00",
        last_used_at="2026-09-02 11:00:00",
        closed_at="2026-10-02 03:00:00",
        closed_reason=SWEPT_REASON,
    )


def _thread() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.parent_id = CHANNEL_ID
    thread.owner_id = 777  # c-lord が立てたスレッド
    thread.name = "かたぼの管理"
    thread.send = AsyncMock()
    thread.edit = AsyncMock()
    return thread


def _message(thread: MagicMock, *, webhook: bool = False) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = 42
    msg.channel = thread
    msg.content = "Factorio 第一サーバを新しいデータで開始してください"
    msg.attachments = []
    msg.reference = None
    msg.webhook_id = 123 if webhook else None
    msg.author = MagicMock()
    msg.author.bot = False
    msg.author.display_name = "yousan"
    msg.type = discord.MessageType.default
    msg.add_reaction = AsyncMock()
    return msg


def _cog(tmp_path: Path, *, tomb: SessionRecord | None, bound: bool = True):
    """片付け済みのスレッド。ディスクには何も残っていない（#700 の再接続は効かない）。"""
    from c_lord.cogs.claude_chat import ClaudeChatCog

    bot = MagicMock()
    bot.channel_id = CHANNEL_ID
    bot.settings_repo = None
    bot.user = MagicMock()
    bot.user.id = 777
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.get_swept = AsyncMock(return_value=tomb)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    cog = ClaudeChatCog(bot=bot, repo=repo, runner=runner)

    sdm = MagicMock()
    sdm.base_dir = str(tmp_path / "sessions" / str(CHANNEL_ID))
    cog._resolve_session_dir_manager = AsyncMock(  # type: ignore[method-assign]
        return_value=sdm if bound else None
    )
    tmux = MagicMock()
    cog._resolve_tmux_manager = AsyncMock(  # type: ignore[method-assign]
        return_value=tmux if bound else None
    )
    cog._thread_binding_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]
    cog._projects_root = tmp_path / "projects"
    cog._run_claude = AsyncMock()  # type: ignore[method-assign]
    return cog


async def _drain(cog) -> None:
    """``_handle_thread_reply`` は ``_run_claude`` を task に投げる — 終わるのを待つ。"""
    for task in list(cog._active_tasks.values()):
        await task


def _sent(thread: MagicMock) -> list[str]:
    return [str(c.args[0]) for c in thread.send.await_args_list if c.args]


class TestSweptThreadStartsFresh:
    """AC1 / AC3 / AC4: 墓標のあるスレッドは、断らずに新しく始まる。"""

    @pytest.mark.asyncio
    async def test_says_it_starts_a_new_conversation(self, tmp_path) -> None:
        cog = _cog(tmp_path, tomb=_tomb())
        thread = _thread()

        await cog._handle_untracked_thread(_message(thread), thread)
        await _drain(cog)

        assert FRESH_START_NOTICE in _sent(thread)
        assert not any("届いていません" in s for s in _sent(thread))

    @pytest.mark.asyncio
    async def test_runs_the_message(self, tmp_path) -> None:
        cog = _cog(tmp_path, tomb=_tomb())
        thread = _thread()
        message = _message(thread)

        await cog._handle_untracked_thread(message, thread)
        await _drain(cog)

        cog._run_claude.assert_awaited_once()
        assert cog._run_claude.await_args.args[0] is message
        assert cog._run_claude.await_args.args[1] is thread

    @pytest.mark.asyncio
    async def test_no_warning_reaction(self, tmp_path) -> None:
        """⚠️ は「届いていない」の印。実行するのに付けたら嘘になる。"""
        cog = _cog(tmp_path, tomb=_tomb())
        thread = _thread()
        message = _message(thread)

        await cog._handle_untracked_thread(message, thread)
        await _drain(cog)

        message.add_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_continue_the_old_conversation(self, tmp_path) -> None:
        """AC3: 墓標に session_id が残っていても、それで --continue / --resume しない。"""
        cog = _cog(tmp_path, tomb=_tomb())
        thread = _thread()

        await cog._handle_untracked_thread(_message(thread), thread)
        await _drain(cog)

        kwargs = cog._run_claude.await_args.kwargs
        assert kwargs["session_id"] is None
        assert kwargs["try_continue"] is False

    @pytest.mark.asyncio
    async def test_a_leftover_window_is_closed_before_starting(self, tmp_path) -> None:
        """AC3: スイープは tmux の窓までは閉じない。古い Claude が窓に残っていたら、
        そこへ打ち込むのは前の会話の続き（しかも消えたディレクトリの中）になる。"""
        cog = _cog(tmp_path, tomb=_tomb())
        tmux = await cog._resolve_tmux_manager(CHANNEL_ID, thread_id=THREAD_ID)
        thread = _thread()

        await cog._handle_untracked_thread(_message(thread), thread)
        await _drain(cog)

        tmux.kill_session.assert_called_once_with(THREAD_ID)

    @pytest.mark.asyncio
    async def test_first_prompt_mentions_discord_read(self, tmp_path) -> None:
        """AC4: 読むかどうかは Claude 任せ。読める、という事実だけ添える。"""
        cog = _cog(tmp_path, tomb=_tomb())
        thread = _thread()
        message = _message(thread)

        await cog._handle_untracked_thread(message, thread)
        await _drain(cog)

        prompt = cog._run_claude.await_args.args[2]
        assert fresh_start_preamble(THREAD_ID) in prompt
        assert message.content in prompt
        assert prompt.index(fresh_start_preamble(THREAD_ID)) < prompt.index(message.content)

    @pytest.mark.asyncio
    async def test_the_preamble_is_only_added_once(self, tmp_path) -> None:
        """2通目以降はふつうの返信 — 一行は最初の依頼にだけ付く。"""
        cog = _cog(tmp_path, tomb=_tomb())
        thread = _thread()

        await cog._handle_untracked_thread(_message(thread), thread)
        await _drain(cog)
        await cog._handle_thread_reply(_message(thread))
        await _drain(cog)

        second = cog._run_claude.await_args_list[1].args[2]
        assert "discord-read" not in second


class TestUntrackedThreadStartsFresh:
    """AC2: DB の行ごと消えたスレッドも、紐付けがあれば同じく新しく始まる。"""

    @pytest.mark.asyncio
    async def test_no_row_but_bound_starts_fresh(self, tmp_path) -> None:
        cog = _cog(tmp_path, tomb=None)
        cog._thread_binding_exists = AsyncMock(return_value=True)  # type: ignore[method-assign]
        thread = _thread()

        await cog._handle_untracked_thread(_message(thread), thread)
        await _drain(cog)

        assert FRESH_START_NOTICE in _sent(thread)
        cog._run_claude.assert_awaited_once()
        assert cog._run_claude.await_args.kwargs["try_continue"] is False


class TestUnboundThreadIsUnchanged:
    """AC5: 何を clone するか分からないときは、従来どおり案内だけ。"""

    @pytest.mark.asyncio
    async def test_unbound_swept_thread_only_gets_the_notice(self, tmp_path) -> None:
        cog = _cog(tmp_path, tomb=_tomb(), bound=False)
        thread = _thread()
        message = _message(thread)

        await cog._handle_untracked_thread(message, thread)
        await _drain(cog)

        cog._run_claude.assert_not_awaited()
        assert FRESH_START_NOTICE not in _sent(thread)
        assert any("片付け済み" in s for s in _sent(thread))
        message.add_reaction.assert_awaited_once()


class TestWhoCanStartIt:
    """AC7 / AC8: 起動してよいのは、ふつうに投稿が通る人・担当の c-lord だけ。"""

    @pytest.mark.asyncio
    async def test_unauthorized_author_starts_nothing(self, tmp_path) -> None:
        cog = _cog(tmp_path, tomb=_tomb())
        cog._is_message_authorized = MagicMock(return_value=False)  # type: ignore[method-assign]
        thread = _thread()
        message = _message(thread)

        await cog.on_message(message)
        await _drain(cog)

        cog._run_claude.assert_not_awaited()
        thread.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_another_instances_thread_starts_nothing(self, tmp_path) -> None:
        cog = _cog(tmp_path, tomb=_tomb())
        cog._is_our_thread = AsyncMock(return_value=False)  # type: ignore[method-assign]
        thread = _thread()

        await cog._handle_untracked_thread(_message(thread), thread)
        await _drain(cog)

        cog._run_claude.assert_not_awaited()
        thread.send.assert_not_awaited()


class TestClordInSweptThreadStartsFresh:
    """``/clord`` もスレッド内では同じ答えを返す — 投稿と食い違わせない (#538 の教訓)。"""

    @pytest.mark.asyncio
    async def test_clord_carries_on_with_a_fresh_session(self, tmp_path) -> None:
        import c_lord.cogs.claude_chat as mod

        cog = _cog(tmp_path, tomb=_tomb())
        thread = _thread()
        respond = AsyncMock()
        orig = mod.foreign_owner_notice_for
        mod.foreign_owner_notice_for = AsyncMock(return_value=None)  # type: ignore[assignment]
        try:
            carried_on = await cog._handle_clord_without_session(thread, CHANNEL_ID, respond)
        finally:
            mod.foreign_owner_notice_for = orig  # type: ignore[assignment]

        assert carried_on is True
        assert FRESH_START_NOTICE in _sent(thread)


class TestHintMatchesBehaviour:
    def test_untracked_hint_says_a_message_starts_fresh(self) -> None:
        """/tmux-screenshot・/resync の案内も、投稿したときの実際の動きと揃える。"""
        text = stopped_hint(ThreadResume.UNTRACKED)
        assert "新しい会話" in text
        assert "復元できません" not in text


class TestWebhookStartsFresh:
    """#862 追加要件（2026-10-08）: webhook の普通の文でも新しく始める。

    Claude どうしの発注（タスク管理スレッドが webhook ``?thread_id=`` で古い
    スレッドに指示を流す）や OpenClaw の人格からの依頼が、黙って捨てられないように。
    """

    @pytest.mark.asyncio
    async def test_a_webhook_message_in_a_swept_thread_starts_fresh(self, tmp_path) -> None:
        cog = _cog(tmp_path, tomb=_tomb())
        thread = _thread()
        message = _message(thread, webhook=True)

        await cog._handle_untracked_thread(message, thread)
        await _drain(cog)

        assert FRESH_START_NOTICE in _sent(thread)
        cog._run_claude.assert_awaited_once()
        assert cog._run_claude.await_args.args[0] is message
        assert cog._run_claude.await_args.kwargs["try_continue"] is False
        assert fresh_start_preamble(THREAD_ID) in cog._run_claude.await_args.args[2]
        message.add_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_webhook_message_with_no_row_but_a_binding_starts_fresh(self, tmp_path) -> None:
        cog = _cog(tmp_path, tomb=None)
        cog._thread_binding_exists = AsyncMock(return_value=True)  # type: ignore[method-assign]
        thread = _thread()

        await cog._handle_untracked_thread(_message(thread, webhook=True), thread)
        await _drain(cog)

        assert FRESH_START_NOTICE in _sent(thread)
        cog._run_claude.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_webhook_message_reattaches_a_surviving_checkout(self, tmp_path) -> None:
        """#700 の再接続も webhook で効く（新しく始めるより先に、残っているものを使う）。"""
        (tmp_path / "sessions" / str(CHANNEL_ID) / str(THREAD_ID)).mkdir(parents=True)
        cog = _cog(tmp_path, tomb=_tomb())
        cog._collect_thread_history = AsyncMock(return_value=[])  # type: ignore[method-assign]
        thread = _thread()

        await cog._handle_untracked_thread(_message(thread, webhook=True), thread)
        await _drain(cog)

        cog.repo.save.assert_awaited_once()
        assert FRESH_START_NOTICE not in _sent(thread)
        cog._run_claude.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unbound_thread_stays_silent_for_webhooks(self, tmp_path) -> None:
        """紐付けが無ければ何も起動せず、webhook には案内も ⚠️ も出さない（#556）。"""
        cog = _cog(tmp_path, tomb=_tomb(), bound=False)
        thread = _thread()
        message = _message(thread, webhook=True)

        await cog._handle_untracked_thread(message, thread)
        await _drain(cog)

        cog._run_claude.assert_not_awaited()
        thread.send.assert_not_awaited()
        message.add_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_message_another_cog_claims_starts_nothing(self, tmp_path) -> None:
        """webhook_trigger / auto_upgrade の決まった形は、そちらの Cog のもの。"""
        cog = _cog(tmp_path, tomb=_tomb())
        trigger_cog = MagicMock()
        trigger_cog.claims_message = MagicMock(return_value=True)
        cog.bot.cogs = {"WebhookTriggerCog": trigger_cog}
        thread = _thread()
        message = _message(thread, webhook=True)

        await cog._handle_untracked_thread(message, thread)
        await _drain(cog)

        trigger_cog.claims_message.assert_called_once_with(message)
        cog._run_claude.assert_not_awaited()
        thread.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unclaimed_message_is_not_held_back_by_other_cogs(self, tmp_path) -> None:
        cog = _cog(tmp_path, tomb=_tomb())
        trigger_cog = MagicMock()
        trigger_cog.claims_message = MagicMock(return_value=False)
        cog.bot.cogs = {"WebhookTriggerCog": trigger_cog, "Plain": object()}
        thread = _thread()

        await cog._handle_untracked_thread(_message(thread, webhook=True), thread)
        await _drain(cog)

        cog._run_claude.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_webhook_text_command_runs_as_a_command_not_a_conversation(
        self, tmp_path
    ) -> None:
        """``!close-workspace`` などは process_commands の担当。会話は始めない。"""
        cog = _cog(tmp_path, tomb=_tomb())
        ctx = MagicMock()
        ctx.valid = True
        cog.bot.get_context = AsyncMock(return_value=ctx)
        thread = _thread()
        message = _message(thread, webhook=True)
        message.content = "!close-workspace"

        await cog.on_message(message)
        await _drain(cog)

        cog._run_claude.assert_not_awaited()
        thread.send.assert_not_awaited()
