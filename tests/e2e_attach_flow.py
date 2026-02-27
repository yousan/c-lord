"""E2E test: !attach text command through the real discord.py pipeline.

This test creates a real commands.Bot + ClaudeChatCog, sends a mock message
through the actual get_context / process_commands pipeline, and verifies:
1. bot.get_context recognizes "!attach work13" as a valid command
2. The cog's on_message guard skips _handle_thread_reply for command messages
3. process_commands invokes the attach_text handler (remap + DB save)
4. Normal (non-command) messages still reach _handle_thread_reply
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from c_lord.cogs.claude_chat import ClaudeChatCog


def _make_thread_message(
    content: str,
    thread_id: int = 55555,
    parent_id: int = 999,
    author_id: int = 42,
) -> MagicMock:
    """Build a mock Message that discord.py's get_context can parse."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.send = AsyncMock()

    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.channel = thread
    msg.attachments = []
    msg.author = MagicMock()
    msg.author.bot = False
    msg.author.id = author_id
    msg.webhook_id = None
    # discord.py Context.__init__ accesses message._state
    msg._state = MagicMock()
    return msg


def _make_bot_and_cog() -> tuple[commands.Bot, ClaudeChatCog, MagicMock, MagicMock]:
    """Create a real Bot with ClaudeChatCog and mocked dependencies.

    Returns (bot, cog, tmux_manager_mock, repo_mock).
    """
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    # bot.user is a read-only property that reads from _connection.user
    mock_user = MagicMock()
    mock_user.id = 9999
    bot._connection.user = mock_user

    # Bot attributes expected by ClaudeChatCog
    bot.channel_id = 999

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()

    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())

    tmux_mgr = MagicMock()
    tmux_mgr.remap_window.return_value = True
    bot.tmux_manager = tmux_mgr

    cog = ClaudeChatCog(bot=bot, repo=repo, runner=runner)
    return bot, cog, tmux_mgr, repo


class TestE2EAttachFlow:
    """Full pipeline E2E: real Bot.get_context + process_commands + cog listener."""

    @pytest.mark.asyncio
    async def test_get_context_recognizes_attach_command(self) -> None:
        """bot.get_context('!attach work13') returns ctx.valid=True."""
        bot, cog, _, _ = _make_bot_and_cog()
        await bot.add_cog(cog)

        msg = _make_thread_message("!attach work13")
        ctx = await bot.get_context(msg)

        assert ctx.valid is True, (
            f"Expected valid context, got prefix={ctx.prefix}, command={ctx.command}"
        )
        assert ctx.command.name == "attach"
        assert ctx.invoked_with == "attach"

    @pytest.mark.asyncio
    async def test_get_context_rejects_normal_message(self) -> None:
        """bot.get_context('hello') returns ctx.valid=False."""
        bot, cog, _, _ = _make_bot_and_cog()
        await bot.add_cog(cog)

        msg = _make_thread_message("hello Claude")
        ctx = await bot.get_context(msg)

        assert ctx.valid is False

    @pytest.mark.asyncio
    async def test_on_message_guard_skips_command(self) -> None:
        """Cog's on_message skips _handle_thread_reply for !attach messages."""
        bot, cog, _, _ = _make_bot_and_cog()
        await bot.add_cog(cog)
        cog._handle_thread_reply = AsyncMock()

        msg = _make_thread_message("!attach work13")
        await cog.on_message(msg)

        cog._handle_thread_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_message_guard_passes_normal_message(self) -> None:
        """Cog's on_message forwards normal messages to _handle_thread_reply."""
        bot, cog, _, _ = _make_bot_and_cog()
        await bot.add_cog(cog)
        cog._handle_thread_reply = AsyncMock()

        msg = _make_thread_message("hello Claude")
        await cog.on_message(msg)

        cog._handle_thread_reply.assert_called_once_with(msg)

    @pytest.mark.asyncio
    async def test_process_commands_invokes_attach(self) -> None:
        """bot.process_commands('!attach work13') → remap + DB save."""
        bot, cog, tmux_mgr, repo = _make_bot_and_cog()
        await bot.add_cog(cog)

        msg = _make_thread_message("!attach work13", thread_id=55555)

        # Patch Context.send so the real discord.py pipeline doesn't
        # try to hit the Discord API when the command responds.
        with patch.object(commands.Context, "send", new_callable=AsyncMock) as mock_send:
            await bot.process_commands(msg)

        tmux_mgr.remap_window.assert_called_once_with(55555, "work13")
        repo.save.assert_called_once_with(55555, session_id="tmux-55555")
        # Verify the command responded with a confirmation
        mock_send.assert_called_once()
        assert "work13" in mock_send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_process_commands_ignores_normal_message(self) -> None:
        """bot.process_commands('hello') does NOT call remap."""
        bot, cog, tmux_mgr, _ = _make_bot_and_cog()
        await bot.add_cog(cog)

        msg = _make_thread_message("hello Claude")
        await bot.process_commands(msg)

        tmux_mgr.remap_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_flow_no_double_processing(self) -> None:
        """Full E2E: on_message + process_commands together — no double processing.

        Simulates the real discord.py event flow:
        1. Cog's on_message listener fires → guard skips _handle_thread_reply
        2. Bot's process_commands fires → !attach handler runs exactly once
        """
        bot, cog, tmux_mgr, repo = _make_bot_and_cog()
        await bot.add_cog(cog)
        cog._handle_thread_reply = AsyncMock()

        msg = _make_thread_message("!attach work13", thread_id=55555)

        # Simulate the two things that happen on a message event:
        with patch.object(commands.Context, "send", new_callable=AsyncMock):
            # 1) Cog listener (on_message)
            await cog.on_message(msg)
            # 2) Bot default handler (process_commands)
            await bot.process_commands(msg)

        # Guard prevented double processing
        cog._handle_thread_reply.assert_not_called()
        # Command handler ran exactly once
        tmux_mgr.remap_window.assert_called_once_with(55555, "work13")
        repo.save.assert_called_once_with(55555, session_id="tmux-55555")

    @pytest.mark.asyncio
    async def test_full_flow_normal_message(self) -> None:
        """Full E2E: normal message → _handle_thread_reply fires, no command."""
        bot, cog, tmux_mgr, _ = _make_bot_and_cog()
        await bot.add_cog(cog)
        cog._handle_thread_reply = AsyncMock()

        msg = _make_thread_message("hello Claude")

        await cog.on_message(msg)
        await bot.process_commands(msg)

        # Normal message reached _handle_thread_reply
        cog._handle_thread_reply.assert_called_once_with(msg)
        # No command was invoked
        tmux_mgr.remap_window.assert_not_called()
