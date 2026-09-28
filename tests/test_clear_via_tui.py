"""``/clear`` types ``/clear`` into Claude Code instead of killing it (#803).

Before #803 the command killed the tmux window and stamped ``session_id = ''``
on the row as a "start fresh next time" mark.  ``sessions.session_id`` is
UNIQUE and SQLite does not let two rows share ``''``, so the second thread on a
host to run ``/clear`` always failed with ``IntegrityError`` — observed in
production on 2026-09-24 and four times on 2026-09-28.

Now the command does what ``/compact`` already does (#278): it types the TUI's
own ``/clear`` with ``send_literal``.  The row is not touched, so the constraint
has nothing to trip on.  What *does* have to move is c-lord's record of which
transcript is this thread's (#773): Claude Code answers ``/clear`` with a brand
new ``<uuid>.jsonl`` that opens with ``<command-name>/clear</command-name>``
(measured on CLI 2.1.282), and a mirror left on the old uuid would never post
the post-clear answers — nor would a later ``--resume`` open the cleared
conversation.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs import claude_chat as claude_chat_module
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.database.models import init_db
from c_lord.database.repository import SessionRecord, SessionRepository
from c_lord.discord_ui.authorization import Authorizer
from c_lord.transcript.claim import (
    adopt_cleared_session,
    list_transcripts,
    read_claim,
    write_claim,
)

PARENT_ID = 999


def _clear_transcript(project_dir: Path, session_id: str | None = None) -> str:
    """Write the jsonl Claude Code creates when ``/clear`` runs (CLI 2.1.282 shape)."""
    session_id = session_id or str(uuid.uuid4())
    lines = [
        {"type": "mode", "sessionId": session_id},
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": str(uuid.uuid4()),
            "message": {
                "role": "user",
                "content": "<command-name>/clear</command-name>\n"
                "            <command-message>clear</command-message>\n"
                "            <command-args></command-args>",
            },
        },
    ]
    (project_dir / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return session_id


def _plain_transcript(project_dir: Path, text: str = "hello") -> str:
    session_id = str(uuid.uuid4())
    line = {
        "type": "user",
        "sessionId": session_id,
        "message": {"role": "user", "content": text},
    }
    (project_dir / f"{session_id}.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    return session_id


# ── claim: adopting the transcript /clear produced ─────────────────────────


class TestAdoptClearedSession:
    def test_adopts_the_new_clear_transcript(self, tmp_path: Path) -> None:
        old = str(uuid.uuid4())
        write_claim(tmp_path, old)
        (tmp_path / f"{old}.jsonl").write_text("{}\n")
        before = list_transcripts(tmp_path)

        new = _clear_transcript(tmp_path)

        assert adopt_cleared_session(tmp_path, before) == new
        assert read_claim(tmp_path) == new

    def test_ignores_a_file_that_was_already_there(self, tmp_path: Path) -> None:
        """A /clear transcript from an earlier clear is not *this* clear's successor."""
        _clear_transcript(tmp_path)
        before = list_transcripts(tmp_path)

        assert adopt_cleared_session(tmp_path, before) is None
        assert read_claim(tmp_path) is None

    def test_ignores_a_new_file_that_is_not_a_clear(self, tmp_path: Path) -> None:
        """A ``claude -p`` sub-invocation writing meanwhile must not take the thread (#627)."""
        before = list_transcripts(tmp_path)
        _plain_transcript(tmp_path, "what does <command-name>/clear</command-name> do?")

        assert adopt_cleared_session(tmp_path, before) is None

    def test_missing_project_dir_is_none(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        assert list_transcripts(missing) == set()
        assert adopt_cleared_session(missing, set()) is None


# ── the cog ────────────────────────────────────────────────────────────────


def _record(thread_id: int, *, closed: bool = False) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id=f"tmux-{thread_id}",
        working_dir="/tmp/x",
        model=None,
        origin="chat",
        summary=None,
        created_at="2026-09-28 00:00:00",
        last_used_at="2026-09-28 00:00:00",
        closed_at="2026-09-28 00:00:00" if closed else None,
    )


def _cog(tmux_manager: MagicMock, repo: object | None = None) -> ClaudeChatCog:
    bot = MagicMock()
    bot.channel_id = PARENT_ID
    bot.settings_repo = None
    if repo is None:
        repo = MagicMock()
        repo.get = AsyncMock(side_effect=lambda tid: _record(tid))
        repo.reset = AsyncMock(return_value=True)
    cog = ClaudeChatCog(
        bot=bot, repo=repo, runner=MagicMock(), authorizer=Authorizer(allow_anyone=True)
    )
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
    return cog


def _tmux(project_dir: Path, *, running: bool = True) -> MagicMock:
    """A tmux manager whose Enter makes Claude Code write the /clear transcript."""
    tmux_manager = MagicMock()
    tmux_manager.is_claude_running = MagicMock(return_value=running)
    tmux_manager.send_literal = MagicMock(return_value=True)
    tmux_manager.send_input = MagicMock(return_value=True)
    tmux_manager.kill_session = MagicMock(return_value=True)
    tmux_manager.project_dir_for = MagicMock(return_value=project_dir)
    tmux_manager.cleared = []

    def _send_keys(_thread_id: int, key: str) -> bool:
        if key == "Enter":
            tmux_manager.cleared.append(_clear_transcript(project_dir))
        return True

    tmux_manager.send_keys = MagicMock(side_effect=_send_keys)
    return tmux_manager


def _thread(thread_id: int) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = PARENT_ID
    thread.send = AsyncMock()
    return thread


def _ctx(thread_id: int) -> MagicMock:
    ctx = MagicMock()
    ctx.channel = _thread(thread_id)
    ctx.author = MagicMock()
    ctx.message = MagicMock(spec=discord.Message)
    ctx.message.webhook_id = 555
    ctx.message.author = MagicMock(bot=True, id=1)
    ctx.send = AsyncMock()
    return ctx


@pytest.fixture(autouse=True)
def _fast_polls():
    with (
        patch.object(claude_chat_module, "_CLEAR_ADOPT_TIMEOUT", 0.5),
        patch.object(claude_chat_module, "_SLASH_POLL_INTERVAL", 0.01),
    ):
        yield


class TestClearLiveClaude:
    @pytest.mark.asyncio
    async def test_types_clear_into_the_tui(self, tmp_path: Path) -> None:
        """AC1: a live Claude gets ``/clear`` typed at it — nothing is killed or reset."""
        tmux_manager = _tmux(tmp_path)
        cog = _cog(tmux_manager)
        ctx = _ctx(1)

        await cog.clear_text.callback(cog, ctx)

        tmux_manager.send_literal.assert_called_once_with(1, "/clear")
        tmux_manager.send_keys.assert_called_once_with(1, "Enter")
        tmux_manager.send_input.assert_not_called()
        tmux_manager.kill_session.assert_not_called()
        cog.repo.reset.assert_not_called()
        assert "/clear" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_claims_the_new_transcript(self, tmp_path: Path) -> None:
        """AC3: the mirror (and a later ``--resume``) must follow the post-clear jsonl."""
        old = str(uuid.uuid4())
        write_claim(tmp_path, old)
        (tmp_path / f"{old}.jsonl").write_text("{}\n")
        tmux_manager = _tmux(tmp_path)
        cog = _cog(tmux_manager)

        await cog.clear_text.callback(cog, _ctx(1))

        assert read_claim(tmp_path) == tmux_manager.cleared[0]

    @pytest.mark.asyncio
    async def test_no_session_is_refused_plainly(self, tmp_path: Path) -> None:
        tmux_manager = _tmux(tmp_path)
        cog = _cog(tmux_manager)
        cog.repo.get = AsyncMock(return_value=None)
        ctx = _ctx(1)

        await cog.clear_text.callback(cog, ctx)

        tmux_manager.send_literal.assert_not_called()
        ctx.send.assert_called_once()


class TestClearTwoThreads:
    @pytest.mark.asyncio
    async def test_second_thread_clear_does_not_fail(self, tmp_path: Path) -> None:
        """AC5 — the original symptom, against a real SQLite with the UNIQUE index."""
        db_path = str(tmp_path / "sessions.db")
        await init_db(db_path)
        repo = SessionRepository(db_path)
        await repo.save(1, session_id="tmux-1")
        await repo.save(2, session_id="tmux-2")
        (tmp_path / "p1").mkdir()
        (tmp_path / "p2").mkdir()
        dirs = {1: tmp_path / "p1", 2: tmp_path / "p2"}

        for thread_id in (1, 2):
            tmux_manager = _tmux(dirs[thread_id])
            cog = _cog(tmux_manager, repo=repo)
            ctx = _ctx(thread_id)
            await cog.clear_text.callback(cog, ctx)
            tmux_manager.send_literal.assert_called_once_with(thread_id, "/clear")
            assert "/clear" in ctx.send.call_args.args[0]

        assert (await repo.get(1)).session_id == "tmux-1"
        assert (await repo.get(2)).session_id == "tmux-2"


class TestClearStoppedClaude:
    @pytest.mark.asyncio
    async def test_wakes_then_clears(self, tmp_path: Path) -> None:
        """AC2: a slept workspace is restored first, and the restore is said out loud."""
        tmux_manager = _tmux(tmp_path, running=False)
        cog = _cog(tmux_manager)
        order: list[str] = []
        cog.wake_workspace = AsyncMock(side_effect=lambda _t: order.append("wake") or True)
        tmux_manager.send_literal.side_effect = lambda *_a: order.append("send") or True
        ctx = _ctx(1)

        await cog.clear_text.callback(cog, ctx)

        assert order == ["wake", "send"]
        notice = ctx.channel.send.call_args.args[0]
        assert "復元" in notice and "/clear" in notice

    @pytest.mark.asyncio
    async def test_wake_failure_is_reported_and_nothing_sent(self, tmp_path: Path) -> None:
        tmux_manager = _tmux(tmp_path, running=False)
        cog = _cog(tmux_manager)
        cog.wake_workspace = AsyncMock(return_value=False)
        ctx = _ctx(1)

        await cog.clear_text.callback(cog, ctx)

        tmux_manager.send_literal.assert_not_called()
        assert "復元に失敗" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_closed_thread_is_not_woken(self, tmp_path: Path) -> None:
        """終了 is the user's choice (#512) — a slash command must not undo it."""
        tmux_manager = _tmux(tmp_path, running=False)
        cog = _cog(tmux_manager)
        cog.repo.get = AsyncMock(side_effect=lambda tid: _record(tid, closed=True))
        cog.wake_workspace = AsyncMock(return_value=True)

        await cog.clear_text.callback(cog, _ctx(1))

        cog.wake_workspace.assert_not_called()
        tmux_manager.send_literal.assert_not_called()


class TestClearDuringTurn:
    @pytest.mark.asyncio
    async def test_interrupts_then_clears(self, tmp_path: Path) -> None:
        """AC4: a running turn is stopped first (not killed), then ``/clear`` is typed."""
        tmux_manager = _tmux(tmp_path)
        cog = _cog(tmux_manager)
        order: list[str] = []
        runner = MagicMock()
        runner.interrupt = AsyncMock(side_effect=lambda **_k: order.append("interrupt"))
        runner.kill = AsyncMock()
        cog._active_runners[1] = runner
        tmux_manager.send_literal.side_effect = lambda *_a: order.append("send") or True

        async def _idle(*_a: object, **_k: object) -> bool:
            order.append("idle")
            return True

        with patch.object(claude_chat_module, "wait_for_idle_prompt", _idle):
            await cog.clear_text.callback(cog, _ctx(1))

        assert order == ["interrupt", "idle", "send"]
        runner.kill.assert_not_called()
        tmux_manager.kill_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_that_will_not_stop_is_reported(self, tmp_path: Path) -> None:
        tmux_manager = _tmux(tmp_path)
        cog = _cog(tmux_manager)
        runner = MagicMock()
        runner.interrupt = AsyncMock()
        cog._active_runners[1] = runner
        ctx = _ctx(1)

        with patch.object(
            claude_chat_module, "wait_for_idle_prompt", AsyncMock(return_value=False)
        ):
            await cog.clear_text.callback(cog, ctx)

        tmux_manager.send_literal.assert_not_called()
        assert "止まりません" in ctx.send.call_args.args[0]


class TestSlashClearAcksBeforeSlowWork:
    @pytest.mark.asyncio
    async def test_defers_before_waking(self, tmp_path: Path) -> None:
        """A wake takes seconds; Discord drops a slash interaction not answered in 3s."""
        tmux_manager = _tmux(tmp_path, running=False)
        cog = _cog(tmux_manager)
        cog.wake_workspace = AsyncMock(return_value=True)
        interaction = MagicMock(spec=discord.Interaction)
        interaction.channel = _thread(1)
        interaction.user = MagicMock()
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        await cog.clear_session.callback(cog, interaction)

        interaction.response.defer.assert_called_once()
        interaction.response.send_message.assert_not_called()
        assert "/clear" in interaction.followup.send.call_args.args[0]
