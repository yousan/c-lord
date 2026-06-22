"""Tests for ClaudeDiscordBot."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from c_lord.bot import ClaudeDiscordBot


class TestProcessCommands:
    """process_commands override: allow webhook messages, block normal bot messages."""

    @pytest.mark.asyncio
    async def test_webhook_message_not_blocked(self) -> None:
        """Webhook messages (author.bot=True, webhook_id set) should be processed."""
        bot = ClaudeDiscordBot(channel_id=123)
        bot.get_context = AsyncMock()
        bot.invoke = AsyncMock()

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.bot = True
        msg.webhook_id = 999  # webhook message

        await bot.process_commands(msg)

        bot.get_context.assert_called_once_with(msg)
        bot.invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_bot_message_blocked(self) -> None:
        """Normal bot messages (author.bot=True, no webhook_id) should be ignored."""
        bot = ClaudeDiscordBot(channel_id=123)
        bot.get_context = AsyncMock()
        bot.invoke = AsyncMock()

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.bot = True
        msg.webhook_id = None

        await bot.process_commands(msg)

        bot.get_context.assert_not_called()
        bot.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_human_message_processed(self) -> None:
        """Normal human messages should be processed."""
        bot = ClaudeDiscordBot(channel_id=123)
        bot.get_context = AsyncMock()
        bot.invoke = AsyncMock()

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.bot = False
        msg.webhook_id = None

        await bot.process_commands(msg)

        bot.get_context.assert_called_once_with(msg)
        bot.invoke.assert_called_once()


class TestOnAppCommandError:
    """on_app_command_error handler tests."""

    @pytest.mark.asyncio
    async def test_handler_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """on_app_command_error should log the error at ERROR level.

        #444 also makes it reply to the user, so the interaction's response
        methods must be awaitable mocks (see test_error_safety_net.py for the
        reply-behavior assertions).
        """
        bot = ClaudeDiscordBot(channel_id=123)
        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        error = app_commands.AppCommandError("test failure")

        with caplog.at_level(logging.ERROR, logger="c_lord.bot"):
            await bot.on_app_command_error(interaction, error)

        assert any("test failure" in record.message for record in caplog.records)


class TestIdentityFailFast:
    """Issue #323: refuse to run when the logged-in identity differs from the expected one.

    Guards against the env-contamination incident class (#322): a "staging"
    launch that inherited the production token must die at startup instead of
    silently running as a second production bot.
    """

    def test_mismatch_exits_process(self) -> None:
        """expected_bot_user_id set + different login identity -> sys.exit(1)."""
        import sys
        from unittest.mock import PropertyMock, patch

        bot = ClaudeDiscordBot(channel_id=123, expected_bot_user_id=111)
        user = MagicMock()
        user.id = 222

        with (
            patch.object(ClaudeDiscordBot, "user", new_callable=PropertyMock, return_value=user),
            patch.object(sys, "exit") as mock_exit,
        ):
            bot._assert_expected_identity()

        mock_exit.assert_called_once_with(1)

    def test_mismatch_logs_expected_and_actual(self, caplog: pytest.LogCaptureFixture) -> None:
        """The failure log must show BOTH the expected and the actual bot user id."""
        import sys
        from unittest.mock import PropertyMock, patch

        bot = ClaudeDiscordBot(channel_id=123, expected_bot_user_id=111)
        user = MagicMock()
        user.id = 222

        with (
            caplog.at_level(logging.CRITICAL, logger="c_lord.bot"),
            patch.object(ClaudeDiscordBot, "user", new_callable=PropertyMock, return_value=user),
            patch.object(sys, "exit"),
        ):
            bot._assert_expected_identity()

        joined = " ".join(r.message for r in caplog.records)
        assert "111" in joined
        assert "222" in joined

    def test_match_does_not_exit(self) -> None:
        """expected_bot_user_id set + matching identity -> bot keeps running."""
        import sys
        from unittest.mock import PropertyMock, patch

        bot = ClaudeDiscordBot(channel_id=123, expected_bot_user_id=111)
        user = MagicMock()
        user.id = 111

        with (
            patch.object(ClaudeDiscordBot, "user", new_callable=PropertyMock, return_value=user),
            patch.object(sys, "exit") as mock_exit,
        ):
            bot._assert_expected_identity()

        mock_exit.assert_not_called()

    def test_unset_is_backward_compatible(self) -> None:
        """No expected_bot_user_id (default) -> never exits, any identity allowed."""
        import sys
        from unittest.mock import PropertyMock, patch

        bot = ClaudeDiscordBot(channel_id=123)
        user = MagicMock()
        user.id = 222

        with (
            patch.object(ClaudeDiscordBot, "user", new_callable=PropertyMock, return_value=user),
            patch.object(sys, "exit") as mock_exit,
        ):
            bot._assert_expected_identity()

        mock_exit.assert_not_called()


class TestOnError:
    """on_error zombie-detection tests (Issue #91)."""

    @pytest.mark.asyncio
    async def test_session_closed_exits_process(self) -> None:
        """RuntimeError('Session is closed') must trigger sys.exit(1)."""
        import sys
        from unittest.mock import patch

        bot = ClaudeDiscordBot(channel_id=123)

        with patch.object(sys, "exit") as mock_exit:
            try:
                raise RuntimeError("Session is closed")
            except RuntimeError:
                await bot.on_error("on_message")

        mock_exit.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_other_runtime_error_does_not_exit(self) -> None:
        """Non-zombie RuntimeError must NOT call sys.exit."""
        import sys
        from unittest.mock import patch

        bot = ClaudeDiscordBot(channel_id=123)

        with patch.object(sys, "exit") as mock_exit:
            try:
                raise RuntimeError("something else")
            except RuntimeError:
                await bot.on_error("on_message")

        mock_exit.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_closed_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """RuntimeError('Session is closed') must log at ERROR level."""
        import sys
        from unittest.mock import patch

        bot = ClaudeDiscordBot(channel_id=123)

        with caplog.at_level(logging.ERROR, logger="c_lord.bot"), patch.object(sys, "exit"):
            try:
                raise RuntimeError("Session is closed")
            except RuntimeError:
                await bot.on_error("on_message")

        assert any(r.levelno >= logging.ERROR for r in caplog.records)
        assert any("zombie" in r.message.lower() for r in caplog.records)


class TestSlashCommandSync:
    """Slash command sync must register guild-only and wipe global commands.

    Before the fix, the bot called both tree.sync(guild=guild) AND tree.sync()
    (global), causing the same command to appear twice in Discord's command list.
    The fix has two parts:
      1. Register as guild commands (instant)
      2. Explicitly clear global commands so previously-registered globals disappear
    """

    def _make_bot_with_mocks(self) -> tuple:
        from unittest.mock import AsyncMock, MagicMock

        bot = ClaudeDiscordBot(channel_id=123)
        guild = MagicMock()
        channel = MagicMock()
        channel.guild = guild
        bot.tree.copy_global_to = MagicMock()
        bot.tree.clear_commands = MagicMock()
        bot.tree.sync = AsyncMock(return_value=[])
        bot.get_channel = MagicMock(return_value=channel)
        return bot, guild

    @pytest.mark.asyncio
    async def test_guild_sync_called(self) -> None:
        """tree.copy_global_to and tree.sync(guild=...) must be called."""
        from unittest.mock import AsyncMock, patch

        bot, guild = self._make_bot_with_mocks()

        with (
            patch.object(bot, "_assert_expected_identity"),
            patch.object(bot, "_restore_pending_ask_views", new_callable=AsyncMock),
            patch("c_lord.bot.isinstance", return_value=False),
        ):
            await bot.on_ready()

        bot.tree.copy_global_to.assert_called_once_with(guild=guild)
        # First sync call must be the guild sync
        first_call = bot.tree.sync.call_args_list[0]
        assert first_call.kwargs.get("guild") == guild

    @pytest.mark.asyncio
    async def test_global_commands_cleared(self) -> None:
        """clear_commands(guild=None) + tree.sync() must be called to wipe legacy globals."""
        from unittest.mock import AsyncMock, call, patch

        bot, guild = self._make_bot_with_mocks()

        with (
            patch.object(bot, "_assert_expected_identity"),
            patch.object(bot, "_restore_pending_ask_views", new_callable=AsyncMock),
            patch("c_lord.bot.isinstance", return_value=False),
        ):
            await bot.on_ready()

        bot.tree.clear_commands.assert_called_once_with(guild=None)
        # Second sync call must be the global clear (no guild kwarg)
        assert call() in bot.tree.sync.call_args_list

    @pytest.mark.asyncio
    async def test_sync_order_guild_before_global_clear(self) -> None:
        """Guild sync must happen before the global clear."""
        from unittest.mock import AsyncMock, patch

        bot, guild = self._make_bot_with_mocks()
        call_order: list[str] = []

        orig_sync = bot.tree.sync.side_effect

        async def tracking_sync(**kwargs: object) -> list:
            if "guild" in kwargs:
                call_order.append("guild")
            else:
                call_order.append("global_clear")
            return []

        bot.tree.sync.side_effect = tracking_sync

        with (
            patch.object(bot, "_assert_expected_identity"),
            patch.object(bot, "_restore_pending_ask_views", new_callable=AsyncMock),
            patch("c_lord.bot.isinstance", return_value=False),
        ):
            await bot.on_ready()

        assert call_order == ["guild", "global_clear"]
