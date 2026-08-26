"""Registry of the menu messages currently live in each thread (#536).

A thread should only ever have one answerable menu, and #535 made that true for
menus raised in one process.  Copies can still outlive it: a bot restart leaves
the previous process's message on screen with its buttons intact, and any menu
posted before #535 is still sitting in some thread's history.

When one copy is resolved, every other copy in the same thread must stop
inviting clicks — otherwise the user picks the dead one and, from their side,
nothing happens.  This registry is what makes "every other copy" addressable.

In-memory by design: it tracks *live* Discord message objects, which do not
survive a restart anyway.  A copy from a previous process is therefore out of
reach here — it is handled where it is reachable, by the click itself landing on
the honest "this question is already closed" path in :mod:`ask_view`.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

# Enough for the pathological case (a thread that accumulated copies before the
# fix); the registry is cleared whenever a thread's menu resolves.
_MAX_PER_THREAD = 8


class AskMenuRegistry:
    """Per-thread set of posted menu messages that still carry live buttons."""

    def __init__(self) -> None:
        self._menus: dict[int, list[Any]] = {}

    def register(self, thread_id: int, message: Any) -> None:
        """Note that *message* is showing an answerable menu for *thread_id*."""
        bucket = self._menus.setdefault(thread_id, [])
        if any(getattr(m, "id", None) == getattr(message, "id", None) for m in bucket):
            return
        bucket.append(message)
        del bucket[:-_MAX_PER_THREAD]

    def forget(self, thread_id: int, message_id: int | None) -> None:
        """Drop one message (it is no longer answerable)."""
        bucket = self._menus.get(thread_id)
        if not bucket:
            return
        bucket[:] = [m for m in bucket if getattr(m, "id", None) != message_id]
        if not bucket:
            self._menus.pop(thread_id, None)

    def pop_others(self, thread_id: int, keep_message_id: int | None) -> list[Any]:
        """Remove and return every registered message except *keep_message_id*."""
        bucket = self._menus.pop(thread_id, [])
        others = [m for m in bucket if getattr(m, "id", None) != keep_message_id]
        kept = [m for m in bucket if getattr(m, "id", None) == keep_message_id]
        if kept:
            self._menus[thread_id] = kept
        return others

    def clear(self, thread_id: int) -> None:
        """Forget every menu for *thread_id* (test isolation / turn boundary)."""
        self._menus.pop(thread_id, None)


# Process-wide singleton — same pattern as ``ask_bus``.
ask_menus = AskMenuRegistry()


async def disable_stale_copies(thread_id: int, keep_message_id: int | None, note: str) -> None:
    """Blank out every other live copy of *thread_id*'s menu (#536).

    Failures are swallowed per message: a copy we cannot edit (deleted, missing
    permission) must not stop the copies we can, and must never surface as an
    error over an interaction the user answered successfully.
    """
    await _disable(ask_menus.pop_others(thread_id, keep_message_id), note)


async def _disable(messages: Iterable[Any], note: str) -> None:
    for message in messages:
        edit = getattr(message, "edit", None)
        if edit is None:
            continue
        with contextlib.suppress(discord.HTTPException, Exception):
            await edit(content=note, embed=None, view=None)
        logger.debug("ask_menus: disabled stale menu copy %s", getattr(message, "id", "?"))
