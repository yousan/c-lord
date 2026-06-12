"""Tests for issue #75: bot に送信権限がない時、ユーザー向けにエラーを返す.

When the bot lacks "Send Messages" / "Send Messages in Threads" permission, the
active seed-message sends (`channel.send` / `thread.send`) raise
``discord.Forbidden`` (403 Missing Access).  Previously this propagated as an
unhandled traceback in the server log and the user got *no* feedback on Discord.

These tests assert that the Forbidden is caught and converted into a clear,
user-facing message that names the missing permissions — without swallowing the
error silently (it must still be logged).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog


def _forbidden() -> discord.Forbidden:
    """Build a realistic 403 Missing Access error like Discord raises."""
    resp = MagicMock()
    resp.status = 403
    resp.reason = "Forbidden"
    return discord.Forbidden(resp, "Missing Access")


def _make_cog() -> ClaudeChatCog:
    """Return a ClaudeChatCog with a binding present (so we reach the send)."""
    from c_lord.cogs.channel_repo import ChannelRepoCog

    bot = MagicMock()
    bot.channel_id = 999
    # ChannelRepoCog returns a non-None manager so the unbound-channel guard
    # passes and execution reaches the seed-message send.  spec=ChannelRepoCog
    # is required so the isinstance() guard in the resolvers passes.
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


class TestClordImplThreadSendForbidden:
    """Thread-continue path: `seed_message = await channel.send(prompt)` (#75)."""

    @pytest.mark.asyncio
    async def test_forbidden_on_thread_send_returns_user_facing_error(self) -> None:
        cog = _make_cog()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 12345
        thread.parent_id = 777
        thread.send = AsyncMock(side_effect=_forbidden())

        user = MagicMock()
        user.id = 1

        responses: list[tuple[tuple, dict]] = []

        async def respond(content=None, **kwargs):
            responses.append((content, kwargs))

        async def ack(*_a, **_k):
            return None

        # _run_claude must NOT be invoked once the seed send fails.
        cog._run_claude = AsyncMock()

        await cog._clord_impl(
            channel=thread,
            channel_id_fallback=777,
            user=user,
            prompt="hello",
            respond=respond,
            ack=ack,
        )

        # A user-facing error must have been produced (not a raised traceback).
        assert responses, "no response was sent to the user on Forbidden"
        joined = " ".join(str(c) for c, _ in responses)
        assert "権限" in joined  # mentions permission
        # Names the required permission(s).
        assert "メッセージ" in joined
        # The failed seed must not have triggered a Claude run.
        cog._run_claude.assert_not_called()


class TestSpawnSessionSendForbidden:
    """New-thread path: `seed_message = await thread.send(prompt)` (#75)."""

    @pytest.mark.asyncio
    async def test_forbidden_on_spawn_seed_send_notifies_parent_channel(self) -> None:
        cog = _make_cog()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 22222
        thread.send = AsyncMock(side_effect=_forbidden())
        thread.mention = "<#22222>"

        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 777
        channel.create_thread = AsyncMock(return_value=thread)
        channel.send = AsyncMock()

        # When the seed send into the new thread fails with Forbidden, the user
        # should be told *in the parent channel* (the thread is unreachable),
        # and the error must be re-raised so the caller knows the spawn failed —
        # not silently swallowed.
        with pytest.raises(discord.Forbidden):
            await cog.spawn_session(channel, "hello")

        channel.send.assert_awaited()
        notice = channel.send.await_args.args[0]
        assert "権限" in notice
        assert "メッセージ" in notice
