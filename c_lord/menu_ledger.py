"""Which question menus are already on screen in a thread (#600, #633, #717).

One fact, asked by two different places:

* the #359 **menu watchdog** asks it before bridging a TUI menu no turn is
  watching — "have I already posted this one?";
* every **turn-side bridge** (``bridge_pane_ask``) answers it as it posts —
  "this menu is on screen now".

Both halves have to use the *same* ledger, and the ledger has to outlive the
process. It did not, and that is #717: only the watchdog ever wrote a row, so a
menu posted by the turn looked unbridged to the next process and was posted a
second time — the long 経緯 message and the question card again, two live sets
of buttons, at the exact moment the user is being asked to decide something.

The module holds the identity rule (:func:`menu_fingerprint`), the ledger
itself, and the process-wide handle both halves reach it through. It lives
outside ``thread_state_sync`` (its first home) because ``ask_handler`` needs it
too, and that module is about Discord thread *names*.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .claude.types import AskQuestion
    from .database.menu_bridge_repo import MenuBridgeRepository

logger = logging.getLogger(__name__)

# #600/#633: how many times the SAME question may be posted in a thread. When an
# answer cannot reach the TUI the menu never closes, so every watchdog sweep sees
# it as "unbridged" and would post it again — production stacked six copies of
# one ❓ over three days and logged 188 re-bridges in a single thread. #600 set
# this to 3 and reset it on every successful bridge, which is why the stream
# never ended; #633 makes it ONE post per menu, released only when the pane is
# observed with no menu on it (see :meth:`MenuRebridgeLedger.clear`).
_MAX_REBRIDGES_PER_MENU = 1


def menu_fingerprint(question: AskQuestion) -> str:
    """Stable identity of a TUI menu — the rule the watchdog dedups on (#633).

    Two panes show *the same menu* when the user is being asked the same thing:
    same ``header``, same question line, same option labels in the same order.
    Descriptions and the pre-menu 経緯 are deliberately excluded — they are
    re-wrapped by every redraw and by every window resize, so including them
    would make one stranded menu look like a fresh question on each tick.

    Hashed rather than stored verbatim: the ledger is only ever compared for
    equality, and a hash keeps the user's question text out of a second table.
    """
    payload = "␟".join(
        [
            question.header or "",
            question.question or "",
            *(o.label for o in question.options),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class MenuRebridgeLedger:
    """Tracks which menus have already been posted to a thread (#600, #633, #717).

    Keyed by (thread, menu fingerprint) rather than by thread: a stuck question
    must stop repeating, but the *next* question in that thread is a different
    decision and starts with a full budget. Keying on the thread alone would
    silence menus the user has never seen.

    #633: the counts are held in SQLite when a ``MenuBridgeRepository`` is
    given. In memory alone they were wiped by every bot restart — and the
    production bot restarts several times a day, so a menu nobody could answer
    was re-posted with a fresh ``attempt=1/3`` on each new process (188
    re-bridges in one thread, one embed six times over three days). Consumers
    that pass no repo keep the old process-local behaviour.
    """

    def __init__(self, repo: MenuBridgeRepository | None = None) -> None:
        self._repo = repo
        self._counts: dict[tuple[int, str], int] = {}

    @staticmethod
    def _key(thread_id: int, signature: str) -> tuple[int, str]:
        return (thread_id, signature or "")

    async def record(self, thread_id: int, signature: str) -> int:
        if self._repo is not None:
            with contextlib.suppress(Exception):
                return await self._repo.record(thread_id, signature or "")
        key = self._key(thread_id, signature)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    async def posts(self, thread_id: int, signature: str) -> int:
        """How many times this menu has been posted to this thread."""
        if self._repo is not None:
            with contextlib.suppress(Exception):
                return await self._repo.posts(thread_id, signature or "")
        return self._counts.get(self._key(thread_id, signature), 0)

    async def note_posted(self, thread_id: int, signature: str) -> None:
        """Write down a menu some OTHER route has just put on screen (#717).

        The watchdog records its own posts before it spawns the bridge; this is
        the same fact arriving from the turn-side bridge, which is where three
        of the four menu routes actually post. Idempotent, so a menu the
        watchdog claimed a moment ago is not counted twice — the number in the
        row is "how many copies are in the thread", and a second increment for
        one copy would be a lie the ``attempt=N/M`` log then repeats.
        """
        if await self.posts(thread_id, signature) > 0:
            return
        await self.record(thread_id, signature)

    async def exhausted(self, thread_id: int, signature: str) -> bool:
        return await self.posts(thread_id, signature) >= _MAX_REBRIDGES_PER_MENU

    async def forget(self, thread_id: int, signature: str) -> None:
        """Undo one :meth:`record` — the bridge raised, so nothing was posted."""
        if self._repo is not None:
            with contextlib.suppress(Exception):
                await self._repo.forget(thread_id, signature or "")
        self._counts.pop(self._key(thread_id, signature), None)

    async def clear(self, thread_id: int) -> None:
        """Forget this thread's budget — its pane no longer shows any menu."""
        if self._repo is not None:
            with contextlib.suppress(Exception):
                await self._repo.clear(thread_id)
        for key in [k for k in self._counts if k[0] == thread_id]:
            del self._counts[key]


# The ledger this process posts menus against. ``setup.py`` binds the
# SQLite-backed one at startup (zero-config); until then — and in consumers that
# wire nothing — an in-memory ledger keeps the callers total.
_shared: MenuRebridgeLedger | None = None


def use_shared_ledger(ledger: MenuRebridgeLedger | None) -> None:
    """Make *ledger* the one every menu route writes to. ``None`` resets it.

    Called once from ``setup.py`` with the SQLite-backed ledger, so that the
    watchdog and the turn-side bridge are looking at the same rows — which is
    the whole of #717. Kept explicit (rather than built on first use) because
    the thing that matters is that it is the *same object* as the watchdog's.
    """
    global _shared
    _shared = ledger


def shared_ledger() -> MenuRebridgeLedger:
    """The process's menu ledger, creating an in-memory one if none is bound."""
    global _shared
    if _shared is None:
        _shared = MenuRebridgeLedger()
    return _shared
