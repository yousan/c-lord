"""#918: the turn takes its model/effort from the scoped settings, as a pair.

Drives the real ``ClaudeChatCog._run_claude`` up to the moment it builds the
runner and checks what the Claude process would be launched with — and whether a
Claude already running with a different pair is restarted (with ``--resume``)
rather than being sent ``/model`` (which would rewrite the host's user settings).
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.database.models import init_db
from c_lord.database.repository import SessionRepository
from c_lord.database.settings_repo import SettingsRepository
from c_lord.model_scopes import ModelScopeService, StartupEnv

CHANNEL = 500
THREAD = 501


class _Launched(BaseException):
    """Raised by the patched runner constructor: we only need its arguments."""


@pytest.fixture
async def env(tmp_path):
    db = str(tmp_path / "s.db")
    await init_db(db)
    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    bot.get_cog = MagicMock(return_value=None)
    bot._clord_model_scopes = None
    repo = SessionRepository(db)
    runner = MagicMock()
    runner.model = "opus"
    runner.effort = "high"
    runner.model_source = ".env"
    runner.effort_source = ".env"
    runner.timeout_seconds = 30
    runner.working_dir = None
    cog = ClaudeChatCog(bot=bot, repo=repo, runner=runner, settings_repo=SettingsRepository(db))
    sdm = MagicMock()
    sdm.create_session_dir = MagicMock(return_value="/tmp/wd")
    tmux = MagicMock()
    tmux.is_claude_running = MagicMock(return_value=True)
    tmux.kill_session = MagicMock(return_value=True)
    tmux.create_session = MagicMock(return_value="w1")
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    cog._get_dashboard = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_coordination = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._stage_attachments = AsyncMock(return_value="")  # type: ignore[method-assign]
    cog._apply_thread_naming = AsyncMock()  # type: ignore[method-assign]
    svc = ModelScopeService(db, StartupEnv("opus", "high", ".env", ".env"))
    bot._clord_model_scopes = svc
    return cog, svc, tmux, repo


def _thread() -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = THREAD
    t.parent_id = CHANNEL
    t.send = AsyncMock(return_value=MagicMock())
    return t


def _message() -> MagicMock:
    m = MagicMock(spec=discord.Message)
    m.id = 77
    m.author = MagicMock()
    m.author.bot = False
    m.add_reaction = AsyncMock()
    m.remove_reaction = AsyncMock()
    m.clear_reaction = AsyncMock()
    return m


async def _launch(cog, *, session_id=None):
    seen: dict = {}

    def fake(**kwargs):
        seen.update(kwargs)
        raise _Launched

    with (
        patch("c_lord.cogs.claude_chat.TmuxClaudeRunner", side_effect=fake),
        contextlib.suppress(_Launched),
    ):
        await cog._run_claude(_message(), _thread(), "hi", session_id)
    return seen


async def test_first_turn_launches_with_the_resolved_pair_and_saves_it(env):
    cog, svc, tmux, repo = env
    await svc.change(scope="channel", model="sonnet", effort="medium", channel_id=CHANNEL)
    seen = await _launch(cog)
    assert (seen["model"], seen["effort"]) == ("sonnet", "medium")
    saved = await svc.view(channel_id=CHANNEL, thread_id=THREAD)
    assert saved.session_saved is not None
    assert (saved.session_saved.model, saved.session_saved.model_src) == ("sonnet", "channel")
    tmux.kill_session.assert_not_called()


async def test_effort_is_dropped_for_a_model_without_effort(env):
    cog, svc, tmux, repo = env
    await svc.change(scope="session", model="haiku", thread_id=THREAD, channel_id=CHANNEL)
    seen = await _launch(cog)
    assert seen["model"] == "haiku" and seen["effort"] is None


async def test_unchanged_pair_does_not_restart_a_running_claude(env):
    cog, svc, tmux, repo = env
    await _launch(cog)
    seen = await _launch(cog, session_id="abc")
    assert (seen["model"], seen["effort"]) == ("opus", "high")
    tmux.kill_session.assert_not_called()


async def test_changed_pair_restarts_with_resume_on_the_next_turn(env):
    cog, svc, tmux, repo = env
    await _launch(cog)  # first turn: launched with opus/high
    await svc.change(
        scope="session", model="sonnet", effort="low", thread_id=THREAD, channel_id=CHANNEL
    )
    seen = await _launch(cog, session_id="abc")
    tmux.kill_session.assert_called_once_with(THREAD)
    assert (seen["model"], seen["effort"]) == ("sonnet", "low")
    assert seen["try_continue"] is True  # same conversation, new flags


async def test_dead_pane_needs_no_kill_just_the_new_pair(env):
    cog, svc, tmux, repo = env
    await _launch(cog)
    tmux.is_claude_running.return_value = False
    await svc.change(scope="session", model="sonnet", thread_id=THREAD, channel_id=CHANNEL)
    seen = await _launch(cog, session_id="abc")
    tmux.kill_session.assert_not_called()
    assert seen["model"] == "sonnet"


async def test_a_settings_failure_falls_back_to_instance_defaults(env):
    cog, svc, tmux, repo = env
    svc.begin_turn = AsyncMock(side_effect=RuntimeError("db locked"))  # type: ignore[method-assign]
    seen = await _launch(cog)
    assert (seen["model"], seen["effort"]) == ("opus", "high")
