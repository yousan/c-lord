"""再接続で過去ログを書き出したら、そのことを Claude にも一行で伝える — #881.

作業フォルダだけ残っているスレッドに投稿すると（#538 / #700 / #862）、c-lord は
「🔗 …このスレッドの過去ログを引き継いで再接続します」と言い、Discord の会話を
``.claude/clord-thread-history.md`` に書き出す。ところが Claude に渡る最初の入力は
依頼文だけで、ファイルがあることは一言も伝わっていなかった（10/9 こぷー管理）。

#862 の合意どおり「最初の依頼に一行添え、読むかどうかは Claude 任せ」にする。
「最初の」は *まだ Claude の会話が始まっていない* こと — 書き出した過去ログより
新しい transcript がまだ無いこと — で判定するので、手動の再接続のあと bot が
再起動しても、次のメッセージに一行が付く。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.database.repository import SWEPT_REASON, SessionRecord
from c_lord.discord_ui.authorization import Authorizer
from c_lord.session_reattach import (
    HISTORY_FILENAME,
    Recovery,
    history_handoff_pending,
    history_handoff_preamble,
)

THREAD_ID = 1530372563858096321
CHANNEL_ID = 999


def _workdir(tmp_path: Path) -> Path:
    return tmp_path / "sessions" / str(CHANNEL_ID) / str(THREAD_ID)


def _project_dir(tmp_path: Path) -> Path:
    work = str(_workdir(tmp_path))
    return tmp_path / "projects" / work.replace("/", "-").replace(".", "-")


def _transcript(tmp_path: Path, *, mtime: float | None = None) -> Path:
    pdir = _project_dir(tmp_path)
    pdir.mkdir(parents=True, exist_ok=True)
    jsonl = pdir / "1fcfb524-aaaa-bbbb-cccc-ddddeeeeffff.jsonl"
    jsonl.write_text('{"type":"system"}\n', encoding="utf-8")
    if mtime is not None:
        os.utime(jsonl, (mtime, mtime))
    return jsonl


def _history(tmp_path: Path, *, mtime: float | None = None) -> Path:
    path = _workdir(tmp_path) / HISTORY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# history\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestPending:
    """一行を付けるかどうかの判定（ディスクだけを見る）。"""

    def test_history_and_no_transcript_is_pending(self, tmp_path) -> None:
        _history(tmp_path)
        assert history_handoff_pending(str(_workdir(tmp_path)), projects_root=tmp_path / "projects")

    def test_no_history_is_not_pending(self, tmp_path) -> None:
        _workdir(tmp_path).mkdir(parents=True)
        assert not history_handoff_pending(
            str(_workdir(tmp_path)), projects_root=tmp_path / "projects"
        )

    def test_a_conversation_started_after_the_export_is_not_pending(self, tmp_path) -> None:
        """一行を受け取った最初のターンで transcript ができる — 2通目からは付かない。"""
        _history(tmp_path, mtime=1_000_000)
        _transcript(tmp_path, mtime=2_000_000)
        assert not history_handoff_pending(
            str(_workdir(tmp_path)), projects_root=tmp_path / "projects"
        )

    def test_a_transcript_older_than_the_export_is_still_pending(self, tmp_path) -> None:
        _transcript(tmp_path, mtime=1_000_000)
        _history(tmp_path, mtime=2_000_000)
        assert history_handoff_pending(str(_workdir(tmp_path)), projects_root=tmp_path / "projects")

    def test_unusable_path_is_not_pending(self) -> None:
        assert not history_handoff_pending("", projects_root=None)

    def test_preamble_names_the_file_and_the_thread(self) -> None:
        line = history_handoff_preamble(THREAD_ID)
        assert HISTORY_FILENAME in line
        assert "discord-read" in line
        assert str(THREAD_ID) in line


# ---------------------------------------------------------------------------
# cog の経路


def _tomb() -> SessionRecord:
    return SessionRecord(
        thread_id=THREAD_ID,
        session_id="1fcfb524-aaaa-bbbb-cccc-ddddeeeeffff",
        working_dir=None,
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
    thread.owner_id = 777
    thread.name = "こぷー管理"
    thread.send = AsyncMock(return_value=MagicMock())
    thread.edit = AsyncMock()
    return thread


def _message(
    thread: MagicMock, *, webhook: bool = False, content: str = "続きをお願い"
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = 42
    msg.channel = thread
    msg.content = content
    msg.attachments = []
    msg.reference = None
    msg.webhook_id = 123 if webhook else None
    msg.author = MagicMock()
    msg.author.bot = False
    msg.author.display_name = "yousan"
    msg.type = discord.MessageType.default
    msg.add_reaction = AsyncMock()
    return msg


def _cog(tmp_path: Path):
    """``repo`` は保存した行をそのまま返す（再接続 → 次の投稿を通しで見るため）。"""
    from c_lord.cogs.claude_chat import ClaudeChatCog

    bot = MagicMock()
    bot.channel_id = CHANNEL_ID
    bot.settings_repo = None
    bot.user = MagicMock()
    bot.user.id = 777
    bot.get_cog = MagicMock(return_value=None)
    rows: dict[int, SessionRecord] = {}

    async def save(*, thread_id: int, session_id: str, working_dir: str | None = None, **_) -> None:
        rows[thread_id] = SessionRecord(
            thread_id=thread_id,
            session_id=session_id,
            working_dir=working_dir,
            model=None,
            origin="discord",
            summary=None,
            created_at="2026-10-09 02:52:40",
            last_used_at="2026-10-09 02:52:40",
        )

    async def get(thread_id: int) -> SessionRecord | None:
        return rows.get(thread_id)

    repo = MagicMock()
    repo.get = AsyncMock(side_effect=get)
    repo.get_swept = AsyncMock(return_value=_tomb())
    repo.save = AsyncMock(side_effect=save)
    cog = ClaudeChatCog(
        bot=bot, repo=repo, runner=MagicMock(), authorizer=Authorizer(allow_anyone=True)
    )
    sdm = MagicMock()
    sdm.base_dir = str(tmp_path / "sessions" / str(CHANNEL_ID))
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    tmux = MagicMock()
    tmux.is_claude_running = MagicMock(return_value=False)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    cog._thread_binding_exists = AsyncMock(return_value=False)  # type: ignore[method-assign]
    cog._projects_root = tmp_path / "projects"
    (tmp_path / "projects").mkdir(exist_ok=True)
    cog._collect_thread_history = AsyncMock(  # type: ignore[method-assign]
        return_value=[("yousan", "2026-10-01 10:00", "こぷーのサーバを見て")]
    )
    cog._run_claude = AsyncMock()  # type: ignore[method-assign]
    return cog


async def _drain(cog) -> None:
    for task in list(cog._active_tasks.values()):
        await task


def _prompt(cog) -> str:
    return cog._run_claude.await_args.args[2]


@pytest.mark.parametrize("webhook", [False, True], ids=["human", "webhook"])
async def test_reattach_on_arrival_tells_claude_about_the_history(tmp_path, webhook) -> None:
    """AC1: WORKDIR の再接続（到着時・#700）で、最初の入力に一行が付く。webhook（#873）も。"""
    _workdir(tmp_path).mkdir(parents=True)
    cog = _cog(tmp_path)
    thread = _thread()
    message = _message(thread, webhook=webhook)

    await cog._handle_untracked_thread(message, thread)
    await _drain(cog)

    assert (_workdir(tmp_path) / HISTORY_FILENAME).is_file()
    prompt = _prompt(cog)
    assert history_handoff_preamble(THREAD_ID) in prompt
    assert prompt.index(history_handoff_preamble(THREAD_ID)) < prompt.index(message.content)


async def test_manual_reattach_then_next_message_gets_the_line(tmp_path) -> None:
    """AC2: 手動の再接続（/workspace-start 等 → _reattach_thread）のあとの最初のメッセージ。"""
    _workdir(tmp_path).mkdir(parents=True)
    cog = _cog(tmp_path)
    thread = _thread()

    plan = await cog._reattach_thread(thread)
    assert plan.kind is Recovery.WORKDIR

    await cog._handle_thread_reply(_message(thread))
    await _drain(cog)

    assert history_handoff_preamble(THREAD_ID) in _prompt(cog)


async def test_clord_in_a_reattached_thread_gets_the_line(tmp_path) -> None:
    """AC2: ``/clord`` から再接続して続けた場合も同じ一行。"""
    import c_lord.cogs.claude_chat as mod

    _workdir(tmp_path).mkdir(parents=True)
    cog = _cog(tmp_path)
    thread = _thread()
    orig = mod.foreign_owner_notice_for
    mod.foreign_owner_notice_for = AsyncMock(return_value=None)  # type: ignore[assignment]
    try:
        await cog._clord_impl(
            channel=thread,
            channel_id_fallback=None,
            user=MagicMock(),
            prompt="続きをお願い",
            respond=AsyncMock(),
            ack=AsyncMock(),
        )
    finally:
        mod.foreign_owner_notice_for = orig  # type: ignore[assignment]

    run = cog._run_claude
    assert isinstance(run, AsyncMock)
    run.assert_awaited_once()
    assert run.await_args is not None
    assert history_handoff_preamble(THREAD_ID) in run.await_args.kwargs["prompt"]


async def test_full_recovery_does_not_get_the_line(tmp_path) -> None:
    """AC3: 会話の履歴が残っている（FULL）なら --continue で続くので、一行は付けない。
    以前の WORKDIR 再接続で書いた古い過去ログが残っていても同じ。"""
    _history(tmp_path, mtime=1_000_000)
    _transcript(tmp_path, mtime=2_000_000)
    cog = _cog(tmp_path)
    thread = _thread()

    await cog._handle_untracked_thread(_message(thread), thread)
    await _drain(cog)

    assert "clord-thread-history" not in _prompt(cog)


async def test_only_the_first_turn_gets_the_line(tmp_path) -> None:
    """2通目は付かない — 最初のターンで Claude の transcript ができているので。"""
    _workdir(tmp_path).mkdir(parents=True)
    cog = _cog(tmp_path)
    thread = _thread()
    await cog._handle_untracked_thread(_message(thread), thread)
    await _drain(cog)
    assert history_handoff_preamble(THREAD_ID) in _prompt(cog)

    # 最初のターンで Claude が transcript を書き始めた
    _transcript(tmp_path, mtime=(_workdir(tmp_path) / HISTORY_FILENAME).stat().st_mtime + 5)
    await cog._handle_thread_reply(_message(thread, content="次の依頼"))
    await _drain(cog)

    assert "clord-thread-history" not in _prompt(cog)
