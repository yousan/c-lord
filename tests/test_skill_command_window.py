"""Issue #762 — ``/skill`` must have a tmux window to start Claude in.

``SkillCommandCog._run_skill_impl`` (shared by ``/skill`` and ``!skill``) built
a ``TmuxClaudeRunner`` and called ``run_claude_with_config`` without creating
the window that runner types into — and nothing downstream creates one. The
third path with the #621 (scheduler) / #629 (webhook) hole, fixed the same way:

* the window is created (and the transcript mirror started) before Claude runs,
  in the thread's own checkout,
* a window that could not be created stops the run loudly — ERROR in the log
  and a message in the thread.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.skill_command import SkillCommandCog
from c_lord.discord_ui.authorization import Authorizer

_PATCH_RUN = "c_lord.cogs.skill_command.run_claude_with_config"
_SESSION_DIR = "/sessions/999/777"


def _make_cog() -> tuple[SkillCommandCog, MagicMock, MagicMock]:
    bot = MagicMock()
    bot.settings_repo = None
    bot.transcript_mirror_cog = MagicMock()
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    runner = MagicMock()
    runner.model = "sonnet"
    runner.working_dir = "/srv/default"
    runner.timeout_seconds = 300
    runner.effort = None
    cog = SkillCommandCog(
        bot=bot,
        repo=repo,
        runner=runner,
        claude_channel_id=999,
        skills_dir="/nonexistent/skills",
        authorizer=Authorizer(allow_anyone=True),
    )
    cog._skills = [{"name": "todoist", "description": "Tasks"}]

    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w7")
    tmux.session_exists = MagicMock(return_value=True)
    sdm = MagicMock()
    sdm.create_session_dir = MagicMock(return_value=_SESSION_DIR)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
    return cog, tmux, sdm


def _new_thread_mode(cog: SkillCommandCog) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = 777
    thread.mention = "<#777>"
    thread.send = AsyncMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 999
    channel.create_thread = AsyncMock(return_value=thread)
    cog.bot.get_channel = MagicMock(return_value=channel)
    return thread


def _in_thread(thread_id: int = 5555) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = 999
    thread.send = AsyncMock()
    return thread


async def _run(cog: SkillCommandCog, channel: object) -> AsyncMock:
    respond = AsyncMock()
    with (
        patch(_PATCH_RUN, new_callable=AsyncMock) as run,
        patch(
            "c_lord.cogs.skill_command.foreign_owner_notice_for",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await cog._run_skill_impl(
            channel=channel,
            user=MagicMock(id=1),
            name="todoist",
            args=None,
            respond=respond,
            ack=AsyncMock(),
        )
    return run


class TestNewThreadModeCreatesWindow:
    async def test_window_is_created_before_claude_runs(self) -> None:
        """The regression: ``_run_skill_impl`` never called ``create_session``."""
        cog, tmux, _ = _make_cog()
        thread = _new_thread_mode(cog)
        order: list[str] = []
        tmux.create_session.side_effect = lambda *_: order.append("create_session") or "w7"

        respond = AsyncMock()
        with (
            patch(_PATCH_RUN, new_callable=AsyncMock) as run,
        ):
            run.side_effect = lambda *_: order.append("run_claude")
            await cog._run_skill_impl(
                channel=MagicMock(),
                user=MagicMock(id=1),
                name="todoist",
                args=None,
                respond=respond,
                ack=AsyncMock(),
            )

        assert order == ["create_session", "run_claude"]
        tmux.create_session.assert_called_once_with(thread.id, _SESSION_DIR)

    async def test_runs_in_the_threads_checkout_and_mirrors_it(self) -> None:
        """The answer only reaches the thread if the mirror tails the dir Claude runs in."""
        cog, _, sdm = _make_cog()
        thread = _new_thread_mode(cog)

        mirror = cog.bot.transcript_mirror_cog  # type: ignore[attr-defined]
        run = await _run(cog, MagicMock())

        sdm.create_session_dir.assert_called_once()
        config = run.call_args.args[0]
        assert config.runner.working_dir == _SESSION_DIR
        assert config.working_dir == _SESSION_DIR
        mirror.start_for.assert_called_once_with(thread.id, _SESSION_DIR)

    async def test_missing_window_aborts_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        """AC3: no window → ERROR in the log, a message in the thread, no Claude."""
        cog, tmux, _ = _make_cog()
        thread = _new_thread_mode(cog)
        tmux.session_exists.return_value = False

        with caplog.at_level(logging.ERROR, logger="c_lord.cogs.skill_command"):
            run = await _run(cog, MagicMock())

        run.assert_not_called()
        assert any("tmux window" in r.getMessage() for r in caplog.records)
        thread.send.assert_awaited_once()
        assert "tmux ウィンドウ" in thread.send.call_args.args[0]


class TestInThreadModeCreatesWindow:
    async def test_window_is_created_in_the_threads_recorded_dir(self) -> None:
        """A slept thread (#576) has no window left — /skill must bring it back."""
        cog, tmux, _ = _make_cog()
        record = MagicMock()
        record.session_id = "abc-123"
        record.working_dir = "/sessions/999/5555"
        cog.repo.get = AsyncMock(return_value=record)
        thread = _in_thread()
        mirror = cog.bot.transcript_mirror_cog  # type: ignore[attr-defined]

        run = await _run(cog, thread)

        tmux.create_session.assert_called_once_with(5555, "/sessions/999/5555")
        run.assert_called_once()
        assert run.call_args.args[0].runner.working_dir == "/sessions/999/5555"
        mirror.start_for.assert_called_once_with(5555, "/sessions/999/5555")

    async def test_missing_window_aborts_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        cog, tmux, _ = _make_cog()
        tmux.session_exists.return_value = False
        thread = _in_thread()

        with caplog.at_level(logging.ERROR, logger="c_lord.cogs.skill_command"):
            run = await _run(cog, thread)

        run.assert_not_called()
        assert any("tmux window" in r.getMessage() for r in caplog.records)
        thread.send.assert_awaited_once()
