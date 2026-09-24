"""In-process routing bus for AskUserQuestion interactions.

AskView button/select callbacks call ``ask_bus.post_answer()`` to deliver the
user's choice to the coroutine waiting inside ``_collect_ask_answers``.

Using an asyncio.Queue (rather than a Future) means:
- Multiple answers can be posted safely without raising InvalidStateError.
- The waiting side can use ``asyncio.wait_for`` with any timeout it likes.
- The view itself needs no reference to the internal Future/Event; routing is
  fully decoupled.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Why a thread's menu stopped accepting answers (#536).  A click that arrives
# after the fact needs the real reason: "the bot restarted" was told to every
# late clicker, including the one who had just answered in the terminal.
CLOSE_ANSWERED = "answered"  # a Discord click was delivered
CLOSE_TERMINAL = "terminal"  # answered/cancelled directly in the tmux pane
CLOSE_TIMEOUT = "timeout"  # nobody answered within ASK_ANSWER_TIMEOUT
CLOSE_INTERRUPTED = "interrupted"  # a new instruction pre-empted the turn (#315)

# What became of an answer the bus routed to the TUI (#804).  The bus itself
# only ever queues an answer, so "post_answer returned True" means "someone is
# listening", never "Claude got it" — announcing the second from the first is
# how a typed answer came to be reported as 送りました while Claude recorded it
# as a refusal.  The bridge that actually types the keystrokes reports one of
# these, and whoever needs to speak to the user waits for it.
DELIVERY_DELIVERED = "delivered"  # keystrokes landed AND Claude recorded the answer
DELIVERY_UNDELIVERED = "undelivered"  # the keystrokes reached no window at all (#600)
DELIVERY_NOT_ANSWERED = "not_answered"  # keys landed, Claude recorded "no answer" (#650)
DELIVERY_UNCONFIRMED = "unconfirmed"  # keys landed, the outcome could not be read (#651)

# Closure notes are only useful while a stale copy of the menu can still be
# clicked; the ask-menu answer timeout (24h) is that horizon.
_CLOSE_NOTE_TTL = 86_400.0
_MAX_CLOSE_NOTES = 512


class AskAnswerBus:
    """Routes button/select interactions to the coroutine awaiting an answer.

    One instance is shared across all active sessions (module-level singleton).
    Each waiting session registers a Queue keyed by thread_id; AskView callbacks
    post the chosen labels into that Queue.
    """

    def __init__(self) -> None:
        self._waiters: dict[int, asyncio.Queue[list[str]]] = {}
        # thread_id -> (noted_at_monotonic, reason) — see note_closed (#536)
        self._closed: dict[int, tuple[float, str]] = {}
        # thread_id -> may a typed sentence be delivered as this menu's answer?
        # (#536 AC7) False for plan-approval menus, which have no free-text row.
        self._free_text: dict[int, bool] = {}
        # thread_id -> where to report what became of this thread's answer (#804)
        self._delivery: dict[int, asyncio.Queue[str]] = {}

    def register(
        self, thread_id: int, *, allow_free_text: bool = False
    ) -> asyncio.Queue[list[str]] | None:
        """Claim the menu for *thread_id* and return its Queue, or None (#535).

        *allow_free_text* says whether the claimed menu can accept a typed
        sentence as its answer (#536 AC7).  Only the bridge knows: an
        AskUserQuestion has a ``Type something.`` row, a plan-approval menu does
        not, and typing into the latter mis-sends keystrokes.  It defaults to
        False so a caller that has not thought about it cannot opt a menu in by
        accident.

        Registration **is** the ownership acquisition: the first caller gets a
        Queue, every later caller gets ``None`` ("already owned") until the
        owner calls :meth:`unregister`.  The caller should await
        ``queue.get()`` (ideally with a timeout) and call :meth:`unregister`
        when done regardless of success/timeout.

        Why refuse instead of overwrite: several independent paths can spot the
        same TUI menu (the run_claude poll loop, the transcript mirror, the
        #359 watchdog).  Overwriting handed the second one the queue and left
        the first awaiting a queue nobody would ever post to — a 24h hang
        holding the thread, with two live copies of the buttons in Discord
        (#535).  Checking ``is_active`` first cannot fix that: the check and
        the register are two steps and both bridges can pass the check.  This
        method is the single atomic step, so exactly one bridge wins.
        """
        if thread_id in self._waiters:
            logger.debug(
                "AskAnswerBus: thread %d already has an owner — refusing register", thread_id
            )
            return None
        q: asyncio.Queue[list[str]] = asyncio.Queue()
        self._waiters[thread_id] = q
        self._free_text[thread_id] = allow_free_text
        logger.debug("AskAnswerBus: registered waiter for thread %d", thread_id)
        return q

    def is_active(self, thread_id: int) -> bool:
        """Return True if a bridge is currently waiting for an answer on *thread_id*.

        Used to dedup between the run_claude poll-loop bridge and the always-on
        transcript-mirror bridge (#232): whichever registers first owns the
        menu; the other must not show a second set of buttons.
        """
        return thread_id in self._waiters

    def post_answer(self, thread_id: int, answers: list[str]) -> bool:
        """Deliver *answers* to the coroutine waiting for *thread_id*.

        Returns True if a waiter was found (live session), False if the session
        is gone (e.g. bot was restarted).
        """
        q = self._waiters.get(thread_id)
        if q is not None:
            q.put_nowait(answers)
            logger.debug("AskAnswerBus: delivered %r to thread %d", answers, thread_id)
            return True
        logger.debug("AskAnswerBus: no waiter for thread %d (bot restarted?)", thread_id)
        return False

    def accepts_free_text(self, thread_id: int) -> bool:
        """True if a typed sentence may be delivered as this menu's answer (#536).

        False when no menu is open, and False for a menu whose TUI has no
        free-text row (plan approval) — there the sentence must stay a new
        instruction rather than becoming stray keystrokes.
        """
        return bool(self._free_text.get(thread_id, False)) and thread_id in self._waiters

    def watch_delivery(self, thread_id: int) -> asyncio.Queue[str]:
        """Arm a channel for the verdict on *thread_id*'s next answer (#804).

        Armed by whoever is about to :meth:`post_answer` and needs to tell the
        user what happened — today that is the typed-sentence path in
        ``ClaudeChatCog``.  A click does not arm it: the button already has its
        own feedback (the menu message is rewritten with the verified outcome by
        the bridge), and arming from there would change a path #651 settled.

        Always call :meth:`unwatch_delivery` when done, or a verdict meant for
        the next answer lands in a queue nobody reads.
        """
        q: asyncio.Queue[str] = asyncio.Queue()
        self._delivery[thread_id] = q
        return q

    def note_delivery(self, thread_id: int, verdict: str) -> None:
        """Report what became of *thread_id*'s answer — one of ``DELIVERY_*`` (#804).

        A no-op when nobody armed a channel, which is the normal case (clicks).
        Reporting is therefore always safe to do, and the bridge does it on
        every exit that consumed an answer.
        """
        q = self._delivery.get(thread_id)
        if q is None:
            return
        q.put_nowait(verdict)
        logger.debug("AskAnswerBus: delivery verdict %s for thread %d", verdict, thread_id)

    def unwatch_delivery(self, thread_id: int) -> None:
        """Stop listening for *thread_id*'s delivery verdict (#804)."""
        self._delivery.pop(thread_id, None)

    def unregister(self, thread_id: int) -> None:
        """Remove the waiter for *thread_id* (called after answer or timeout)."""
        self._waiters.pop(thread_id, None)
        self._free_text.pop(thread_id, None)
        logger.debug("AskAnswerBus: unregistered waiter for thread %d", thread_id)

    def note_closed(self, thread_id: int, reason: str) -> None:
        """Record WHY *thread_id*'s menu stopped accepting answers (#536).

        A button click that arrives after the menu closed finds no waiter, and
        without this note the view could only guess — it used to guess "the bot
        was restarted" every time, which is wrong for every menu that was
        answered in the terminal, timed out, or was pre-empted. Use one of the
        ``CLOSE_*`` constants.
        """
        now = time.monotonic()
        self._closed[thread_id] = (now, reason)
        if len(self._closed) > _MAX_CLOSE_NOTES:
            for tid, (noted_at, _) in list(self._closed.items()):
                if now - noted_at >= _CLOSE_NOTE_TTL:
                    del self._closed[tid]
        while len(self._closed) > _MAX_CLOSE_NOTES:
            self._closed.pop(next(iter(self._closed)))  # oldest insertion first

    def closed_reason(self, thread_id: int) -> str | None:
        """Return the recorded ``CLOSE_*`` reason for *thread_id*, or None.

        None means "this process has no idea" — which, for a menu posted before
        the process started, is itself the evidence of a restart.
        """
        note = self._closed.get(thread_id)
        if note is None:
            return None
        noted_at, reason = note
        if time.monotonic() - noted_at >= _CLOSE_NOTE_TTL:
            del self._closed[thread_id]
            return None
        return reason


# Module-level singleton — import this everywhere.
ask_bus = AskAnswerBus()
