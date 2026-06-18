"""Tests for #444: last-resort error surfacing ("safety net").

Before #444, an unhandled exception in a slash command, text command, or a
View button callback left the user with nothing actionable: slash commands
showed "アプリが応答しませんでした", text commands failed silently, and buttons
showed "インタラクションに失敗しました". These tests assert each path now logs AND
replies to the user with a sanitized, actionable message.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from c_lord.bot import ClaudeDiscordBot
from c_lord.discord_ui.error_reporting import ErrorReportingViewMixin
from c_lord.discord_ui.permission_help import (
    PERMISSION_DENIED_HELP,
    UNEXPECTED_ERROR_HELP,
    command_error_help,
)


def _forbidden() -> discord.Forbidden:
    resp = MagicMock()
    resp.status = 403
    resp.reason = "Forbidden"
    return discord.Forbidden(resp, "Missing Access")


class TestCommandErrorHelp:
    """command_error_help() maps an exception to a user-facing message."""

    def test_forbidden_maps_to_permission_help(self) -> None:
        assert command_error_help(_forbidden()) == PERMISSION_DENIED_HELP

    def test_wrapped_forbidden_maps_to_permission_help(self) -> None:
        # discord.py wraps callback exceptions in CommandInvokeError(.original).
        wrapped = app_commands.CommandInvokeError(MagicMock(), _forbidden())
        assert command_error_help(wrapped) == PERMISSION_DENIED_HELP

    def test_generic_error_maps_to_unexpected(self) -> None:
        msg = command_error_help(RuntimeError("boom"))
        assert msg == UNEXPECTED_ERROR_HELP
        # Must not leak the raw exception text / traceback to the user.
        assert "boom" not in msg


class TestOnAppCommandErrorReplies:
    """on_app_command_error logs AND replies ephemerally (was log-only)."""

    @pytest.mark.asyncio
    async def test_replies_ephemeral_when_not_yet_responded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bot = ClaudeDiscordBot(channel_id=123)
        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()

        error = app_commands.CommandInvokeError(MagicMock(), _forbidden())
        with caplog.at_level(logging.ERROR, logger="c_lord.bot"):
            await bot.on_app_command_error(interaction, error)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
        assert PERMISSION_DENIED_HELP in interaction.response.send_message.call_args.args[0]
        # Still logged (regression guard for the pre-#444 behavior).
        assert caplog.records

    @pytest.mark.asyncio
    async def test_uses_followup_when_already_responded(self) -> None:
        bot = ClaudeDiscordBot(channel_id=123)
        interaction = MagicMock()
        interaction.response.is_done.return_value = True
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()

        await bot.on_app_command_error(
            interaction, app_commands.CommandInvokeError(MagicMock(), RuntimeError("x"))
        )

        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.call_args.kwargs.get("ephemeral") is True
        interaction.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_failure_is_suppressed(self) -> None:
        # If even the ephemeral reply fails (e.g. interaction expired), the
        # handler must not raise (it is the last-resort net).
        bot = ClaudeDiscordBot(channel_id=123)
        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock(side_effect=_forbidden())
        interaction.followup.send = AsyncMock(side_effect=_forbidden())
        await bot.on_app_command_error(
            interaction, app_commands.CommandInvokeError(MagicMock(), RuntimeError("x"))
        )  # must not raise


class TestOnCommandError:
    """on_command_error (text commands) — new in #444."""

    @pytest.mark.asyncio
    async def test_command_not_found_is_ignored(self) -> None:
        bot = ClaudeDiscordBot(channel_id=123)
        ctx = MagicMock()
        ctx.send = AsyncMock()
        await bot.on_command_error(ctx, commands.CommandNotFound("nope"))
        ctx.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_error_sends_user_message(self) -> None:
        bot = ClaudeDiscordBot(channel_id=123)
        ctx = MagicMock()
        ctx.send = AsyncMock()
        await bot.on_command_error(ctx, commands.CommandInvokeError(_forbidden()))
        ctx.send.assert_awaited_once()
        assert PERMISSION_DENIED_HELP in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_send_failure_is_suppressed(self) -> None:
        bot = ClaudeDiscordBot(channel_id=123)
        ctx = MagicMock()
        ctx.send = AsyncMock(side_effect=_forbidden())
        await bot.on_command_error(
            ctx, commands.CommandInvokeError(RuntimeError("x"))
        )  # must not raise


class TestErrorReportingViewMixin:
    """Button/modal callbacks surface an actionable message instead of the
    generic 'This interaction failed'."""

    @pytest.mark.asyncio
    async def test_on_error_replies_ephemeral_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _V(ErrorReportingViewMixin, discord.ui.View):
            pass

        view = _V(timeout=None)
        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()

        with caplog.at_level(logging.ERROR):
            await view.on_error(interaction, RuntimeError("boom"), MagicMock())

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
        assert caplog.records

    def test_all_real_views_inherit_the_mixin(self) -> None:
        from c_lord.discord_ui.ask_view import AskView
        from c_lord.discord_ui.elicitation_view import (
            ElicitationFormView,
            ElicitationUrlView,
        )
        from c_lord.discord_ui.permission_view import PermissionView
        from c_lord.discord_ui.plan_view import PlanApprovalView
        from c_lord.discord_ui.views import StopView

        for cls in (
            StopView,
            PermissionView,
            PlanApprovalView,
            AskView,
            ElicitationUrlView,
            ElicitationFormView,
        ):
            assert issubclass(cls, ErrorReportingViewMixin), cls.__name__
