"""Commands that change state refuse a stranger and do nothing (#781).

The structural half — every command reaches the gate — is
``tests/test_command_authorization_coverage.py``. This file checks what the gate
*does* when it is reached: a user outside the allowlist gets "not authorized"
and nothing is touched; the owner, a webhook and a trusted bot still get
through (the E2E twins in ``tests/e2e/test_text_command_twins.py`` drive the
text commands from a webhook).

The gate is the shared rule, not a new one (#781 AC3): slash → the
:class:`Authorizer`, text → :func:`c_lord.command_gate.is_message_authorized`.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from c_lord.cogs.auto_upgrade import AutoUpgradeCog, UpgradeConfig
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.cogs.session_manage import SETTING_CLAUDE_MODEL, SessionManageCog
from c_lord.discord_ui.authorization import Authorizer, set_default_authorizer
from tests.test_command_authorization_coverage import PUBLIC_COMMANDS

OWNER = 499163459418587176
STRANGER = 12345
THREAD_ID = 4242


def _member(user_id: int) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = False
    member.roles = []
    return member


def _thread() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.parent_id = 999
    return thread


def _interaction(user_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = _thread()
    interaction.user = _member(user_id)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _message(user_id: int, *, webhook: bool = False, bot: bool = False) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.webhook_id = 555 if webhook else None
    msg.author = _member(user_id)
    msg.author.bot = bot or webhook
    return msg


def _ctx(message: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.channel = _thread()
    ctx.author = message.author
    ctx.message = message
    ctx.send = AsyncMock()
    return ctx


def _fill(callback: object) -> dict[str, object]:
    """Placeholder arguments for a command's required parameters.

    The gate runs before any of them is looked at, so their values do not matter
    — they only have to exist for the call to be made at all.
    """
    params = list(inspect.signature(callback).parameters.values())[2:]  # self, interaction/ctx
    return {p.name: "x" for p in params if p.default is inspect.Parameter.empty}


def _session_manage_cog() -> tuple[SessionManageCog, MagicMock, MagicMock]:
    bot = MagicMock()
    repo = MagicMock()
    settings_repo = MagicMock()
    settings_repo.set = AsyncMock()
    settings_repo.get = AsyncMock(return_value=None)
    cog = SessionManageCog(
        bot=bot,
        repo=repo,
        settings_repo=settings_repo,
        authorizer=Authorizer(allowed_user_ids={OWNER}),
    )
    return cog, repo, settings_repo


def _slash_commands(cog: commands.Cog) -> list[app_commands.Command]:
    return [c for c in cog.walk_app_commands() if isinstance(c, app_commands.Command)]


def _is_public(command: app_commands.Command | commands.Command) -> bool:
    prefix = "/" if isinstance(command, app_commands.Command) else "!"
    return f"{prefix}{command.qualified_name}" in PUBLIC_COMMANDS


def _session_manage_slash() -> list[str]:
    cog, _, _ = _session_manage_cog()
    return [c.qualified_name for c in _slash_commands(cog) if not _is_public(c)]


def _session_manage_text() -> list[str]:
    cog, _, _ = _session_manage_cog()
    return [c.qualified_name for c in cog.get_commands() if not _is_public(c)]


def _assert_refused_slash(interaction: MagicMock) -> None:
    interaction.response.send_message.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "not authorized" in (args[0] if args else kwargs.get("content", ""))
    assert kwargs.get("ephemeral") is True
    interaction.response.defer.assert_not_called()


def _assert_refused_text(ctx: MagicMock) -> None:
    ctx.send.assert_called_once()
    assert "not authorized" in ctx.send.call_args.args[0]


class TestSessionManageRefusesStrangers:
    """Every non-public SessionManageCog command — none may be skipped (#781 AC1)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", _session_manage_slash())
    async def test_slash(self, name: str) -> None:
        cog, repo, settings_repo = _session_manage_cog()
        command = next(c for c in _slash_commands(cog) if c.qualified_name == name)
        interaction = _interaction(STRANGER)

        await command.callback(cog, interaction, **_fill(command.callback))  # type: ignore[arg-type]

        _assert_refused_slash(interaction)
        assert repo.method_calls == []
        settings_repo.set.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", _session_manage_text())
    async def test_text(self, name: str) -> None:
        cog, repo, settings_repo = _session_manage_cog()
        command = cog.get_command(name) if hasattr(cog, "get_command") else None
        command = command or next(c for c in cog.get_commands() if c.qualified_name == name)
        ctx = _ctx(_message(STRANGER))

        await command.callback(cog, ctx, **_fill(command.callback))  # type: ignore[arg-type]

        _assert_refused_text(ctx)
        assert repo.method_calls == []
        settings_repo.set.assert_not_called()

    def test_the_destructive_ones_are_in_the_parametrization(self) -> None:
        """If a rename dropped one of these, the tests above would silently shrink."""
        slash, text = _session_manage_slash(), _session_manage_text()
        for name in ("workspace-delete", "workspace-stop", "model set", "workspace-cleanup"):
            assert name in slash
        for name in ("workspace-delete", "workspace-stop", "model-set", "workspace-cleanup"):
            assert name in text


class TestSessionManageLetsTheRightPeopleThrough:
    @pytest.mark.asyncio
    async def test_owner_can_set_the_model(self) -> None:
        cog, _, settings_repo = _session_manage_cog()

        await cog.model_set.callback(cog, _interaction(OWNER), "opus")

        settings_repo.set.assert_awaited_once_with(SETTING_CLAUDE_MODEL, "opus")

    @pytest.mark.asyncio
    async def test_webhook_can_set_the_model(self) -> None:
        """#781 AC4: text twins stay drivable from a webhook (E2E, CI/CD)."""
        cog, _, settings_repo = _session_manage_cog()

        await cog.model_set_text.callback(cog, _ctx(_message(777, webhook=True)), "opus")

        settings_repo.set.assert_awaited_once_with(SETTING_CLAUDE_MODEL, "opus")

    @pytest.mark.asyncio
    async def test_untrusted_bot_is_refused(self) -> None:
        cog, _, settings_repo = _session_manage_cog()
        ctx = _ctx(_message(777, bot=True))

        await cog.model_set_text.callback(cog, ctx, "opus")

        _assert_refused_text(ctx)
        settings_repo.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_public_command_needs_no_allowlist(self) -> None:
        cog, repo, _ = _session_manage_cog()
        repo.get = AsyncMock(return_value=None)
        interaction = _interaction(STRANGER)

        await cog.model_show.callback(cog, interaction)

        _, kwargs = interaction.response.send_message.call_args
        assert "embed" in kwargs

    @pytest.mark.asyncio
    async def test_unwired_cog_uses_the_process_authorizer(self) -> None:
        """A consumer building the cog without ``authorizer=`` still gets the allowlist.

        Not a blank ``Authorizer()``: with an allowlist configured, a blank one
        denies everyone including the owner (#739).
        """
        set_default_authorizer(Authorizer(allowed_user_ids={OWNER}))
        try:
            bot = MagicMock(spec=commands.Bot)  # no ``authorizer`` attribute
            settings_repo = MagicMock()
            settings_repo.set = AsyncMock()
            cog = SessionManageCog(bot=bot, repo=MagicMock(), settings_repo=settings_repo)

            stranger = _interaction(STRANGER)
            await cog.model_set.callback(cog, stranger, "opus")
            _assert_refused_slash(stranger)

            await cog.model_set.callback(cog, _interaction(OWNER), "opus")
            settings_repo.set.assert_awaited_once_with(SETTING_CLAUDE_MODEL, "opus")
        finally:
            set_default_authorizer(None)

    @pytest.mark.asyncio
    async def test_no_authorizer_anywhere_denies(self) -> None:
        """Nothing to check against → deny, never allow (#713)."""
        set_default_authorizer(None)
        bot = MagicMock(spec=commands.Bot)
        settings_repo = MagicMock()
        settings_repo.set = AsyncMock()
        cog = SessionManageCog(bot=bot, repo=MagicMock(), settings_repo=settings_repo)
        interaction = _interaction(OWNER)

        await cog.model_set.callback(cog, interaction, "opus")

        _assert_refused_slash(interaction)
        settings_repo.set.assert_not_called()


# ── ClaudeChatCog: /stop, /claude-restart, /restart-claude, /compact ──────────


def _chat_cog() -> tuple[ClaudeChatCog, MagicMock, MagicMock]:
    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(closed_at=None))
    cog = ClaudeChatCog(
        bot=bot,
        repo=repo,
        runner=MagicMock(),
        authorizer=Authorizer(allowed_user_ids={OWNER}),
    )
    tmux_manager = MagicMock()
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
    runner = MagicMock()
    runner.kill = AsyncMock()
    runner.interrupt = AsyncMock()
    cog._active_runners[THREAD_ID] = runner
    return cog, tmux_manager, runner


_CHAT_SLASH = ["stop_session", "claude_restart", "restart_claude", "compact_session"]
_CHAT_TEXT = ["stop_text", "claude_restart_text", "restart_claude_text", "compact_text"]


class TestChatCommandsRefuseStrangers:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("attr", _CHAT_SLASH)
    async def test_slash(self, attr: str) -> None:
        cog, tmux_manager, runner = _chat_cog()
        interaction = _interaction(STRANGER)

        await getattr(cog, attr).callback(cog, interaction)

        _assert_refused_slash(interaction)
        runner.interrupt.assert_not_called()
        runner.kill.assert_not_called()
        assert tmux_manager.method_calls == []
        assert cog._active_runners.get(THREAD_ID) is runner

    @pytest.mark.asyncio
    @pytest.mark.parametrize("attr", _CHAT_TEXT)
    async def test_text(self, attr: str) -> None:
        cog, tmux_manager, runner = _chat_cog()
        ctx = _ctx(_message(STRANGER))

        await getattr(cog, attr).callback(cog, ctx)

        _assert_refused_text(ctx)
        runner.interrupt.assert_not_called()
        runner.kill.assert_not_called()
        assert tmux_manager.method_calls == []

    @pytest.mark.asyncio
    async def test_owner_can_stop(self) -> None:
        cog, _, runner = _chat_cog()

        await cog.stop_session.callback(cog, _interaction(OWNER))

        runner.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_webhook_can_stop(self) -> None:
        """#781 AC4: ``!stop`` from a webhook still reaches the runner."""
        cog, _, runner = _chat_cog()

        await cog.stop_text.callback(cog, _ctx(_message(777, webhook=True)))

        runner.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_owner_can_restart(self) -> None:
        cog, tmux_manager, runner = _chat_cog()

        await cog.claude_restart.callback(cog, _interaction(OWNER))

        runner.kill.assert_awaited_once()
        tmux_manager.kill_session.assert_called_once_with(THREAD_ID)


# ── AutoUpgradeCog: /upgrade ──────────────────────────────────────────────────


class TestUpgradeRefusesStrangers:
    def _cog(self) -> AutoUpgradeCog:
        bot = MagicMock()
        bot.authorizer = Authorizer(allowed_user_ids={OWNER})
        bot.settings_repo = None
        cog = AutoUpgradeCog(bot, UpgradeConfig(package_name="pkg", slash_command_enabled=True))
        cog._run_pipeline = AsyncMock()  # type: ignore[method-assign]
        return cog

    @pytest.mark.asyncio
    async def test_stranger_is_refused(self) -> None:
        cog = self._cog()
        interaction = _interaction(STRANGER)
        interaction.channel = MagicMock(spec=discord.TextChannel)
        interaction.channel.create_thread = AsyncMock()

        await cog.upgrade_command.callback(cog, interaction)

        _assert_refused_slash(interaction)
        interaction.channel.create_thread.assert_not_called()
        cog._run_pipeline.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_owner_can_upgrade(self) -> None:
        cog = self._cog()
        interaction = _interaction(OWNER)
        interaction.channel = MagicMock(spec=discord.TextChannel)
        interaction.channel.create_thread = AsyncMock(return_value=MagicMock())

        await cog.upgrade_command.callback(cog, interaction)

        cog._run_pipeline.assert_awaited_once()  # type: ignore[attr-defined]
