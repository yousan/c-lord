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
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..transcript.mirror import TranscriptMirror, bridge_mode_jsonl
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
        mirror = TranscriptMirror(thread_id=thread_id, project_dir=project_dir, sink=sink)
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
        """Return an awaitable callable that posts a string to the given thread."""
        bot = self.bot

        async def sink(text: str) -> None:
            channel = bot.get_channel(thread_id)
            if channel is None:
                # Thread may not be in cache after a restart — try fetching.
                try:
                    channel = await bot.fetch_channel(thread_id)
                except (discord.HTTPException, discord.NotFound) as exc:
                    logger.warning(
                        "TranscriptMirror sink: fetch_channel %d failed: %s",
                        thread_id,
                        exc,
                    )
            if channel is None:
                logger.warning(
                    "TranscriptMirror sink: channel %d not found, dropping post",
                    thread_id,
                )
                return
            # Discord caps a single message at 2000 chars; the formatter does
            # not pre-chunk so we hard-truncate here.  When this PR lands the
            # chunk + .txt-attach policy from Issue #71 §2 will live behind a
            # dedicated helper.
            body = text if len(text) <= 1990 else text[:1985] + "…"
            send = getattr(channel, "send", None)
            if send is None:
                logger.warning(
                    "TranscriptMirror sink: channel %d has no .send (got %s)",
                    thread_id,
                    type(channel).__name__,
                )
                return
            await send(body)

        return sink
