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
        """on_app_command_error should log the error at ERROR level."""
        bot = ClaudeDiscordBot(channel_id=123)
        interaction = MagicMock()
        error = app_commands.AppCommandError("test failure")

        with caplog.at_level(logging.ERROR, logger="c_lord.bot"):
            await bot.on_app_command_error(interaction, error)

        assert any("test failure" in record.message for record in caplog.records)


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
    async def test_session_closed_logs_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """RuntimeError('Session is closed') must log at ERROR level."""
        import sys
        from unittest.mock import patch

        bot = ClaudeDiscordBot(channel_id=123)

        with caplog.at_level(logging.ERROR, logger="c_lord.bot"):
            with patch.object(sys, "exit"):
                try:
                    raise RuntimeError("Session is closed")
                except RuntimeError:
                    await bot.on_error("on_message")

        assert any(r.levelno >= logging.ERROR for r in caplog.records)
        assert any("zombie" in r.message.lower() for r in caplog.records)
