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

    def register(self, thread_id: int) -> asyncio.Queue[list[str]] | None:
        """Claim the menu for *thread_id* and return its Queue, or None (#535).

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

    def unregister(self, thread_id: int) -> None:
        """Remove the waiter for *thread_id* (called after answer or timeout)."""
        self._waiters.pop(thread_id, None)
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
