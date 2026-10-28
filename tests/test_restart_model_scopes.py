"""#954: right after a restart, scheduled and webhook turns still follow /model.

#918 routed both through :func:`begin_turn_for`, which builds the model-scope
service from ``settings_repo.db_path`` when nothing has cached it on the bot
yet. Neither cog has a ``settings_repo`` and nothing sets ``bot.settings_repo``,
so until a chat turn or ``/model`` ran, they got ``None`` and launched on the
instance defaults (``runner.model``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.claude.config import ClaudeConfig
from c_lord.cogs.scheduler import SchedulerCog
from c_lord.cogs.webhook_trigger import WebhookTrigger, WebhookTriggerCog
from c_lord.database.models import init_db
from c_lord.database.settings_repo import SettingsRepository
from c_lord.database.task_repo import TaskRepository
from c_lord.model_scopes import ModelScopeService, StartupEnv

CHANNEL = 99


@pytest.fixture
async def db(tmp_path) -> str:
    path = str(tmp_path / "s.db")
    await init_db(path)
    svc = ModelScopeService(path, StartupEnv("opus", None))
    await svc.change(scope="channel", model="sonnet", channel_id=CHANNEL)
    return path


def _fresh_bot(chat_repo: SettingsRepository | None) -> MagicMock:
    """A bot as it is just after a restart: no cached service, no ``settings_repo``.

    The chat cog is loaded (it always is under ``setup_bridge``) and holds the
    settings repository ``/model`` writes to — but has not run a turn yet.
    """
    bot = MagicMock(spec=["get_cog", "get_channel", "loop", "user", "session_registry"])
    chat = MagicMock(spec=["_settings_repo"])
    chat._settings_repo = chat_repo
    bot.get_cog = MagicMock(side_effect=lambda name: chat if name == "ClaudeChatCog" else None)
    return bot


def _runner() -> ClaudeConfig:
    return ClaudeConfig(command="claude", model="opus", working_dir="/home/user")


async def _scheduled_model(bot: MagicMock, tmp_path) -> str:
    repo = TaskRepository(str(tmp_path / "tasks.db"))
    await repo.init_db()
    task_id = await repo.create(name="t", prompt="p", interval_seconds=60, channel_id=CHANNEL)
    task = await repo.get(task_id)
    assert task is not None

    thread = AsyncMock(spec=discord.Thread)
    thread.id = 4242
    thread.parent_id = CHANNEL
    starter = AsyncMock()
    starter.create_thread = AsyncMock(return_value=thread)
    channel = AsyncMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=starter)
    bot.get_channel = MagicMock(return_value=channel)

    cog = SchedulerCog(bot, _runner(), repo=repo)
    cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
    with (
        patch("c_lord.claude.tmux_runner.TmuxClaudeRunner") as runner_cls,
        patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock),
    ):
        await cog._run_task(task)
    runner_cls.assert_called_once()
    return runner_cls.call_args.kwargs["model"]


async def _webhook_model(bot: MagicMock) -> str:
    cog = WebhookTriggerCog(
        bot=bot, runner=_runner(), triggers={"🔄 sync": WebhookTrigger(prompt="Sync")}
    )
    cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
    msg = MagicMock(spec=discord.Message)
    msg.content = "🔄 sync"
    msg.webhook_id = 1
    msg.channel = MagicMock()
    msg.channel.id = CHANNEL
    msg.add_reaction = AsyncMock()
    thread = AsyncMock(spec=discord.Thread)
    thread.id = 4343
    thread.parent_id = CHANNEL
    msg.create_thread = AsyncMock(return_value=thread)
    with (
        patch("c_lord.claude.tmux_runner.TmuxClaudeRunner") as runner_cls,
        patch("c_lord.cogs.webhook_trigger.run_claude_with_config", new_callable=AsyncMock),
    ):
        await cog.on_message(msg)
    runner_cls.assert_called_once()
    return runner_cls.call_args.kwargs["model"]


async def test_scheduled_turn_uses_channel_model_before_anything_cached(db, tmp_path):
    """AC1/AC3: no ``settings_repo`` on the cog or the bot, nothing cached yet."""
    assert await _scheduled_model(_fresh_bot(SettingsRepository(db)), tmp_path) == "sonnet"


async def test_webhook_turn_uses_channel_model_before_anything_cached(db):
    """AC2/AC3."""
    assert await _webhook_model(_fresh_bot(SettingsRepository(db))) == "sonnet"


async def test_without_any_settings_repository_the_instance_default_still_runs(tmp_path):
    """A consumer without the chat cog has no /model at all: launch on the defaults."""
    assert await _scheduled_model(_fresh_bot(None), tmp_path) == "opus"
