"""#927: ``/skill`` launches with the model/effort ``/model`` resolved, like a chat turn.

#918 fixed the pair once per turn for chat, scheduled and webhook runs, but
``SkillCommandCog._make_runner`` kept passing the instance defaults
(``runner.model`` / ``runner.effort``). A thread set to ``/model sonnet`` then
ran its ``/skill`` turns on another model, contradicting what ``/model`` showed.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.skill_command import SkillCommandCog
from c_lord.database.models import init_db
from c_lord.discord_ui.authorization import Authorizer
from c_lord.model_scopes import ModelScopeService, StartupEnv

CHANNEL = 999
THREAD = 5555
NEW_THREAD = 777
_SESSION_DIR = "/sessions/999/x"


class _Launched(BaseException):
    """Raised by the patched runner constructor: only its arguments matter."""


@pytest.fixture
async def env(tmp_path):
    db = str(tmp_path / "s.db")
    await init_db(db)
    bot = MagicMock()
    bot.settings_repo = None
    bot.transcript_mirror_cog = MagicMock()
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(session_id="abc-123", working_dir=None))
    runner = MagicMock()
    runner.model = "opus"
    runner.effort = "high"
    runner.working_dir = "/srv/default"
    runner.timeout_seconds = 300
    cog = SkillCommandCog(
        bot=bot,
        repo=repo,
        runner=runner,
        claude_channel_id=CHANNEL,
        skills_dir="/nonexistent/skills",
        authorizer=Authorizer(allow_anyone=True),
    )
    cog._skills = [{"name": "todoist", "description": "Tasks"}]
    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w7")
    tmux.session_exists = MagicMock(return_value=True)
    tmux.is_claude_running = MagicMock(return_value=True)
    tmux.kill_session = MagicMock(return_value=True)
    sdm = MagicMock()
    sdm.create_session_dir = MagicMock(return_value=_SESSION_DIR)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    svc = ModelScopeService(db, StartupEnv("opus", "high", ".env", ".env"))
    bot._clord_model_scopes = svc
    return cog, svc, tmux


def _in_thread() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD
    thread.parent_id = CHANNEL
    thread.send = AsyncMock()
    return thread


def _channel_for_new_thread(cog: SkillCommandCog) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = NEW_THREAD
    thread.mention = f"<#{NEW_THREAD}>"
    thread.send = AsyncMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CHANNEL
    channel.create_thread = AsyncMock(return_value=thread)
    cog.bot.get_channel = MagicMock(return_value=channel)
    return MagicMock()  # invoked outside any claude thread


async def _launch(cog: SkillCommandCog, channel: object) -> dict:
    seen: dict = {}

    def fake(**kwargs):
        seen.update(kwargs)
        raise _Launched

    with (
        patch("c_lord.cogs.skill_command.TmuxClaudeRunner", side_effect=fake),
        patch("c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock),
        patch(
            "c_lord.cogs.skill_command.foreign_owner_notice_for",
            new_callable=AsyncMock,
            return_value=None,
        ),
        contextlib.suppress(_Launched),
    ):
        await cog._run_skill_impl(
            channel=channel,
            user=MagicMock(id=1),
            name="todoist",
            args=None,
            respond=AsyncMock(),
            ack=AsyncMock(),
        )
    return seen


async def test_new_thread_skill_uses_the_channel_model(env):
    cog, svc, tmux = env
    await svc.change(scope="channel", model="sonnet", effort="medium", channel_id=CHANNEL)
    seen = await _launch(cog, _channel_for_new_thread(cog))
    assert (seen["model"], seen["effort"]) == ("sonnet", "medium")
    saved = await svc.view(channel_id=CHANNEL, thread_id=NEW_THREAD)
    assert saved.session_saved is not None  # begin_turn fixed the pair


async def test_in_thread_skill_uses_the_session_model(env):
    cog, svc, tmux = env
    await svc.change(
        scope="session", model="sonnet", effort="low", thread_id=THREAD, channel_id=CHANNEL
    )
    seen = await _launch(cog, _in_thread())
    assert (seen["model"], seen["effort"]) == ("sonnet", "low")


async def test_unchanged_pair_does_not_restart(env):
    cog, svc, tmux = env
    await _launch(cog, _in_thread())  # first turn records opus/high as applied
    seen = await _launch(cog, _in_thread())
    assert (seen["model"], seen["effort"]) == ("opus", "high")
    tmux.kill_session.assert_not_called()
    assert seen.get("try_continue", False) is False


async def test_changed_pair_restarts_with_resume(env):
    cog, svc, tmux = env
    await _launch(cog, _in_thread())  # launched with opus/high
    await svc.change(scope="session", model="sonnet", thread_id=THREAD, channel_id=CHANNEL)
    seen = await _launch(cog, _in_thread())
    tmux.kill_session.assert_called_once_with(THREAD)
    assert seen["model"] == "sonnet"
    assert seen["try_continue"] is True  # same conversation, new flags


async def test_settings_failure_falls_back_to_instance_defaults(env):
    cog, svc, tmux = env
    svc.begin_turn = AsyncMock(side_effect=RuntimeError("db locked"))  # type: ignore[method-assign]
    seen = await _launch(cog, _in_thread())
    assert (seen["model"], seen["effort"]) == ("opus", "high")
