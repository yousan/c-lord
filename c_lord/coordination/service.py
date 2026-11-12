"""One-line session announcements in a shared coordination channel.

Optional: with no channel configured every call returns without touching
Discord. An announcement is a courtesy to the people watching that channel,
so failing to deliver one is logged and never reaches the caller.
"""

from __future__ import annotations

import logging

import discord

from ..discord_failures import DISCORD_CALL_FAILURES

logger = logging.getLogger(__name__)


class CoordinationService:
    """Posts session lifecycle notes to the coordination channel, if there is one."""

    def __init__(self, bot: discord.Client, channel_id: int | None) -> None:
        self.bot = bot
        self.channel_id = channel_id

    @property
    def enabled(self) -> bool:
        """Whether a coordination channel was configured."""
        return self.channel_id is not None

    async def post_session_end(self, thread: discord.Thread) -> None:
        """Announce that the session living in *thread* has ended."""
        await self._announce(f"✅ **{thread.name}** セッション終了")

    async def _announce(self, text: str) -> None:
        channel_id = self.channel_id
        if channel_id is None:
            return
        # Cache only: an announcement is not worth an API round trip.
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            logger.warning("Coordination channel %d not found in bot cache", channel_id)
            return
        try:
            await channel.send(text)  # type: ignore[union-attr]
        except DISCORD_CALL_FAILURES:
            logger.warning("Failed to post to coordination channel %d", channel_id, exc_info=True)
