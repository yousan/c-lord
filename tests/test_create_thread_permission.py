"""Tests for #443: /clord が権限不足のチャンネルでスレッドを作れない時、黙らず案内する。

When the bot is denied View Channel / Create Public Threads on a channel,
``channel.create_thread()`` raises ``discord.Forbidden``.  Previously this
propagated unhandled (the slash command's ``on_app_command_error`` only logs),
so the user saw *nothing* — the session simply did not start.  These tests
assert the failure is turned into an actionable, user-facing message that names
the missing permissions, delivered ephemerally so it reaches the user even
though the bot cannot see (or post in) the channel.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.discord_ui.permission_help import ThreadCreateForbiddenError


def _forbidden() -> discord.Forbidden:
    """Build a realistic 403 Missing Access error like Discord raises."""
    resp = MagicMock()
    resp.status = 403
    resp.reason = "Forbidden"
    return discord.Forbidden(resp, "Missing Access")


def _make_cog() -> ClaudeChatCog:
    """Return a ClaudeChatCog with a repo binding present (passes the unbound guard)."""
    from c_lord.cogs.channel_repo import ChannelRepoCog

    bot = MagicMock()
    bot.channel_id = 999
    channel_cog = MagicMock(spec=ChannelRepoCog)
    channel_cog.resolve_manager = AsyncMock(return_value=MagicMock())
    channel_cog.resolve_tmux_manager = AsyncMock(return_value=MagicMock())
    bot.get_cog = MagicMock(return_value=channel_cog)
    bot.settings_repo = None
    bot.thread_dashboard = None
    bot.ask_repo = None
    bot.lounge_repo = None
    bot.resume_repo = None

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.working_dir = "/tmp/test"
    runner.model = "sonnet"
    runner.timeout_seconds = 300
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner, settings_repo=None)


class TestSpawnSessionCreateThreadForbidden:
    """New-thread path: ``channel.create_thread()`` denied."""

    @pytest.mark.asyncio
    async def test_create_thread_forbidden_raises_thread_create_forbidden(self) -> None:
        cog = _make_cog()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 777
        channel.create_thread = AsyncMock(side_effect=_forbidden())
        channel.send = AsyncMock()

        with pytest.raises(ThreadCreateForbiddenError):
            await cog.spawn_session(channel, "hello")

        # The bot was denied View Channel — posting into the channel would fail
        # too, so spawn_session must NOT attempt channel.send for this case.
        channel.send.assert_not_called()


class TestClordImplCreateThreadForbidden:
    """/clord (and !clord) channel path: surface the permission help ephemerally."""

    @pytest.mark.asyncio
    async def test_forbidden_on_create_thread_returns_permission_help(self) -> None:
        cog = _make_cog()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 777
        channel.create_thread = AsyncMock(side_effect=_forbidden())

        user = MagicMock()
        user.id = 1

        responses: list[tuple[object, dict]] = []

        async def respond(content=None, **kwargs):
            responses.append((content, kwargs))

        async def ack(*_a, **_k):
            return None

        cog._run_claude = AsyncMock()

        await cog._clord_impl(
            channel=channel,
            channel_id_fallback=777,
            user=user,
            prompt="hello",
            respond=respond,
            ack=ack,
        )

        assert responses, "no response was sent to the user on create_thread Forbidden"
        joined = " ".join(str(c) for c, _ in responses)
        # Names the actually-missing permissions (not just "send").
        assert "チャンネルを見る" in joined
        assert "公開スレッドの作成" in joined
        # Delivered ephemerally — the channel is invisible to the bot, so the
        # interaction is the only path that reaches the user.
        assert any(kw.get("ephemeral") for _, kw in responses)
        # The failed thread creation must not have started a Claude run.
        cog._run_claude.assert_not_called()


class TestHandleNewConversationCreateThreadForbidden:
    """Message-trigger path: ``message.create_thread()`` denied (lacks Create Public Threads)."""

    @pytest.mark.asyncio
    async def test_forbidden_on_message_create_thread_replies_in_channel(self) -> None:
        cog = _make_cog()

        message = MagicMock(spec=discord.Message)
        message.id = 55555
        message.content = "do the thing"
        message.create_thread = AsyncMock(side_effect=_forbidden())
        message.reply = AsyncMock()

        # Should not raise; should reply in-channel (the bot CAN see this
        # channel — it received the message) and not start a run.
        cog._run_claude = AsyncMock()
        cog._build_prompt_and_images = AsyncMock(return_value=("p", []))

        await cog._handle_new_conversation(message)

        message.reply.assert_awaited()
        notice = message.reply.await_args.args[0]
        assert "公開スレッドの作成" in notice
        cog._run_claude.assert_not_called()
