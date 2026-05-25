"""Cog: JSONL transcript → Discord thread mirror (Issue #71).

When ``CLORD_BRIDGE_MODE=jsonl``, this Cog tails ``~/.claude/projects/<slug>/``
for every thread that has a stored ``working_dir`` and forwards rendered events
to the corresponding Discord thread.  It coexists with the legacy skill-based
reply path: the skill injection itself is suppressed under jsonl mode so each
event is posted at most once.

Lifecycle:
- ``on_ready``: walk the sessions table and start a mirror task for every row
  whose ``working_dir`` resolves to an existing Claude Code project dir.
- :meth:`start_for` is called from :mod:`c_lord.cogs.claude_chat` when a new
  thread is provisioned (or an existing thread continues a session).
- :meth:`cog_unload` cancels all running mirror tasks cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..transcript.mirror import (
    TranscriptMirror,
    bridge_mode_jsonl,
    reply_to_trigger_enabled,
    silent_posts_enabled,
    verbosity_mode,
)
from ..transcript.resolver import derive_project_dir

if TYPE_CHECKING:
    from ..database.repository import SessionRepository

logger = logging.getLogger(__name__)


class TranscriptMirrorCog(commands.Cog):
    """Owns a ``TranscriptMirror`` per active thread."""

    def __init__(self, bot: commands.Bot, *, session_repo: SessionRepository) -> None:
        self.bot = bot
        self._session_repo = session_repo
        self._mirrors: dict[int, TranscriptMirror] = {}
        self._trigger_messages: dict[int, int] = {}

    def set_trigger_message(self, thread_id: int, message_id: int) -> None:
        """Record the Discord message ID that triggered the current Claude turn.

        Called by ClaudeChatCog before each run so that the reply_sink can
        thread the final answer back to the user's message.
        """
        self._trigger_messages[thread_id] = message_id

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not bridge_mode_jsonl():
            logger.info("TranscriptMirrorCog: CLORD_BRIDGE_MODE != jsonl — staying idle")
            return
        # Sessions are bounded by Discord usage; 10k is well above realistic.
        rows = await self._session_repo.list_all(limit=10_000)
        started = 0
        for row in rows:
            if row.working_dir and self.start_for(row.thread_id, row.working_dir):
                started += 1
        logger.info(
            "TranscriptMirrorCog: started %d mirror(s) from %d session row(s)",
            started,
            len(rows),
        )

    def start_for(self, thread_id: int, working_dir: str) -> bool:
        """Spawn a mirror for ``thread_id`` if one is not already running.

        Returns True if a new mirror was started, False if one already exists
        or bridge mode is not jsonl.
        """
        if not bridge_mode_jsonl():
            return False
        if thread_id in self._mirrors:
            return False

        project_dir = derive_project_dir(working_dir)
        sink = self._make_sink(thread_id)
        reply_sink = self._make_reply_sink(thread_id)
        file_sink = self._make_file_sink(thread_id)
        mirror = TranscriptMirror(
            thread_id=thread_id,
            project_dir=project_dir,
            sink=sink,
            reply_sink=reply_sink,
            file_sink=file_sink,
            verbosity=verbosity_mode(),
        )
        mirror.start()
        self._mirrors[thread_id] = mirror
        logger.info(
            "TranscriptMirrorCog: started mirror thread=%d project_dir=%s",
            thread_id,
            project_dir,
        )
        return True

    async def stop_for(self, thread_id: int) -> None:
        mirror = self._mirrors.pop(thread_id, None)
        if mirror is not None:
            await mirror.stop()

    async def cog_unload(self) -> None:
        await asyncio.gather(*(m.stop() for m in self._mirrors.values()), return_exceptions=True)
        self._mirrors.clear()

    def _make_sink(self, thread_id: int):
        """Return an awaitable callable that posts intermediate messages silently."""
        bot = self.bot

        async def sink(text: str) -> None:
            channel = await self._resolve_channel(bot, thread_id)
            if channel is None:
                return
            body = text if len(text) <= 1990 else text[:1985] + "…"
            send = getattr(channel, "send", None)
            if send is None:
                logger.warning(
                    "TranscriptMirror sink: channel %d has no .send (got %s)",
                    thread_id,
                    type(channel).__name__,
                )
                return
            send_kwargs: dict = {"content": body}
            if silent_posts_enabled():
                send_kwargs["silent"] = True
            try:
                await send(**send_kwargs)
            except discord.HTTPException as exc:
                logger.warning(
                    "TranscriptMirror sink failed: thread=%d body_len=%d status=%s — %s",
                    thread_id,
                    len(body),
                    getattr(exc, "status", "?"),
                    exc,
                    exc_info=True,
                )

        return sink

    def _make_reply_sink(self, thread_id: int):
        """Return an awaitable callable for final assistant text (no progress.txt).

        Sends without silent flag (notifies user) and includes a reference to
        the trigger message so the reply threads visually in Discord.
        """
        bot = self.bot

        async def reply_sink(text: str) -> None:
            channel = await self._resolve_channel(bot, thread_id)
            if channel is None:
                return
            body = text if len(text) <= 1990 else text[:1985] + "…"
            send = getattr(channel, "send", None)
            if send is None:
                return
            from io import BytesIO

            from ..discord_ui.table_renderer import get_table_images

            table_files = [
                discord.File(BytesIO(img), filename=fname) for fname, img in get_table_images(text)
            ]
            send_kwargs: dict = {"content": body}
            if table_files:
                send_kwargs["files"] = table_files
            trigger_id = self._trigger_messages.get(thread_id)
            if trigger_id is not None and reply_to_trigger_enabled():
                send_kwargs["reference"] = discord.MessageReference(
                    message_id=trigger_id,
                    channel_id=thread_id,
                    fail_if_not_exists=False,
                )
                send_kwargs["mention_author"] = False
            try:
                await send(**send_kwargs)
            except discord.HTTPException as exc:
                logger.warning(
                    "TranscriptMirror reply_sink failed: thread=%d body_len=%d status=%s — %s",
                    thread_id,
                    len(body),
                    getattr(exc, "status", "?"),
                    exc,
                    exc_info=True,
                )

        return reply_sink

    def _make_file_sink(self, thread_id: int):
        """Return an awaitable callable for final answers with progress.txt attachment.

        Sends without silent flag (notifies user) and includes a reference to
        the trigger message. Falls back to plain text if the attachment send fails.
        """
        bot = self.bot

        async def file_sink(text: str, file_path: str) -> None:
            channel = await self._resolve_channel(bot, thread_id)
            if channel is None:
                return
            body = text if len(text) <= 1990 else text[:1985] + "…"
            send = getattr(channel, "send", None)
            if send is None:
                return
            from io import BytesIO

            from ..discord_ui.table_renderer import get_table_images

            table_files = [
                discord.File(BytesIO(img), filename=fname) for fname, img in get_table_images(text)
            ]
            files = [discord.File(file_path, filename="progress.txt")] + table_files
            send_kwargs: dict = {"content": body, "files": files}
            trigger_id = self._trigger_messages.get(thread_id)
            if trigger_id is not None and reply_to_trigger_enabled():
                send_kwargs["reference"] = discord.MessageReference(
                    message_id=trigger_id,
                    channel_id=thread_id,
                    fail_if_not_exists=False,
                )
                send_kwargs["mention_author"] = False
            try:
                await send(**send_kwargs)
                return
            except discord.HTTPException as exc:
                logger.warning(
                    "TranscriptMirror file_sink failed (with attachment): "
                    "thread=%d body_len=%d status=%s — retrying without attachment — %s",
                    thread_id,
                    len(body),
                    getattr(exc, "status", "?"),
                    exc,
                    exc_info=True,
                )
            # Fallback: plain text without attachment
            try:
                await send(body)
            except discord.HTTPException as exc:
                logger.warning(
                    "TranscriptMirror file_sink fallback also failed: "
                    "thread=%d body_len=%d status=%s — %s",
                    thread_id,
                    len(body),
                    getattr(exc, "status", "?"),
                    exc,
                    exc_info=True,
                )

        return file_sink

    @staticmethod
    async def _resolve_channel(bot, thread_id: int):
        """Fetch the channel/thread from cache or Discord API."""
        channel = bot.get_channel(thread_id)
        if channel is None:
            with contextlib.suppress(discord.HTTPException, discord.NotFound):
                channel = await bot.fetch_channel(thread_id)
        if channel is None:
            logger.warning(
                "TranscriptMirror sink: channel %d not found, dropping post",
                thread_id,
            )
        return channel
