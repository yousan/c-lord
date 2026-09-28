"""``/compact`` on a stopped thread restores it first instead of refusing (#806).

2026-09-28 09:52, task-management thread 1544180092836773989: the Claude had
been down since the 9/25 22:47 host restart, and ``/compact`` answered
``No running Claude session in this thread to compact.``  A plain message would
have restored it (#700), ``/tmux-screenshot`` restores it (#642) — only
``/compact`` said the session was gone, which reads as the conversation being
lost.  It now goes through the same "wake, then type the slash command" path as
``/clear`` (#803).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.database.repository import SessionRecord
from c_lord.discord_ui.authorization import Authorizer
from c_lord.transcript.resolver import derive_project_dir


def _record(thread_id: int, working_dir: str | None, *, closed: bool = False) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id=f"tmux-{thread_id}",
        working_dir=working_dir,
        model=None,
        origin="chat",
        summary=None,
        created_at="2026-09-28 00:00:00",
        last_used_at="2026-09-28 00:00:00",
        closed_at="2026-09-28 00:00:00" if closed else None,
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A session dir whose Claude Code project dir holds one transcript."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    wd = tmp_path / "ws"
    wd.mkdir()
    project = derive_project_dir(str(wd), projects_root=tmp_path / "home/.claude/projects")
    project.mkdir(parents=True)
    (project / "11111111-1111-1111-1111-111111111111.jsonl").write_text("{}\n")
    return wd


def _cog(record: SessionRecord | None, *, running: bool) -> tuple[ClaudeChatCog, MagicMock]:
    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    repo = MagicMock()
    repo.get = AsyncMock(return_value=record)
    cog = ClaudeChatCog(
        bot=bot, repo=repo, runner=MagicMock(), authorizer=Authorizer(allow_anyone=True)
    )
    tmux_manager = MagicMock()
    tmux_manager.is_claude_running = MagicMock(return_value=running)
    tmux_manager.send_literal = MagicMock(return_value=True)
    tmux_manager.send_keys = MagicMock(return_value=True)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
    cog.wake_workspace = AsyncMock(return_value=True)
    return cog, tmux_manager


def _ctx(thread_id: int = 1) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = 999
    thread.send = AsyncMock()
    ctx = MagicMock()
    ctx.channel = thread
    ctx.send = AsyncMock()
    return ctx


class TestCompactStoppedThread:
    @pytest.mark.asyncio
    async def test_wakes_then_compacts(self, workspace: Path) -> None:
        """AC1 + AC3: restored with ``--resume``/``--continue``, said so, then compacted."""
        cog, tmux_manager = _cog(_record(1, str(workspace)), running=False)
        ctx = _ctx()

        await cog.compact_text.callback(cog, ctx)

        cog.wake_workspace.assert_awaited_once_with(ctx.channel)
        tmux_manager.send_literal.assert_called_once_with(1, "/compact")
        tmux_manager.send_keys.assert_called_once_with(1, "Enter")
        notice = ctx.channel.send.call_args.args[0]
        assert "復元" in notice and "/compact" in notice
        assert "Compacting" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_instructions_survive_the_wake(self, workspace: Path) -> None:
        cog, tmux_manager = _cog(_record(1, str(workspace)), running=False)

        await cog.compact_text.callback(cog, _ctx(), instructions="keep open tasks")

        tmux_manager.send_literal.assert_called_once_with(1, "/compact keep open tasks")

    @pytest.mark.asyncio
    async def test_live_claude_is_unchanged(self, workspace: Path) -> None:
        """AC4: no wake, no notice — typed straight away."""
        cog, tmux_manager = _cog(_record(1, str(workspace)), running=True)
        ctx = _ctx()

        await cog.compact_text.callback(cog, ctx)

        cog.wake_workspace.assert_not_called()
        ctx.channel.send.assert_not_called()
        tmux_manager.send_literal.assert_called_once_with(1, "/compact")

    @pytest.mark.asyncio
    async def test_untracked_thread_is_refused_in_words(self) -> None:
        """AC5: no c-lord record → the usual explanation, nothing woken."""
        cog, tmux_manager = _cog(None, running=False)
        ctx = _ctx()

        await cog.compact_text.callback(cog, ctx)

        cog.wake_workspace.assert_not_called()
        tmux_manager.send_literal.assert_not_called()
        assert "c-lord の記録が見つかりません" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_thread_where_claude_never_ran_is_refused(self, tmp_path: Path) -> None:
        """AC5: a row but no transcript at all — there is no conversation to compact.

        Waking would start an empty Claude only to compact nothing.
        """
        empty = tmp_path / "never-ran"
        empty.mkdir()
        cog, tmux_manager = _cog(_record(1, str(empty)), running=False)
        ctx = _ctx()

        await cog.compact_text.callback(cog, ctx)

        cog.wake_workspace.assert_not_called()
        tmux_manager.send_literal.assert_not_called()
        assert "会話の記録" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_closed_thread_is_not_woken(self, workspace: Path) -> None:
        cog, tmux_manager = _cog(_record(1, str(workspace), closed=True), running=False)

        await cog.compact_text.callback(cog, _ctx())

        cog.wake_workspace.assert_not_called()
        tmux_manager.send_literal.assert_not_called()

    @pytest.mark.asyncio
    async def test_slash_defers_before_waking(self, workspace: Path) -> None:
        cog, _tmux = _cog(_record(1, str(workspace)), running=False)
        interaction = MagicMock(spec=discord.Interaction)
        interaction.channel = _ctx().channel
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        await cog.compact_session.callback(cog, interaction)

        interaction.response.defer.assert_called_once()
        interaction.response.send_message.assert_not_called()
        assert "Compacting" in interaction.followup.send.call_args.args[0]
