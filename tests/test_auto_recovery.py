"""c-lord が自分で止めたワークスペースは、投稿だけで戻る — #700.

久しぶりのスレッドに投稿したとき、いままで起きていたのはこれ:

* 7日 無操作で自動停止 → 「⏹️ このスレッドは終了しています」＋ **▶️ 再開する**
* 30日 無操作で ``sessions`` の行が消えた → ⚠️ ＋ 壁のような案内文 ＋ **🔗 再接続する**

どちらも **利用者は止めた覚えがない**。にもかかわらず、戻るのに「案内文を読む →
ボタンを探す → 押す」が要った。しかも c-lord 自身の停止通知は
「このスレッドに投稿しても再開できます」と約束していた — 約束する側と実装する側が
食い違う、#538 とまったく同じ形。

線は「**誰が止めたか**」で引く（``docs/specs/session-close.md`` の
「自分で終わらせたなら、勝手に動き出さないでほしい」を守るため）:

* c-lord が自動で止めた（``closed_reason="idle"`` / 行の掃除）→ **黙って戻す**
* 利用者が ``/workspace-stop`` で止めた → **ボタンのまま**

そして戻すときに出すのは **1行だけ**。棚卸し（何が残っているか）は停止通知側の
役目で、復帰通知には要らない。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.database.repository import SessionRecord

THREAD_ID = 1508626302813601843


# ── helpers ──────────────────────────────────────────────────────────────────


def _record(*, closed_at: str | None = None, closed_reason: str | None = None) -> SessionRecord:
    return SessionRecord(
        thread_id=555,
        session_id="sess-abc",
        working_dir="/tmp/x",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-08-18 10:00:00",
        last_used_at="2026-08-18 11:00:00",
        topic="認証リファクタ",
        closed_at=closed_at,
        closed_reason=closed_reason,
    )


def _thread(thread_id: int = 555) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = 999
    thread.owner_id = 777
    thread.name = "W3 │ #404 認証リファクタ"
    thread.edit = AsyncMock()
    thread.send = AsyncMock()
    thread.history = MagicMock()
    return thread


def _message(thread: MagicMock) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = 1
    msg.channel = thread
    msg.content = "続きをお願い"
    msg.attachments = []
    msg.reference = None
    msg.webhook_id = None
    msg.author = MagicMock()
    msg.author.bot = False
    msg.add_reaction = AsyncMock()
    return msg


# ── 7日で自動停止したワークスペース ──────────────────────────────────────────


class TestIdleStoppedWorkspaceComesBackOnItsOwn:
    """AC1 / AC2: 自動停止はボタン無しで戻り、手動停止は従来どおり。"""

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
        cog._apply_thread_naming = AsyncMock()
        return cog

    @pytest.mark.asyncio
    async def test_an_idle_stop_reopens_and_runs_the_message(self) -> None:
        """AC1: 押させない。投稿がそのまま実行される。"""
        cog = self._cog(_record(closed_at="2026-08-18 12:00:00", closed_reason="idle"))
        thread = _thread()

        await cog._handle_thread_reply(_message(thread))

        cog.repo.set_closed.assert_awaited_once_with(555, False)
        assert cog._run_claude.called

    @pytest.mark.asyncio
    async def test_an_idle_stop_offers_no_button(self) -> None:
        """AC1: 「▶️ 再開する」も終了案内の embed も出さない。"""
        cog = self._cog(_record(closed_at="2026-08-18 12:00:00", closed_reason="idle"))
        thread = _thread()

        await cog._handle_thread_reply(_message(thread))

        for call in thread.send.await_args_list:
            assert call.kwargs.get("view") is None
            assert call.kwargs.get("embed") is None

    @pytest.mark.asyncio
    async def test_a_manual_stop_still_holds_the_message_behind_the_button(self) -> None:
        """AC2: 自分で止めたものが勝手に動き出さない（session-close.md の決定）。"""
        cog = self._cog(_record(closed_at="2026-08-18 12:00:00", closed_reason="manual"))
        thread = _thread()

        await cog._handle_thread_reply(_message(thread))

        cog._run_claude.assert_not_called()
        thread.send.assert_awaited_once()
        assert thread.send.await_args.kwargs.get("view") is not None

    @pytest.mark.asyncio
    async def test_a_legacy_row_without_a_reason_is_treated_as_manual(self) -> None:
        """AC2: #574 より前に閉じた行は ``closed_reason`` を持たない。当時
        自動停止は存在せず、閉じられる唯一の道が手動だったので、手動として扱う
        — 迷ったら「勝手に動かさない」側に倒す。"""
        cog = self._cog(_record(closed_at="2026-08-18 12:00:00", closed_reason=None))
        thread = _thread()

        await cog._handle_thread_reply(_message(thread))

        cog._run_claude.assert_not_called()
        assert thread.send.await_args.kwargs.get("view") is not None


# ── 30日で記録が消えたワークスペース ─────────────────────────────────────────


def _seed(tmp_path: Path, *, checkout: bool, transcript: bool) -> Path:
    """``THREAD_ID`` のディスク状態を作り、projects root を返す。"""
    base = tmp_path / "sessions" / "9999"
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    work = base / str(THREAD_ID)
    if checkout:
        work.mkdir(parents=True, exist_ok=True)
        (work / "draft.md").write_text("# 書きかけの記事\n", encoding="utf-8")
    if transcript:
        slug = str(work).replace("/", "-").replace(".", "-")
        pdir = projects / slug
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "1fcfb524-aaaa-bbbb-cccc-ddddeeeeffff.jsonl").write_text(
            '{"type":"system"}\n', encoding="utf-8"
        )
    return projects


def _reattach_cog(tmp_path):
    from c_lord.cogs.claude_chat import ClaudeChatCog

    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    bot.user = MagicMock()
    bot.user.id = 777
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    cog = ClaudeChatCog(bot=bot, repo=repo, runner=runner)
    sdm = MagicMock()
    sdm.base_dir = str(tmp_path / "sessions" / "9999")
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    cog._thread_binding_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]
    cog._projects_root = tmp_path / "projects"
    cog._handle_thread_reply = AsyncMock()  # type: ignore[method-assign]

    async def _no_history(_thread, limit=None):
        return []

    cog._collect_thread_history = _no_history  # type: ignore[method-assign]
    return cog


class TestSweptWorkspaceReattachesOnItsOwn:
    """AC3 / AC4: 復元できるものが残っていれば黙って繋ぎ直し、投稿を実行する。"""

    @pytest.mark.asyncio
    async def test_a_recoverable_thread_is_reattached_without_a_button(self, tmp_path) -> None:
        _seed(tmp_path, checkout=True, transcript=True)
        cog = _reattach_cog(tmp_path)
        thread = _thread(THREAD_ID)

        await cog._handle_untracked_thread(_message(thread), thread)

        cog.repo.save.assert_awaited_once()
        thread.send.assert_awaited_once()
        assert thread.send.await_args.kwargs.get("view") is None

    @pytest.mark.asyncio
    async def test_the_message_that_hit_the_swept_thread_is_run(self, tmp_path) -> None:
        """AC3: 打ち直しを要求しない — ボタンがやっていたことと同じ。"""
        _seed(tmp_path, checkout=True, transcript=True)
        cog = _reattach_cog(tmp_path)
        thread = _thread(THREAD_ID)
        message = _message(thread)

        await cog._handle_untracked_thread(message, thread)

        cog._handle_thread_reply.assert_awaited_once_with(message)

    @pytest.mark.asyncio
    async def test_no_warning_reaction_when_the_message_is_actually_run(self, tmp_path) -> None:
        """⚠️ は「届いていない」の印。届いたのに付けるのは嘘になる。"""
        _seed(tmp_path, checkout=True, transcript=True)
        cog = _reattach_cog(tmp_path)
        thread = _thread(THREAD_ID)
        message = _message(thread)

        await cog._handle_untracked_thread(message, thread)

        message.add_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_notice_is_one_line(self, tmp_path) -> None:
        """AC5: 「本当に小さく」— 1メッセージ・1行。"""
        _seed(tmp_path, checkout=True, transcript=True)
        cog = _reattach_cog(tmp_path)
        thread = _thread(THREAD_ID)

        await cog._handle_untracked_thread(_message(thread), thread)

        said = str(thread.send.await_args.args[0])
        assert "\n" not in said.strip()

    @pytest.mark.asyncio
    async def test_a_workdir_only_thread_is_reattached_too(self, tmp_path) -> None:
        """transcript が消えていても作業は残っている（実測された Qiita の形）。"""
        _seed(tmp_path, checkout=True, transcript=False)
        cog = _reattach_cog(tmp_path)
        thread = _thread(THREAD_ID)
        message = _message(thread)

        await cog._handle_untracked_thread(message, thread)

        cog.repo.save.assert_awaited_once()
        cog._handle_thread_reply.assert_awaited_once_with(message)
        said = str(thread.send.await_args.args[0])
        assert "\n" not in said.strip()
        # 会話は戻らない、という事実だけは言う。
        assert "過去ログ" in said or "履歴" in said

    @pytest.mark.asyncio
    async def test_nothing_on_disk_is_unchanged(self, tmp_path) -> None:
        """AC4: 何も残っていないときは従来どおり — ⚠️ と、復元できない案内。"""
        _seed(tmp_path, checkout=False, transcript=False)
        cog = _reattach_cog(tmp_path)
        cog._thread_binding_exists = AsyncMock(return_value=True)  # まだ c-lord のもの
        thread = _thread(THREAD_ID)
        message = _message(thread)

        await cog._handle_untracked_thread(message, thread)

        message.add_reaction.assert_called_once()
        cog._handle_thread_reply.assert_not_awaited()
        cog.repo.save.assert_not_awaited()
        assert "/clord" in str(thread.send.await_args.args[0])


# ── 文言 ─────────────────────────────────────────────────────────────────────


class TestWording:
    def test_an_auto_stopped_resume_says_why_it_was_stopped(self) -> None:
        """AC5: 「落ちていた」でも「あなたが止めた」でもない、第3の事実。"""
        from c_lord.session_resume import resume_notice

        notice = resume_notice(slept=False, reopened=False, auto_stopped=True)

        assert notice is not None
        assert "\n" not in notice.strip()
        assert "停止" in notice

    def test_an_already_announced_resume_says_nothing_twice(self) -> None:
        """再接続の1行を出したあとに「落ちていたので」を足すと矛盾する。"""
        from c_lord.session_resume import resume_notice

        assert resume_notice(slept=False, reopened=False, already_announced=True) is None

    def test_a_crash_still_says_it_crashed(self) -> None:
        """#464: 本当に落ちたときの文言は変えない。"""
        from c_lord.session_resume import resume_notice

        notice = resume_notice(slept=False, reopened=False)
        assert notice is not None
        assert "落ちて" in notice

    def test_the_reattach_line_is_one_line_per_outcome(self) -> None:
        from c_lord.session_reattach import Plan, Recovery, auto_reattach_notice

        for kind in (Recovery.FULL, Recovery.WORKDIR):
            line = auto_reattach_notice(Plan(kind, working_dir="/tmp/x"))
            assert "\n" not in line.strip()
            assert line.strip()
