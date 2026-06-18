"""Shared ``on_error`` for Discord UI Views (#444 safety net).

By default, an unhandled exception in a ``discord.ui.View`` button/modal callback
shows the user a generic "This interaction failed" and prints a traceback to
stderr (invisible on bot hosts).  ``ErrorReportingViewMixin`` overrides
``on_error`` so every View instead logs the exception AND replies to the user
with a sanitized, actionable message.

Usage: list the mixin *before* ``discord.ui.View`` in the bases so its
``on_error`` takes precedence::

    class StopView(ErrorReportingViewMixin, discord.ui.View): ...
"""

from __future__ import annotations

import contextlib
import logging

import discord

from .permission_help import command_error_help

logger = logging.getLogger(__name__)


class ErrorReportingViewMixin:
    """Mixin that turns an unhandled View callback error into a user reply."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.error(
            "Unhandled error in %s view callback (item=%r): %s",
            type(self).__name__,
            getattr(item, "custom_id", item),
            error,
            exc_info=error,
        )
        message = command_error_help(error)
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
