"""``/clear`` and ``!clear`` must be gated like every other command that drives c-lord (#405).

``/clear`` is the most destructive command in the chat cog: it kills the active
runner, kills the thread's tmux window unconditionally and resets the session
row — the conversation is gone.  The neighbouring ``/clord-attach`` /
``!attach`` were gated, ``/clear`` / ``!clear`` were not, so anyone who could
type in a thread could wipe somebody else's session.

The gate is the shared one, not a new rule:

* slash → :class:`~c_lord.discord_ui.authorization.Authorizer` (the human
  allowlist; a slash command can never come from a webhook),
* text → :func:`c_lord.command_gate.is_message_authorized`, which lets webhook
  messages through — that is what keeps the E2E webhook path
  (``tests/e2e/test_text_command_twins.py``) working with an owner configured.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.discord_ui.authorization import Authorizer

OWNER = 499163459418587176
STRANGER = 12345
WEBHOOK_AUTHOR = 987654321
THREAD_ID = 4242


def _make_cog() -> tuple[ClaudeChatCog, MagicMock, MagicMock]:
    """A cog with ``DISCORD_OWNER_ID`` configured, a live runner and a tmux window."""
    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    repo = MagicMock()
    repo.reset = AsyncMock(return_value=True)
    cog = ClaudeChatCog(
        bot=bot,
        repo=repo,
        runner=MagicMock(),
        authorizer=Authorizer(allowed_user_ids={OWNER}),
    )
    tmux_manager = MagicMock()
    tmux_manager.kill_session = MagicMock(return_value=True)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
    runner = MagicMock()
    runner.kill = AsyncMock()
    cog._active_runners[THREAD_ID] = runner
    return cog, tmux_manager, runner


def _thread() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.parent_id = 999
    return thread


def _member(user_id: int) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = False
    member.roles = []
    return member


def _interaction(user_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = _thread()
    interaction.user = _member(user_id)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _ctx(message: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.channel = _thread()
    ctx.author = message.author
    ctx.message = message
    ctx.send = AsyncMock()
    return ctx


def _human_message(user_id: int) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.webhook_id = None
    msg.author = _member(user_id)
    return msg


def _webhook_message() -> MagicMock:
    """A webhook message: bot-authored, ``webhook_id`` set, id in no allowlist."""
    msg = MagicMock(spec=discord.Message)
    msg.webhook_id = 555
    msg.author = MagicMock()
    msg.author.bot = True
    msg.author.id = WEBHOOK_AUTHOR
    return msg


def _bot_message(user_id: int) -> MagicMock:
    """Another bot, not a webhook and not in ``CLORD_TRUSTED_BOT_IDS``."""
    msg = MagicMock(spec=discord.Message)
    msg.webhook_id = None
    msg.author = MagicMock()
    msg.author.bot = True
    msg.author.id = user_id
    return msg


def _assert_untouched(cog: ClaudeChatCog, tmux_manager: MagicMock, runner: MagicMock) -> None:
    """Nothing about the session may have been destroyed (#405 AC3)."""
    runner.kill.assert_not_called()
    assert cog._active_runners.get(THREAD_ID) is runner
    tmux_manager.kill_session.assert_not_called()
    cog.repo.reset.assert_not_called()


def _assert_cleared(cog: ClaudeChatCog, tmux_manager: MagicMock, runner: MagicMock) -> None:
    runner.kill.assert_called_once()
    assert THREAD_ID not in cog._active_runners
    tmux_manager.kill_session.assert_called_once_with(THREAD_ID)
    cog.repo.reset.assert_called_once_with(THREAD_ID)


class TestSlashClear:
    @pytest.mark.asyncio
    async def test_stranger_is_rejected_and_session_survives(self) -> None:
        """#405 AC1 + AC3."""
        cog, tmux_manager, runner = _make_cog()
        interaction = _interaction(STRANGER)

        await cog.clear_session.callback(cog, interaction)

        _assert_untouched(cog, tmux_manager, runner)
        interaction.response.send_message.assert_called_once()
        args, kwargs = interaction.response.send_message.call_args
        assert "not authorized" in (args[0] if args else kwargs.get("content", ""))
        assert kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_allowed_user_still_clears(self) -> None:
        cog, tmux_manager, runner = _make_cog()

        await cog.clear_session.callback(cog, _interaction(OWNER))

        _assert_cleared(cog, tmux_manager, runner)


class TestTextClear:
    @pytest.mark.asyncio
    async def test_stranger_is_rejected_and_session_survives(self) -> None:
        """#405 AC2 + AC3 — the same gate as ``/clear`` for a human."""
        cog, tmux_manager, runner = _make_cog()
        ctx = _ctx(_human_message(STRANGER))

        await cog.clear_text.callback(cog, ctx)

        _assert_untouched(cog, tmux_manager, runner)
        ctx.send.assert_called_once()
        assert "not authorized" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_untrusted_bot_is_rejected(self) -> None:
        cog, tmux_manager, runner = _make_cog()
        ctx = _ctx(_bot_message(WEBHOOK_AUTHOR))

        await cog.clear_text.callback(cog, ctx)

        _assert_untouched(cog, tmux_manager, runner)

    @pytest.mark.asyncio
    async def test_allowed_user_still_clears(self) -> None:
        cog, tmux_manager, runner = _make_cog()

        await cog.clear_text.callback(cog, _ctx(_human_message(OWNER)))

        _assert_cleared(cog, tmux_manager, runner)

    @pytest.mark.asyncio
    async def test_webhook_still_clears_with_an_owner_configured(self) -> None:
        """#405 AC2 — the E2E webhook path keeps working (possession of the URL is the grant)."""
        cog, tmux_manager, runner = _make_cog()

        await cog.clear_text.callback(cog, _ctx(_webhook_message()))

        _assert_cleared(cog, tmux_manager, runner)
