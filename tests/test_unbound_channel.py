"""Tests for unbound-channel error behavior.

When a channel has no /clord-init binding, all Claude commands should return
a clear error message instead of silently falling back to a global manager.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cog(**overrides: object) -> ClaudeChatCog:
    """Return a ClaudeChatCog with minimal mocked dependencies."""
    bot = MagicMock()
    bot.channel_id = 999
    # ChannelRepoCog returns None (no binding)
    channel_cog = MagicMock()
    channel_cog.resolve_manager = AsyncMock(return_value=None)
    channel_cog.resolve_tmux_manager = AsyncMock(return_value=None)
    bot.get_cog = MagicMock(return_value=channel_cog)
    # Global manager exists on bot — should NOT be used
    bot.session_dir_manager = MagicMock()
    bot.tmux_manager = MagicMock()

    _default_ctx = MagicMock()
    _default_ctx.valid = False
    bot.get_context = AsyncMock(return_value=_default_ctx)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    runner.working_dir = "/tmp/test"
    runner.model = "sonnet"
    runner.timeout_seconds = 300
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner, **overrides)


# ---------------------------------------------------------------------------
# Phase 1: _resolve_session_dir_manager / _resolve_tmux_manager
# ---------------------------------------------------------------------------


class TestResolveWithoutBinding:
    """When ChannelRepoCog returns None (no binding), resolvers must return None.

    They must NOT fall back to bot.session_dir_manager / bot.tmux_manager.
    """

    @pytest.mark.asyncio
    async def test_resolve_session_dir_returns_none_without_binding(self) -> None:
        cog = _make_cog()
        result = await cog._resolve_session_dir_manager(channel_id=12345)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_tmux_returns_none_without_binding(self) -> None:
        cog = _make_cog()
        result = await cog._resolve_tmux_manager(channel_id=12345, thread_id=None)
        assert result is None


# ---------------------------------------------------------------------------
# Phase 1: _run_claude — error when unbound
# ---------------------------------------------------------------------------


class TestRunClaudeUnboundChannel:
    """_run_claude should post an error and return when channel has no binding."""

    @pytest.mark.asyncio
    async def test_run_claude_unbound_channel_posts_error(self) -> None:
        cog = _make_cog()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 12345
        thread.parent_id = 777  # unbound channel
        thread.send = AsyncMock()

        user_message = MagicMock(spec=discord.Message)
        user_message.id = 1

        await cog._run_claude(
            user_message=user_message,
            thread=thread,
            prompt="hello",
            session_id=None,
        )

        # Should have sent an error message mentioning /clord-init
        thread.send.assert_called()
        error_msg = thread.send.call_args_list[0].args[0]
        assert "/clord-init" in error_msg


# ---------------------------------------------------------------------------
# Phase 1: start_session — error when unbound
# ---------------------------------------------------------------------------


class TestStartSessionUnboundChannel:
    """/clord in an unbound channel should send an ephemeral error."""

    @pytest.mark.asyncio
    async def test_start_session_unbound_channel_sends_error(self) -> None:
        cog = _make_cog()

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 1
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        # Non-thread channel (new session path)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 777  # unbound
        interaction.channel = channel
        interaction.channel_id = 777

        await cog.start_session.callback(cog, interaction, prompt="hello")

        # Should have responded with error before creating a thread
        interaction.response.send_message.assert_called_once()
        error_msg = interaction.response.send_message.call_args.args[0]
        assert "/clord-init" in error_msg
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


# ---------------------------------------------------------------------------
# #522: several c-lord instances can share a guild
# ---------------------------------------------------------------------------


class TestForeignChannelStaysSilent:
    """A text command aimed at another instance's channel must not be answered.

    ``!clord`` reaches every instance that can read the channel, so answering
    an unbound channel means each bystander bot posts the same public warning
    (and one holding a stale binding spawns a real session). Slash commands are
    ephemeral — only the invoker sees them — so those keep explaining.
    """

    def _text_message(self) -> MagicMock:
        message = MagicMock(spec=discord.Message)
        message.id = 1
        message.webhook_id = 4242  # webhook-driven, as `!clord` E2E is
        message.author = MagicMock()
        return message

    @pytest.mark.asyncio
    async def test_text_command_in_a_foreign_unbound_channel_says_nothing(self) -> None:
        cog = _make_cog()
        cog.spawn_session = AsyncMock()  # type: ignore[method-assign]
        respond = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 777  # not bot.channel_id (999), no binding

        await cog._clord_impl(
            channel=channel,
            channel_id_fallback=channel.id,
            user=MagicMock(),
            prompt="やること",
            respond=respond,
            ack=AsyncMock(),
            message=self._text_message(),
        )

        respond.assert_not_awaited()
        cog.spawn_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_text_command_in_a_foreign_unbound_thread_says_nothing(self) -> None:
        cog = _make_cog()
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]
        respond = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 601
        thread.parent_id = 777
        thread.send = AsyncMock(return_value=MagicMock())

        await cog._clord_impl(
            channel=thread,
            channel_id_fallback=None,
            user=MagicMock(),
            prompt="やること",
            respond=respond,
            ack=AsyncMock(),
            message=self._text_message(),
        )

        respond.assert_not_awaited()
        cog._run_claude.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slash_in_an_unbound_channel_still_explains(self) -> None:
        """Ephemeral, so it reaches only the invoker — no cross-bot noise."""
        cog = _make_cog()
        cog.spawn_session = AsyncMock()  # type: ignore[method-assign]
        respond = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 777

        await cog._clord_impl(
            channel=channel,
            channel_id_fallback=channel.id,
            user=MagicMock(),
            prompt="やること",
            respond=respond,
            ack=AsyncMock(),
            message=None,
        )

        respond.assert_awaited()
        assert "/clord-init" in str(respond.await_args.args[0])

    @pytest.mark.asyncio
    async def test_text_command_in_our_own_unbound_channel_still_explains(self) -> None:
        """This instance *is* configured for the channel, so the user gets help."""
        cog = _make_cog()
        cog.spawn_session = AsyncMock()  # type: ignore[method-assign]
        respond = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 999  # == bot.channel_id

        await cog._clord_impl(
            channel=channel,
            channel_id_fallback=channel.id,
            user=MagicMock(),
            prompt="やること",
            respond=respond,
            ack=AsyncMock(),
            message=self._text_message(),
        )

        respond.assert_awaited()
        assert "/clord-init" in str(respond.await_args.args[0])
