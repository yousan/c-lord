"""``/upgrade`` — starting the upgrade pipeline by hand (``c_lord.cogs.auto_upgrade``).

The command is off unless the config turns it on. When it runs, it opens the
progress thread in the text channel it was used in (or, from inside a thread,
in that thread's parent channel) and hands over to the same pipeline the
webhook trigger uses — which is covered in ``tests/test_auto_upgrade.py`` and
replaced by a recorder here.

That strangers are refused is in ``tests/test_command_authorization.py``; the
caller in these tests is always on the allowlist.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.auto_upgrade import AutoUpgradeCog, UpgradeConfig
from c_lord.discord_ui.authorization import Authorizer
from c_lord.thread_settings import DEFAULT_AUTO_ARCHIVE_DURATION

OWNER_ID = 4990


class Timeline:
    """The order things happened in, as short labels."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def note(self, label: str) -> Any:
        async def record(*_args: Any, **_kwargs: Any) -> None:
            self.events.append(label)

        return record


def make_cog(*, settings_repo: Any = None, **config: Any) -> AutoUpgradeCog:
    """A cog whose pipeline is a recorder, on a bot that allows ``OWNER_ID``."""
    bot = SimpleNamespace(
        cogs={},
        user=SimpleNamespace(id=9000),
        authorizer=Authorizer(allowed_user_ids={OWNER_ID}),
        settings_repo=settings_repo,
    )
    cog = AutoUpgradeCog(bot, UpgradeConfig(package_name="pkg", **config))  # type: ignore[arg-type]
    cog._run_pipeline = AsyncMock()  # type: ignore[method-assign]
    return cog


def text_channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.create_thread = AsyncMock(return_value=MagicMock(spec=discord.Thread))
    return channel


def thread_in(parent: Any) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.parent = parent
    return thread


def used_in(channel: Any) -> MagicMock:
    """The interaction of the owner running ``/upgrade`` in *channel*."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = OWNER_ID
    interaction.channel = channel
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    return interaction


async def run_upgrade(cog: AutoUpgradeCog, interaction: MagicMock) -> None:
    await cog.upgrade_command.callback(cog, interaction)  # type: ignore[arg-type]


def private_reply(interaction: MagicMock) -> str:
    """The one message sent back to the caller alone."""
    interaction.response.send_message.assert_awaited_once()
    call = interaction.response.send_message.await_args
    assert call.kwargs == {"ephemeral": True}
    return call.args[0]


class TestRegistration:
    def test_the_command_is_called_upgrade(self) -> None:
        command = make_cog().upgrade_command

        assert command.name == "upgrade"
        assert command.description == "Manually trigger a package upgrade"


class TestDisabled:
    def test_the_command_is_off_unless_configured(self) -> None:
        assert UpgradeConfig(package_name="pkg").slash_command_enabled is False

    def test_it_can_be_turned_on(self) -> None:
        assert UpgradeConfig(package_name="pkg", slash_command_enabled=True).slash_command_enabled

    async def test_the_caller_is_told_privately_that_it_is_off(self) -> None:
        cog = make_cog()
        channel = text_channel()
        interaction = used_in(channel)

        await run_upgrade(cog, interaction)

        assert private_reply(interaction) == (
            "⚠️ Slash command upgrades are not enabled for this bot."
        )

    async def test_nothing_is_started_while_it_is_off(self) -> None:
        cog = make_cog()
        channel = text_channel()
        interaction = used_in(channel)

        await run_upgrade(cog, interaction)

        channel.create_thread.assert_not_called()
        cog._run_pipeline.assert_not_called()  # type: ignore[attr-defined]
        interaction.response.defer.assert_not_called()


class TestFromATextChannel:
    async def test_opens_a_progress_thread_and_runs_the_pipeline_in_it(self) -> None:
        cog = make_cog(slash_command_enabled=True)
        channel = text_channel()

        await run_upgrade(cog, used_in(channel))

        thread = channel.create_thread.return_value
        cog._run_pipeline.assert_awaited_once_with(thread, status_target=None)  # type: ignore[attr-defined]

    async def test_the_thread_is_named_after_the_trigger_line(self) -> None:
        cog = make_cog(slash_command_enabled=True, trigger_prefix="🚀 deploy")
        channel = text_channel()

        await run_upgrade(cog, used_in(channel))

        channel.create_thread.assert_awaited_once_with(
            name="🚀 deploy", auto_archive_duration=DEFAULT_AUTO_ARCHIVE_DURATION
        )

    async def test_a_long_trigger_line_is_cut_to_a_valid_thread_name(self) -> None:
        cog = make_cog(slash_command_enabled=True, trigger_prefix="n" * 150)
        channel = text_channel()

        await run_upgrade(cog, used_in(channel))

        assert channel.create_thread.await_args.kwargs["name"] == "n" * 100

    async def test_the_thread_follows_the_configured_auto_archive_time(self) -> None:
        settings_repo = MagicMock()
        settings_repo.get = AsyncMock(return_value="60")
        cog = make_cog(slash_command_enabled=True, settings_repo=settings_repo)
        channel = text_channel()

        await run_upgrade(cog, used_in(channel))

        assert channel.create_thread.await_args.kwargs["auto_archive_duration"] == 60

    async def test_the_interaction_is_acknowledged_before_the_slow_part(self) -> None:
        """Discord drops an interaction not answered within three seconds."""
        timeline = Timeline()
        cog = make_cog(slash_command_enabled=True)
        cog._run_pipeline = AsyncMock(side_effect=timeline.note("pipeline"))  # type: ignore[method-assign]
        channel = text_channel()
        channel.create_thread.side_effect = timeline.note("thread")
        interaction = used_in(channel)
        interaction.response.defer.side_effect = timeline.note("defer")

        await run_upgrade(cog, interaction)

        assert timeline.events == ["defer", "thread", "pipeline"]
        interaction.response.defer.assert_awaited_once_with()
        interaction.response.send_message.assert_not_called()

    async def test_other_upgrades_are_locked_out_while_this_one_runs(self) -> None:
        cog = make_cog(slash_command_enabled=True)
        locked: dict[str, bool] = {}
        interaction = used_in(text_channel())

        async def at_defer() -> None:
            locked["defer"] = cog._lock.locked()

        async def at_pipeline(_thread: Any, status_target: Any) -> None:
            locked["pipeline"] = cog._lock.locked()

        interaction.response.defer.side_effect = at_defer
        cog._run_pipeline = at_pipeline  # type: ignore[method-assign]

        await run_upgrade(cog, interaction)

        assert locked == {"defer": False, "pipeline": True}
        assert not cog._lock.locked()


class TestAlreadyRunning:
    async def test_a_second_request_is_turned_away_privately(self) -> None:
        cog = make_cog(slash_command_enabled=True)
        channel = text_channel()
        interaction = used_in(channel)

        async with cog._lock:
            await run_upgrade(cog, interaction)

        assert private_reply(interaction) == "⏳ Upgrade is already running. Please wait."
        channel.create_thread.assert_not_called()
        cog._run_pipeline.assert_not_called()  # type: ignore[attr-defined]
        interaction.response.defer.assert_not_called()


class TestFromInsideAThread:
    async def test_the_progress_thread_is_opened_in_the_parent_channel(self) -> None:
        """A thread cannot hold a thread, so the upgrade's own thread goes one level up."""
        cog = make_cog(slash_command_enabled=True)
        parent = text_channel()

        await run_upgrade(cog, used_in(thread_in(parent)))

        parent.create_thread.assert_awaited_once()
        cog._run_pipeline.assert_awaited_once_with(  # type: ignore[attr-defined]
            parent.create_thread.return_value, status_target=None
        )

    async def test_the_interaction_is_acknowledged_there_too(self) -> None:
        cog = make_cog(slash_command_enabled=True)
        interaction = used_in(thread_in(text_channel()))

        await run_upgrade(cog, interaction)

        interaction.response.defer.assert_awaited_once_with()
        interaction.response.send_message.assert_not_called()


class TestUnsupportedPlaces:
    @pytest.mark.parametrize(
        "channel",
        [
            MagicMock(spec=discord.DMChannel),
            MagicMock(spec=discord.VoiceChannel),
            thread_in(MagicMock(spec=discord.ForumChannel)),
            thread_in(None),
            None,
        ],
        ids=["dm", "voice", "forum-post", "orphan-thread", "no-channel"],
    )
    async def test_the_caller_is_told_where_the_command_works(self, channel: Any) -> None:
        cog = make_cog(slash_command_enabled=True)
        interaction = used_in(channel)

        await run_upgrade(cog, interaction)

        assert private_reply(interaction) == (
            "⚠️ This command can only be used in a text channel or thread."
        )
        cog._run_pipeline.assert_not_called()  # type: ignore[attr-defined]
        interaction.response.defer.assert_not_called()
