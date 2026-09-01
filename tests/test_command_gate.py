"""One gate for message-backed command invocations (#596, #508).

Two questions live here, and #596 AC4 asks whether they are the same bug:

* **担当 (#522 → #596)** — *which instance* answers.  A text command reaches
  every c-lord that can read the channel, so without this the whole fleet runs
  it (three staging bots stopped two production workspaces).
* **認可 (#507 → #508)** — *who* may drive it.  Commands that gate on the human
  allowlist reject the webhook pseudo-user by construction.

Same root — no shared gate for message-backed invocations — two axes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.bot import ClaudeDiscordBot
from c_lord.command_gate import is_message_authorized, owns, owns_channel

HOME = 111
FOREIGN = 222


def _bot(channel_id: int = HOME) -> MagicMock:
    bot = MagicMock()
    bot.channel_id = channel_id
    bot.get_cog.return_value = None
    bot.session_repo = None
    return bot


def _channel_cog(*, tmux: object = None, sdm: object = None) -> MagicMock:
    cog = MagicMock()
    cog.resolve_tmux_manager = AsyncMock(return_value=tmux)
    cog.resolve_manager = AsyncMock(return_value=sdm)
    return cog


# ── 担当判定 (#596) ────────────────────────────────────────────────────────


class TestOwns:
    """Which instance is responsible for a channel / thread."""

    @pytest.mark.asyncio
    async def test_home_channel_is_ours(self) -> None:
        assert await owns(home_channel_id=HOME, channel_id=HOME) is True

    @pytest.mark.asyncio
    async def test_thread_under_home_channel_is_ours(self) -> None:
        assert await owns(home_channel_id=HOME, channel_id=HOME, thread_id=999) is True

    @pytest.mark.asyncio
    async def test_foreign_channel_without_any_trace_is_not_ours(self) -> None:
        """The #596 case: a production thread seen by a staging bot."""
        assert await owns(home_channel_id=HOME, channel_id=FOREIGN, thread_id=999) is False

    @pytest.mark.asyncio
    async def test_a_binding_makes_it_ours(self) -> None:
        resolve = AsyncMock(return_value=object())
        assert await owns(home_channel_id=HOME, channel_id=FOREIGN, resolvers=(resolve,)) is True
        resolve.assert_awaited_once_with(FOREIGN, thread_id=None)

    @pytest.mark.asyncio
    async def test_the_second_resolver_is_consulted_when_the_first_finds_nothing(self) -> None:
        first = AsyncMock(return_value=None)
        second = AsyncMock(return_value=object())
        assert (
            await owns(home_channel_id=HOME, channel_id=FOREIGN, resolvers=(first, second)) is True
        )

    @pytest.mark.asyncio
    async def test_session_row_makes_the_thread_ours(self) -> None:
        """A thread we started still belongs to us if its channel lost its binding."""
        assert (
            await owns(
                home_channel_id=HOME,
                channel_id=FOREIGN,
                thread_id=999,
                session_get=AsyncMock(return_value={"thread_id": 999}),
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_session_row_is_only_consulted_for_threads(self) -> None:
        session_get = AsyncMock(return_value={"thread_id": 999})
        assert (
            await owns(home_channel_id=HOME, channel_id=FOREIGN, session_get=session_get) is False
        )
        session_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failing_lookup_does_not_claim_the_channel(self) -> None:
        """A DB hiccup must not turn a bystander into the owner."""
        assert (
            await owns(
                home_channel_id=HOME,
                channel_id=FOREIGN,
                thread_id=999,
                resolvers=(AsyncMock(side_effect=RuntimeError("db down")),),
                session_get=AsyncMock(side_effect=RuntimeError("db down")),
            )
            is False
        )


class TestOwnsChannel:
    """The bot-shaped entry point used by ``process_commands``."""

    @pytest.mark.asyncio
    async def test_thread_resolves_parent_and_thread_ids(self) -> None:
        bot = _bot()
        cog = _channel_cog()
        bot.get_cog.return_value = cog
        thread = MagicMock(spec=discord.Thread)
        thread.id = 999
        thread.parent_id = FOREIGN

        assert await owns_channel(bot, thread) is False
        cog.resolve_tmux_manager.assert_awaited_once_with(FOREIGN, thread_id=999)

    @pytest.mark.asyncio
    async def test_a_channel_binding_makes_it_ours(self) -> None:
        bot = _bot()
        bot.get_cog.return_value = _channel_cog(tmux=object())
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = FOREIGN
        assert await owns_channel(bot, channel) is True

    @pytest.mark.asyncio
    async def test_a_session_row_makes_the_thread_ours(self) -> None:
        bot = _bot()
        bot.session_repo = MagicMock()
        bot.session_repo.get = AsyncMock(return_value={"thread_id": 999})
        thread = MagicMock(spec=discord.Thread)
        thread.id = 999
        thread.parent_id = FOREIGN
        assert await owns_channel(bot, thread) is True

    @pytest.mark.asyncio
    async def test_dm_is_always_ours(self) -> None:
        """A DM reaches exactly one bot, so there is nobody to defer to."""
        dm = MagicMock(spec=discord.DMChannel)
        dm.id = FOREIGN
        dm.guild = None  # what a real DMChannel reports
        assert await owns_channel(_bot(), dm) is True

    @pytest.mark.asyncio
    async def test_channel_we_cannot_identify_keeps_working(self) -> None:
        """No channel to compare against: fall back to running, not to silence."""
        assert await owns_channel(_bot(), None) is True


# ── 認可判定 (#508) ────────────────────────────────────────────────────────


def _message(*, webhook_id: int | None = None, bot_author: bool = False, author_id: int = 7):
    msg = MagicMock(spec=discord.Message)
    msg.webhook_id = webhook_id
    msg.author = MagicMock()
    msg.author.bot = bot_author
    msg.author.id = author_id
    return msg


class TestIsMessageAuthorized:
    def test_webhook_bypasses_the_human_allowlist(self) -> None:
        assert is_message_authorized(_message(webhook_id=1), lambda _u: False) is True

    def test_trusted_bot_bypasses_the_human_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLORD_TRUSTED_BOT_IDS", "42")
        msg = _message(bot_author=True, author_id=42)
        assert is_message_authorized(msg, lambda _u: False) is True

    def test_untrusted_bot_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_TRUSTED_BOT_IDS", "42")
        msg = _message(bot_author=True, author_id=43)
        assert is_message_authorized(msg, lambda _u: True) is False

    def test_human_still_goes_through_the_allowlist(self) -> None:
        msg = _message()
        assert is_message_authorized(msg, lambda _u: False) is False
        assert is_message_authorized(msg, lambda _u: True) is True


# ── process_commands: the single choke point (#596 AC3) ────────────────────


class TestProcessCommandsGate:
    """Every text command goes through here, so the gate covers all of them."""

    @staticmethod
    def _prepare(bot: ClaudeDiscordBot, *, command: object) -> tuple[MagicMock, MagicMock]:
        ctx = MagicMock()
        ctx.command = command
        bot.get_context = AsyncMock(return_value=ctx)
        bot.invoke = AsyncMock()
        return ctx, bot.invoke  # type: ignore[return-value]

    @staticmethod
    def _webhook_message(channel: object) -> MagicMock:
        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.bot = True
        msg.webhook_id = 999
        msg.channel = channel
        return msg

    @pytest.mark.asyncio
    async def test_command_in_another_instances_thread_is_dropped_silently(self) -> None:
        """#596 RED: `!workspace-stop` in a thread this instance does not own."""
        bot = ClaudeDiscordBot(channel_id=HOME)
        _, invoke = self._prepare(bot, command=MagicMock())
        thread = MagicMock(spec=discord.Thread)
        thread.id = 999
        thread.parent_id = FOREIGN

        await bot.process_commands(self._webhook_message(thread))

        invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_command_in_our_own_channel_runs(self) -> None:
        bot = ClaudeDiscordBot(channel_id=HOME)
        _, invoke = self._prepare(bot, command=MagicMock())
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = HOME

        await bot.process_commands(self._webhook_message(channel))

        invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_command_message_is_left_alone(self) -> None:
        """No command matched — the gate must not change CommandNotFound handling."""
        bot = ClaudeDiscordBot(channel_id=HOME)
        _, invoke = self._prepare(bot, command=None)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = FOREIGN

        await bot.process_commands(self._webhook_message(channel))

        invoke.assert_called_once()
