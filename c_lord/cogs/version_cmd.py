"""VersionCog — report the running bot's build version.

Exposes ``/version`` (slash) and ``!version`` (text/mention twin, so it is
webhook-invokable for E2E like the other ops commands, #209/#230). The string
follows the article format ``v1.4.0-b<commit>-<YYYYMMDD>`` resolved by
:func:`c_lord.version.resolve_version`.

Read-only — no permission gate needed. Auto-registered by ``setup_bridge`` so
consumers get it by updating the package alone (zero-config principle).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from ..discord_ui.embeds import COLOR_INFO
from ..version import resolve_version

_Responder = Callable[..., Awaitable[None]]


def version_embed(version: str) -> discord.Embed:
    """Build the embed shown by /version and !version."""
    return discord.Embed(
        title="\U0001f4e6 c-lord version",
        description=f"Running build: `{version}`",
        color=COLOR_INFO,
    )


class VersionCog(commands.Cog):
    """Cog exposing the running bot's version via /version (+ text twin)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Slash/text I/O plumbing (mirrors SessionManageCog #209/#230) ──────────

    def _slash_io(self, interaction: discord.Interaction) -> _Responder:
        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            ephemeral: bool = False,
        ) -> None:
            if embed is not None:
                await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)

        return respond

    def _ctx_io(self, ctx: commands.Context) -> _Responder:
        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            ephemeral: bool = False,
        ) -> None:
            # Text channels can't be ephemeral — ``ephemeral`` is ignored.
            if embed is not None:
                await ctx.send(embed=embed)
            else:
                await ctx.send(content or "")

        return respond

    async def _version_impl(self, *, respond: _Responder) -> None:
        """Shared core for /version and !version."""
        await respond(embed=version_embed(resolve_version()))

    @app_commands.command(name="version", description="Show the running c-lord build version")
    async def version(self, interaction: discord.Interaction) -> None:
        await self._version_impl(respond=self._slash_io(interaction))

    @commands.command(name="version")
    async def version_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /version — webhook-invokable for E2E."""
        await self._version_impl(respond=self._ctx_io(ctx))
