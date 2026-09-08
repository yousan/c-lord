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

**Timestamps, not just "a marker arrived".** A turn displaced by a new
instruction has its own ``turn_duration`` written at interrupt time — before
c-lord delivers the next prompt.  Honouring that marker would finalize the new
turn on its first poll and fire 🟡 before Claude had said anything, which is
#365 all over again.  So the marker carries the transcript event's own
timestamp and the runner compares it against the moment its prompt went in.
"""

from __future__ import annotations

import logging
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


class TurnEndBus:
    """Per-thread record of the last transcript turn-end marker.

    One instance is shared process-wide (module-level singleton).  Everything
    runs on the asyncio loop, so no locking is needed.
    """

    def __init__(self) -> None:
        self._ended: dict[int, datetime] = {}

    def mark(self, thread_id: int, *, at: datetime | None = None) -> None:
        """Record that a turn ended in *thread_id* at *at*.

        *at* is the transcript event's own timestamp.  ``None`` means the event
        carried none — some Claude Code builds omit it — and the time it was
        read is then the closest thing to the truth available.
        """
        moment = _as_utc(at) if at is not None else datetime.now(_UTC)
        self._ended[thread_id] = moment
        while len(self._ended) > _MAX_THREADS:
            self._ended.pop(next(iter(self._ended)))  # oldest insertion first
        logger.debug("turn_end_bus: marked thread=%d at=%s", thread_id, moment.isoformat())

    def ended_after(self, thread_id: int, started_at: datetime) -> bool:
        """Has a turn ended in *thread_id* since *started_at*?

        ``False`` means "no evidence" — never "the turn is still running".  The
        caller keeps its own pane-based detection as the fallback, so a build
        that writes no marker behaves exactly as it did before.
        """
        ended = self._ended.get(thread_id)
        return ended is not None and ended > _as_utc(started_at)

    def forget(self, thread_id: int) -> None:
        """Drop *thread_id*'s record (workspace teardown, tests)."""
        self._ended.pop(thread_id, None)

    def size(self) -> int:
        """How many threads are on record — for the bound's own test."""
        return len(self._ended)


# Module-level singleton — import this everywhere.
turn_end_bus = TurnEndBus()
