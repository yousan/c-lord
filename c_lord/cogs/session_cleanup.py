"""Tell each thread that its session record was swept — #554.

The 30-day sweep itself (since #818 it marks rows swept instead of deleting
them) still runs from :mod:`c_lord.main`,
before the bot connects. That timing matters: ``TranscriptMirrorCog``
walks the ``sessions`` table on ``on_ready`` and starts a mirror per row, so
sweeping *after* the connection would race it into starting mirrors for rows
that are about to vanish. The sweep therefore stays where it is, and hands the
deleted rows to this cog, which posts once the bot is actually able to.

Consumers get this for free — :mod:`c_lord.setup` registers it — but note the
cog only *announces*. It never deletes. A consumer embedding c-lord in their own
bot has never had the 30-day sweep and does not get it now; enabling data
deletion as a side effect of a version bump would be the wrong kind of
zero-config.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..session_cleanup import inspect_survivors, notice_for
from ..utils.logger import log_ctx

if TYPE_CHECKING:
    from ..database.repository import SessionRecord

logger = logging.getLogger(__name__)

#: Seconds between notices. The sweep normally takes 1–3 rows (「Cleaned up 3 old
#: sessions」 is a real log line), so this is invisible in the common case. It
#: exists for the backlog run — yousan's instance carries 159 session dirs whose
#: rows are already gone — where posting back to back is what gets a bot rate
#: limited. Nothing is capped or dropped: a big batch simply takes longer, in the
#: background, where nobody is waiting for it.
DEFAULT_POST_DELAY_SECONDS = 1.0


class SessionCleanupCog(commands.Cog):
    """Posts the 🧹 notice into every swept thread that is still open (#857)."""

    def __init__(self, bot: commands.Bot, *, days: int = 30) -> None:
        self.bot = bot
        self._days = days
        self._post_delay = DEFAULT_POST_DELAY_SECONDS
        self._queue: list[SessionRecord] = []
        self._task: asyncio.Task[None] | None = None

    @property
    def pending(self) -> int:
        """How many notices are still queued (used by tests and diagnostics)."""
        return len(self._queue)

    def announce(self, deleted: list[SessionRecord]) -> None:
        """Queue notices for ``deleted`` and return immediately.

        Deliberately not a coroutine: the caller is the startup path, and it has
        no business waiting on Discord. Logs the thread ids here rather than in
        the posting task (#554 AC7) so that the record of *what was deleted*
        exists even if every post later fails.
        """
        if not deleted:
            return
        self._queue.extend(deleted)
        logger.info(
            "Cleaned up %d session(s) unused for %d+ days: threads=%s",
            len(deleted),
            self._days,
            [r.thread_id for r in deleted],
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Start draining once the bot can actually post.

        ``on_ready`` fires again on every reconnect, so this must be idempotent —
        a reconnect mid-drain must not start a second drain over the same queue.
        """
        if self._task is not None and not self._task.done():
            return
        if not self._queue:
            return
        self._task = asyncio.create_task(self._drain())

    async def flush(self) -> None:
        """Drain the queue now and wait for it. For tests and shutdown."""
        await self._drain()

    async def _drain(self) -> None:
        """Post one notice per queued row, spaced by ``_post_delay``.

        Every failure mode here is survivable and none of them may propagate: the
        rows are already deleted, so raising would lose the remaining notices for
        nothing. A thread the user has since deleted (``NotFound``) or one the bot
        can no longer post in (``Forbidden``) is logged and skipped.
        """
        while self._queue:
            record = self._queue.pop(0)
            await self._notify(record)
            if self._queue and self._post_delay:
                await asyncio.sleep(self._post_delay)

    async def _notify(self, record: SessionRecord) -> None:
        ctx = log_ctx(thread_id=record.thread_id)
        try:
            survivors = inspect_survivors(record)
            thread = await self.bot.fetch_channel(record.thread_id)
        except (discord.HTTPException, discord.ClientException, OSError, ValueError):
            logger.warning("%s cleanup notice skipped — thread unreachable (#554)", ctx)
            return
        if not isinstance(thread, discord.abc.Messageable):
            logger.warning("%s cleanup notice skipped — not messageable (#554)", ctx)
            return
        # #857: Discord un-archives a thread the moment anything is posted to it.
        # A swept thread has been quiet for 30 days, so most are already closed —
        # and posting there reopened 27 of them at once (2026-10-02). A closed
        # thread is tidied in silence: the #818 tombstone answers whoever comes
        # back to it. ``is True`` because only an explicit flag counts as closed.
        if getattr(thread, "archived", False) is True:
            logger.info(
                "%s cleanup: thread already archived — swept silently, no notice (#857)", ctx
            )
            return
        try:
            await thread.send(notice_for(record, survivors, days=self._days))
        except discord.HTTPException:
            logger.warning("%s cleanup notice failed to send (#554)", ctx)
            return
        # Leave the thread as closed as it would have been without us: an open
        # one that has been quiet for 30 days is done with (#857).
        try:
            if isinstance(thread, discord.Thread):
                await thread.edit(archived=True)
        except discord.HTTPException:
            logger.warning(
                "%s cleanup notice posted but re-archiving failed — thread left open (#857)",
                ctx,
            )
            return
        logger.info(
            "%s cleanup notice posted, thread archived (session_dir=%s transcript=%s)",
            ctx,
            survivors.session_dir,
            survivors.transcript,
        )
