"""Where "Claude's turn is over" comes from when the pane cannot say (#583).

The tmux pane is a bad witness for turn boundaries, in two opposite ways:

* **It goes silent.** In the default jsonl bridge mode the answer never touches
  the pane — it is read from the transcript and posted by
  :class:`~c_lord.transcript.mirror.TranscriptMirror`.  Once the turn ends the
  pane freezes with nothing scrapable on it, so the runner's completion exit
  (which needs a *non-empty* stable response) and its idle exit (which needs an
  *empty* one) both stall, and the turn hangs until the 300s inactivity
  backstop.  The user gets 🟡 five minutes after the answer.
* **It never goes silent.** With background work in flight the pane keeps
  redrawing, so even that backstop cannot fire.  The turn then stays open until
  the user's *next message* pre-empts it — 14m38s in the 2026-08-31 report —
  and the completion ping lands seconds after they typed, reading as a reply to
  what they just said.

Claude Code's own transcript answers the question exactly, with
``{"type": "system", "subtype": "turn_duration"}``, and the mirror already
tails it.  This module is the hand-off: the mirror :meth:`~TurnEndBus.mark` s
the marker, the runner's poll loop asks :meth:`~TurnEndBus.ended_after`.

**Whose ending is it?** A turn displaced by a new instruction writes its own
``turn_duration`` at interrupt time.  Honouring that marker would finalize the
new turn on its first poll and fire 🟡 before Claude had said anything, which is
#365 all over again.  The transcript answers the question by itself, because it
records both halves in order::

    user (c-lord's prompt)  →  assistant …  →  system/turn_duration

so a turn end is *this* turn's when a prompt was recorded after this run began
and the ending came after that prompt.  Both halves carry the transcript
event's own timestamp.

Deliberately not "and the pane agrees the turn started": on 2026-09-08 a long
answer scrolled its ``●`` markers off the visible pane and the spinner fell
between two polls, so the pane could not answer that question at all — and the
turn hung to the 300s backstop with the marker sitting right there in the
transcript.  The pane is the witness this module exists to replace.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ``datetime.UTC`` is 3.11+ and c-lord supports 3.10.
_UTC = timezone.utc  # noqa: UP017

# One entry per thread that has ever ended a turn in this process.  Production
# mirrors hundreds of threads, so the map is bounded and evicted oldest-first —
# a dropped entry only costs one turn the fast exit (it falls back to the pane
# detection + inactivity backstop), never correctness.
_MAX_THREADS = 2048


def _as_utc(moment: datetime) -> datetime:
    """Return *moment* as an aware UTC datetime.

    Transcript timestamps are ISO-8601 ``…Z``; a naive value can only have come
    from a parser that dropped the zone, and reading it as anything but UTC
    would mis-order the comparison (or raise on it).
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_UTC)
    return moment.astimezone(_UTC)


@dataclass
class _ThreadTurns:
    """What the transcript has said about one thread's turn boundaries."""

    prompt_at: datetime | None = None
    ended_at: datetime | None = None
    # #908: the text of this turn's latest reply when that reply was Claude
    # Code's synthetic API error; ``None`` once anything else follows it.
    api_error: str | None = None


class TurnEndBus:
    """Per-thread record of the transcript's turn boundaries.

    One instance is shared process-wide (module-level singleton).  Everything
    runs on the asyncio loop, so no locking is needed.
    """

    def __init__(self) -> None:
        self._threads: dict[int, _ThreadTurns] = {}
        self._tailing: set[int] = set()
        # #1223: a transcript c-lord named that has not appeared (keyed by the
        # name and when it was written), the one turn that spent it, and which
        # turn is running now (a token, so a displaced turn ending late cannot
        # clear the turn that displaced it).
        self._pending_claim: dict[int, str] = {}
        self._claim_turn: dict[int, tuple[str, float]] = {}
        self._running_turn: dict[int, object] = {}

    def _record(self, thread_id: int) -> _ThreadTurns:
        record = self._threads.get(thread_id)
        if record is None:
            record = self._threads.setdefault(thread_id, _ThreadTurns())
            while len(self._threads) > _MAX_THREADS:
                self._threads.pop(next(iter(self._threads)))  # oldest insertion first
        return record

    def note_prompt(self, thread_id: int, *, at: datetime | None = None) -> None:
        """Record that Claude read an instruction in *thread_id* at *at*.

        This is the "a new turn began" half.  ``at`` is the transcript event's
        own timestamp; ``None`` means the event carried none, and the time it
        was read is then the closest thing to the truth available.
        """
        moment = _as_utc(at) if at is not None else datetime.now(_UTC)
        record = self._record(thread_id)
        record.prompt_at = moment
        record.api_error = None
        logger.debug("turn_end_bus: prompt thread=%d at=%s", thread_id, moment.isoformat())

    def mark(self, thread_id: int, *, at: datetime | None = None) -> None:
        """Record that a turn ended in *thread_id* at *at*."""
        moment = _as_utc(at) if at is not None else datetime.now(_UTC)
        self._record(thread_id).ended_at = moment
        logger.debug("turn_end_bus: marked thread=%d at=%s", thread_id, moment.isoformat())

    def note_reply(self, thread_id: int, *, api_error: str | None) -> None:
        """Record Claude's latest reply in *thread_id* (#908).

        *api_error* is the CLI's text when the reply was its synthetic API-error
        message, ``None`` for anything else.  Only the latest reply counts: an
        error the CLI retried past, with Claude answering after it, is not how
        the turn ended.
        """
        self._record(thread_id).api_error = api_error

    def api_error_since(self, thread_id: int, started_at: datetime) -> str | None:
        """The API error *this* turn ended on, if it ended on one (#908).

        Same ownership rule as :meth:`ended_after`: the turn must have started
        after *started_at* and ended since.
        """
        if not self.ended_after(thread_id, started_at):
            return None
        return self._threads[thread_id].api_error

    def ended_after(self, thread_id: int, started_at: datetime) -> bool:
        """Has a turn started after *started_at* and then ended, in *thread_id*?

        Both halves are required, in that order — a bare ending belongs to
        whatever ran before this call (see the module docstring).

        ``False`` means "no evidence" — never "the turn is still running".  The
        caller keeps its own pane-based detection as the fallback, so a build
        that writes no marker behaves exactly as it did before.
        """
        record = self._threads.get(thread_id)
        if record is None or record.prompt_at is None or record.ended_at is None:
            return False
        return record.prompt_at > _as_utc(started_at) and record.ended_at >= record.prompt_at

    def in_flight_after(self, thread_id: int, started_at: datetime) -> bool:
        """Has Claude read an instruction after *started_at* and not yet ended that turn? (#1132)

        The transcript's own "this turn is still going". While it holds, the
        pane is not allowed to declare the turn over: a long prompt that Claude
        Code takes as a paste shows neither a spinner nor anything new on the
        pane for the 15–40 s before Claude's first output, and the pane exit
        closed such turns 14 s in while the work went on for up to an hour.

        ``False`` without a record — no transcript, no evidence — so a thread
        whose transcript c-lord cannot read keeps the pane detection.
        """
        record = self._threads.get(thread_id)
        if record is None or record.prompt_at is None:
            return False
        if record.prompt_at <= _as_utc(started_at):
            return False
        return record.ended_at is None or record.ended_at < record.prompt_at

    def prompt_since(self, thread_id: int, started_at: datetime) -> bool:
        """Has the transcript recorded an instruction after *started_at*? (#1132)"""
        record = self._threads.get(thread_id)
        return (
            record is not None
            and record.prompt_at is not None
            and record.prompt_at > _as_utc(started_at)
        )

    def note_tailing(self, thread_id: int, live: bool) -> None:
        """A mirror started (*live*) or stopped tailing *thread_id*'s transcript (#1132).

        While one is, an instruction c-lord typed will show up here as
        :meth:`note_prompt` — a pasted one only once Claude Code sends it on,
        which can be many seconds after the keystrokes.
        """
        if live:
            self._tailing.add(thread_id)
        else:
            self._tailing.discard(thread_id)

    def note_pending_claim(self, thread_id: int, key: str | None) -> None:
        """The mirror's named transcript has not appeared yet (*key*), or that ended (#1223).

        *key* names the claim and when it was written, so a new claim — another
        wake, another fresh start — is a new key. A key that first appears while
        a turn is running is that turn's: on a fresh start ``start_claude`` names
        the session after the turn began, and a mirror started with the turn may
        see a wake's name only after it began.
        """
        if key is None:
            # The spent names stay on record: a stat that failed once must not
            # hand an old name a second turn when it comes back (#1190).
            self._pending_claim.pop(thread_id, None)
            return
        self._pending_claim[thread_id] = key
        if thread_id in self._running_turn:
            self._grant(thread_id, key)

    def _grant(self, thread_id: int, key: str) -> None:
        """Give *key* its one turn of grace, unless a turn already spent it."""
        granted = self._claim_turn.get(thread_id)
        if granted is None or granted[0] != key:
            # Re-inserted so the bound below drops the thread granted longest
            # ago; bounded like ``_threads`` — nothing in c-lord calls forget().
            # A dropped entry can only let that thread's old name hold one more
            # turn, never lose a reply.
            self._claim_turn.pop(thread_id, None)
            self._claim_turn[thread_id] = (key, time.monotonic())
            while len(self._claim_turn) > _MAX_THREADS:
                self._claim_turn.pop(next(iter(self._claim_turn)))  # oldest first

    def note_turn_started(self, thread_id: int) -> object:
        """c-lord is about to send *thread_id* an instruction (#1223).

        A named transcript appears only with the first instruction, so the grace
        for "about to appear" counts from the first turn that runs under the
        name — not from when the name was written, which for a wake
        (``start_claude(prompt=None)``) can be long before. Once per name: a
        name whose transcript never came after its first turn does not hold any
        later turn (#1190).

        Returns the token to hand :meth:`note_turn_ended`.
        """
        token = object()
        self._running_turn[thread_id] = token
        key = self._pending_claim.get(thread_id)
        if key is not None:
            self._grant(thread_id, key)
        return token

    def note_turn_ended(self, thread_id: int, token: object) -> None:
        """The turn :meth:`note_turn_started` returned *token* for is over (#1223)."""
        if self._running_turn.get(thread_id) is token:
            del self._running_turn[thread_id]

    def is_tailing(self, thread_id: int) -> bool:
        """Whether a mirror is tailing *thread_id*'s transcript (#1132).

        Also while a transcript c-lord named is due with this turn's instruction
        — for :data:`~c_lord.transcript.resolver.CLAIM_GRACE_SECONDS` after the
        first instruction sent under that name (#1223).
        """
        if thread_id in self._tailing:
            return True
        key = self._pending_claim.get(thread_id)
        granted = self._claim_turn.get(thread_id)
        if key is None or granted is None or granted[0] != key:
            return False
        from .transcript.resolver import CLAIM_GRACE_SECONDS

        return time.monotonic() - granted[1] < CLAIM_GRACE_SECONDS

    def ended_at(self, thread_id: int) -> datetime | None:
        """When the transcript last said a turn in *thread_id* ended, if ever.

        For a caller that already knows a turn was running at some moment and
        only asks "has it ended since?" — the lamp adopted after a restart
        (#718), whose prompt was read by the previous process.
        """
        record = self._threads.get(thread_id)
        return record.ended_at if record is not None else None

    def forget(self, thread_id: int) -> None:
        """Drop *thread_id*'s record (workspace teardown, tests)."""
        self._threads.pop(thread_id, None)
        self._tailing.discard(thread_id)
        self._pending_claim.pop(thread_id, None)
        self._claim_turn.pop(thread_id, None)
        self._running_turn.pop(thread_id, None)

    def size(self) -> int:
        """How many threads are on record — for the bound's own test."""
        return len(self._threads)


# Module-level singleton — import this everywhere.
turn_end_bus = TurnEndBus()
