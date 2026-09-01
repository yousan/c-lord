"""Startup sweep for ``⏹ Stop`` buttons a previous process left behind (#634).

``StopView.disable`` deletes the stop-button message when a turn ends. At
**shutdown** it cannot: aiohttp's session is already closed, so the delete
raises and the message survives —

    views.py:109  StopView.disable: could not delete message — Session is closed

46 of those in production, every one at shutdown. Nothing cleaned them up
afterwards, so each restart added another clickable button wired to a runner
that no longer exists; pressing one answers ``This interaction failed``. A UI
element that looks live and is not is worse than no element at all, which is
why this runs on startup rather than waiting for the thread's next turn.

Matching on the message text rather than on a recorded message id is deliberate:
the residue that already exists was written by versions that recorded nothing,
and those are exactly the messages that need clearing.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import TYPE_CHECKING

import discord

from .discord_ui.views import STOP_MESSAGE_PREFIX

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .database.repository import SessionRepository

logger = logging.getLogger(__name__)

# How far back in each thread to look. The stop message is re-posted at the
# bottom on every bump, so a leftover is always near the tail; a deep scan would
# buy nothing and cost pagination.
_SCAN_MESSAGES = 15

# Upper bound on threads visited in one sweep, newest-used first. Production
# carries hundreds of live sessions and this runs at startup, so the sweep is
# bounded rather than exhaustive — an old thread nobody opens costs nothing.
# Override with CLORD_STOP_SWEEP_MAX; 0 disables the sweep entirely.
_MAX_THREADS_ENV = "CLORD_STOP_SWEEP_MAX"
_DEFAULT_MAX_THREADS = 200


def _max_threads() -> int:
    raw = os.getenv(_MAX_THREADS_ENV)
    if raw is None:
        return _DEFAULT_MAX_THREADS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s=%r is not a number — using the default", _MAX_THREADS_ENV, raw)
        return _DEFAULT_MAX_THREADS


async def sweep_dead_stop_buttons(
    bot: Bot,
    repo: SessionRepository,
    *,
    scan_messages: int = _SCAN_MESSAGES,
    max_threads: int | None = None,
) -> int:
    """Delete this bot's leftover stop-button messages. Returns how many went.

    Safe to run only at startup: it removes **every** stop message it finds, and
    that is correct precisely because no turn is in flight yet — a stop button
    that survived the restart cannot interrupt anything, its runner died with
    the previous process.

    Never raises. One unreadable thread (deleted, permissions changed, archived
    out of reach) must not strand the residue in every other thread.
    """
    limit = _max_threads() if max_threads is None else max_threads
    if limit <= 0:
        return 0

    me = getattr(bot, "user", None)
    if me is None:
        logger.debug("stop-button sweep: not logged in yet, skipping")
        return 0

    try:
        rows = await repo.list_alive()
    except Exception:
        logger.warning("stop-button sweep: could not list sessions", exc_info=True)
        return 0

    removed = 0
    for record in rows[:limit]:
        thread = bot.get_channel(record.thread_id)
        if thread is None:
            with contextlib.suppress(Exception):
                thread = await bot.fetch_channel(record.thread_id)
        if not isinstance(thread, (discord.Thread, discord.abc.Messageable)):
            continue
        try:
            async for message in thread.history(limit=scan_messages):
                if not _is_dead_stop_message(message, me.id):
                    continue
                with contextlib.suppress(Exception):
                    await message.delete()
                    removed += 1
        except Exception:
            logger.debug(
                "stop-button sweep: thread %d unreadable, skipping",
                record.thread_id,
                exc_info=True,
            )
            continue

    if removed:
        logger.info("stop-button sweep: removed %d dead ⏹ Stop button(s) (#634)", removed)
    return removed


def _is_dead_stop_message(message: object, my_id: int) -> bool:
    """True for one of *our* stop-button messages left over from a past process.

    Three conditions, all necessary: written by this bot (another bot's message
    is not ours to delete), the stop-message text, and at least one component —
    a stop message whose buttons were already stripped has nothing clickable on
    it and is just a line of history.
    """
    author = getattr(message, "author", None)
    if author is None or getattr(author, "id", None) != my_id:
        return False
    content = getattr(message, "content", "") or ""
    if not content.startswith(STOP_MESSAGE_PREFIX):
        return False
    return bool(getattr(message, "components", None))
