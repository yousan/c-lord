"""Startup sweep for buttons a previous process left behind (#634, #752).

A button's handler lives in the process that drew it. When that process goes,
the button stays on screen — and pressing it answers ``This interaction
failed``. A UI element that looks live and is not is worse than no element at
all, which is why this runs on startup rather than waiting for the thread's
next turn. Two kinds of residue are cleared:

- **⏹ Stop** (#634). ``StopView.disable`` deletes the stop-button message when a
  turn ends. At **shutdown** it cannot: aiohttp's session is already closed, so
  the delete raises and the message survives —

      views.py:109  StopView.disable: could not delete message — Session is closed

  46 of those in production, every one at shutdown. They are deleted: a stop
  message is a control, not a record.

- **❓ question menus** (#752). Restart recovery (#671) re-arms the menus it has
  a ``pending_asks`` row for; every other menu from an earlier process has no
  handler anywhere and never will. Production had 82 of them standing live
  (2026-09-23, oldest from May) while ``pending_asks`` held 0 rows — a new
  question overwrites the previous one's row (the table is keyed by thread),
  so nothing remembered them. They are **retired, not deleted**: the buttons
  go, the question stays readable.

Matching on the messages themselves rather than on recorded message ids is
deliberate: the residue that already exists was written by versions that
recorded nothing, and those are exactly the messages that need clearing.

**How far it reads (#752).** This used to re-read a fixed window on every start
— the newest 100 messages of the 200 most recently used threads. Production had
339 threads, and a Stop from 2026-08-07 with 164 messages on top of it: both
out of reach forever. Now each thread has a cursor (``ui_sweep_cursors``): the
first visit reads deep, and every later start resumes after the last message
the previous sweep examined — so nothing sinks out of reach, and a thread with
nothing new costs no request at all.

**What counts as residue.** Only messages created before *this* process started.
Everything newer was drawn by live code — a turn that began while the sweep was
still walking threads has a Stop button that works — so the process start, not
"now", is the boundary.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from .discord_ui.ask_view import _PROCESS_STARTED_AT
from .discord_ui.views import STOP_MESSAGE_PREFIX
from .utils.logger import log_ctx

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .database.repository import SessionRepository
    from .database.sweep_cursor_repo import SweepCursorRepository

logger = logging.getLogger(__name__)

# How deep the FIRST visit to a thread reads. Later visits resume from the
# thread's cursor and read everything since, so this only bounds the one-time
# catch-up over residue written before cursors existed (#752). Measured on
# production 2026-09-23 (363 threads, full history): the deepest live-looking
# button sat under 1,667 messages, and reading every thread to the bottom took
# 517 requests in all — a one-time cost.
_FIRST_VISIT_MESSAGES = 5000

# Upper bound on threads visited in one sweep, newest-used first. Unbounded by
# default since #752: with cursors a thread with nothing new costs no history
# request, and the 200 that used to be the default left 139 production threads
# unvisited forever. Set CLORD_STOP_SWEEP_MAX to cap it; 0 disables the sweep.
_MAX_THREADS_ENV = "CLORD_STOP_SWEEP_MAX"

# Written over a retired menu's buttons. The embed — the question, and any
# answer it recorded — is left as it was (#536).
RETIRED_MENU_NOTE = (
    "-# 🔁 この質問はもう受け付けていません（このボタンは無効です）。"
    "続きが必要なら、あらためてメッセージを送ってください。"
)

#: ``keep_menu(thread_id, message_id)`` — True for a menu that is still served.
KeepMenu = Callable[[int, int], bool]

_ASK_CUSTOM_ID_PREFIX = "ask_"


def _max_threads() -> int | None:
    raw = os.getenv(_MAX_THREADS_ENV)
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s=%r is not a number — visiting every thread", _MAX_THREADS_ENV, raw)
        return None


@dataclass
class _Tally:
    stops: int = 0
    menus: int = 0
    failed: int = 0
    threads_failed: int = 0


async def sweep_dead_buttons(
    bot: Bot,
    repo: SessionRepository,
    *,
    keep_menu: KeepMenu | None = None,
    cursors: SweepCursorRepository | None = None,
    first_visit_messages: int = _FIRST_VISIT_MESSAGES,
    max_threads: int | None = None,
) -> int:
    """Clear this bot's dead buttons from every session thread. Returns how many.

    *keep_menu* names the menus restart recovery has re-armed — those work and
    must be left alone. Without it every earlier process's menu is retired.
    *cursors* makes the sweep resume where the last one stopped; without it
    every visit is a first visit.

    Never raises. One unreadable thread (deleted, permissions changed, archived
    out of reach) must not strand the residue in every other thread.
    """
    limit = _max_threads() if max_threads is None else max_threads
    if limit == 0:
        return 0

    me = getattr(bot, "user", None)
    if me is None:
        logger.debug("dead-button sweep: not logged in yet, skipping")
        return 0

    try:
        rows = await repo.list_alive()
    except Exception:
        logger.warning("dead-button sweep: could not list sessions", exc_info=True)
        return 0
    if limit is not None:
        rows = rows[:limit]

    known: dict[int, int] = {}
    if cursors is not None:
        try:
            known = await cursors.get_all()
        except Exception:
            logger.warning("dead-button sweep: could not read cursors", exc_info=True)

    boundary = discord.Object(id=discord.utils.time_snowflake(_PROCESS_STARTED_AT))
    tally = _Tally()
    for record in rows:
        await _sweep_thread(
            bot,
            record.thread_id,
            me.id,
            boundary,
            known.get(record.thread_id),
            keep_menu,
            first_visit_messages,
            cursors,
            tally,
        )

    if tally.stops or tally.menus or tally.failed or tally.threads_failed:
        logger.info(
            "dead-button sweep: removed %d dead ⏹ Stop button(s), retired %d dead ❓ "
            "menu(s); %d could not be cleared, %d thread(s) unreadable (#634/#752)",
            tally.stops,
            tally.menus,
            tally.failed,
            tally.threads_failed,
        )
    return tally.stops + tally.menus


async def _sweep_thread(
    bot: Bot,
    thread_id: int,
    my_id: int,
    boundary: discord.Object,
    cursor: int | None,
    keep_menu: KeepMenu | None,
    first_visit_messages: int,
    cursors: SweepCursorRepository | None,
    tally: _Tally,
) -> None:
    ctx = log_ctx(thread_id=thread_id)
    thread: Any = bot.get_channel(thread_id)
    if thread is None:
        with contextlib.suppress(Exception):
            thread = await bot.fetch_channel(thread_id)
    if thread is None or not callable(getattr(thread, "history", None)):
        return

    last = getattr(thread, "last_message_id", None)
    if cursor is not None and isinstance(last, int) and last <= cursor:
        return  # nothing has been posted since the last sweep read this thread

    if cursor is not None:
        messages = thread.history(limit=None, after=discord.Object(id=cursor), before=boundary)
    else:
        messages = thread.history(limit=first_visit_messages, before=boundary)

    found: list[tuple[Any, str]] = []
    try:
        async for message in messages:
            kind = _residue_kind(message, my_id, thread_id, keep_menu)
            if kind is not None:
                found.append((message, kind))
    except Exception:
        # #678: not DEBUG — a thread that is never readable is a thread whose
        # residue stays live, and that has to be findable with grep thread=<id>.
        tally.threads_failed += 1
        logger.info(
            "%s dead-button sweep: could not read this thread — its dead buttons stay "
            "until the next start (#752)",
            ctx,
            exc_info=True,
        )
        return

    failed_ids = await _clear_all(thread, found, tally, ctx)
    failed = len(failed_ids)
    oldest_failed = min(failed_ids) if failed_ids else None

    # Everything before this process started has now been examined — except what
    # could not be cleared: the cursor stops short of it so the next start tries
    # again, instead of stepping over it for good.
    new_cursor = int(boundary.id) - 1
    if oldest_failed is not None:
        tally.failed += failed
        new_cursor = oldest_failed - 1
        logger.info(
            "%s dead-button sweep: could not clear %d dead button message(s) — "
            "they stay live-looking until the next start retries (#752)",
            ctx,
            failed,
        )
    if cursors is not None and new_cursor != cursor:
        with contextlib.suppress(Exception):
            await cursors.set(thread_id, new_cursor)


async def _clear_all(
    thread: Any, found: list[tuple[Any, str]], tally: _Tally, ctx: str
) -> list[int]:
    """Clear *found* in *thread*; return the ids that could not be cleared.

    An archived thread is reopened for the duration and archived again after.
    That is where nearly all of the residue lives — production 2026-09-23: 81 of
    82 dead menus and 52 of 65 dead Stops, in ``[停止]`` threads #685 archives —
    and Discord refuses every edit and delete there ("Thread is archived"). It
    is reopened only when there is something to clear, so a sweep over a quiet
    archive changes nothing.
    """
    if not found:
        return []
    reopened = False
    if getattr(thread, "archived", None) is True:
        try:
            await thread.edit(archived=False)
        except Exception as exc:
            # A locked thread needs Manage Threads to reopen (#678: said, not
            # swallowed — the next start retries, see the cursor below).
            logger.info(
                "%s dead-button sweep: could not reopen this archived thread to clear "
                "%d dead button message(s): %s (#752)",
                ctx,
                len(found),
                exc,
            )
            return [int(message.id) for message, _kind in found]
        reopened = True
    failed: list[int] = []
    try:
        for message, kind in found:
            if not await _clear(message, kind):
                failed.append(int(message.id))
            elif kind == "stop":
                tally.stops += 1
            else:
                tally.menus += 1
    finally:
        if reopened:
            try:
                await thread.edit(archived=True)
            except Exception:
                logger.warning(
                    "%s dead-button sweep: could not archive this thread again after "
                    "clearing its dead buttons (#752)",
                    ctx,
                    exc_info=True,
                )
    return failed


def _residue_kind(
    message: object, my_id: int, thread_id: int, keep_menu: KeepMenu | None
) -> str | None:
    """``"stop"`` / ``"menu"`` for one of *our* dead controls, else None."""
    if _is_dead_stop_message(message, my_id):
        return "stop"
    if _is_menu_message(message, my_id):
        message_id = int(getattr(message, "id", 0))
        if keep_menu is not None and keep_menu(thread_id, message_id):
            return None  # re-armed by restart recovery (#671) — it works
        return "menu"
    return None


async def _clear(message: Any, kind: str) -> bool:
    """Delete a Stop / strip a menu's buttons. True when Discord accepted it."""
    try:
        if kind == "stop":
            await message.delete()
        else:
            # ``view=None`` only: ``embed=None`` would erase the question and any
            # answer it recorded along with the buttons (#536).
            await message.edit(content=RETIRED_MENU_NOTE, view=None)
    except Exception:
        return False
    return True


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


def _is_menu_message(message: object, my_id: int) -> bool:
    """True for one of *our* ❓ menus that still shows a pressable control.

    Recognised by the ``ask_…`` custom ids ``AskView`` gives every control —
    stable on purpose (#671), and nothing else in c-lord uses them. A disabled
    control, or a link button (which needs no handler), is not "pressable".
    """
    author = getattr(message, "author", None)
    if author is None or getattr(author, "id", None) != my_id:
        return False
    for row in getattr(message, "components", None) or []:
        children = getattr(row, "children", None)
        for item in children if isinstance(children, list) else [row]:
            if getattr(item, "disabled", False) is True or getattr(item, "url", None):
                continue
            custom_id = getattr(item, "custom_id", None)
            if isinstance(custom_id, str) and custom_id.startswith(_ASK_CUSTOM_ID_PREFIX):
                return True
    return False
