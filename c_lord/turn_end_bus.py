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


class TurnEndBus:
    """Per-thread record of the transcript's turn boundaries.

    One instance is shared process-wide (module-level singleton).  Everything
    runs on the asyncio loop, so no locking is needed.
    """

    def __init__(self) -> None:
        self._threads: dict[int, _ThreadTurns] = {}

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
        self._record(thread_id).prompt_at = moment
        logger.debug("turn_end_bus: prompt thread=%d at=%s", thread_id, moment.isoformat())

    def mark(self, thread_id: int, *, at: datetime | None = None) -> None:
        """Record that a turn ended in *thread_id* at *at*."""
        moment = _as_utc(at) if at is not None else datetime.now(_UTC)
        self._record(thread_id).ended_at = moment
        logger.debug("turn_end_bus: marked thread=%d at=%s", thread_id, moment.isoformat())

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

    def forget(self, thread_id: int) -> None:
        """Drop *thread_id*'s record (workspace teardown, tests)."""
        self._threads.pop(thread_id, None)

    def size(self) -> int:
        """How many threads are on record — for the bound's own test."""
        return len(self._threads)


# Module-level singleton — import this everywhere.
turn_end_bus = TurnEndBus()
