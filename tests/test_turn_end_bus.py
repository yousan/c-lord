"""#583: the bus that carries "Claude's turn is over" from the transcript.

The tmux pane cannot answer "is the turn over?".  In jsonl bridge mode it
freezes completely once the answer is delivered (so the poll loop's completion
detector never fires and the turn hangs until the 300s inactivity backstop),
and when Claude holds background work it never freezes at all (so even the
backstop cannot fire, and the turn stays open until the user speaks again).

Claude Code's own transcript *does* answer it — ``system/turn_duration`` — and
the TranscriptMirror already reads it.  This bus is how that fact reaches the
runner's poll loop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from c_lord.turn_end_bus import turn_end_bus

# ``datetime.UTC`` is 3.11+ and c-lord supports 3.10.
_UTC = timezone.utc  # noqa: UP017


def _utc(offset_seconds: float = 0.0) -> datetime:
    return datetime.now(_UTC) + timedelta(seconds=offset_seconds)


class TestTurnEndBus:
    def setup_method(self) -> None:
        turn_end_bus.forget(1)

    def test_no_marker_means_the_turn_is_still_open(self) -> None:
        assert turn_end_bus.ended_after(1, _utc(-10)) is False

    def test_a_marker_after_the_turn_started_ends_it(self) -> None:
        started = _utc(-10)
        turn_end_bus.mark(1, at=_utc(-1))
        assert turn_end_bus.ended_after(1, started) is True

    def test_a_marker_from_the_previous_turn_is_ignored(self) -> None:
        """The whole point of comparing timestamps (#365 lives here).

        A turn that is displaced by a new instruction gets its ``turn_duration``
        written at interrupt time — *before* c-lord delivers the next prompt.
        Counting that marker would finalize the new turn on its first poll and
        fire "🟡 Claude has finished" before Claude had said anything.
        """
        turn_end_bus.mark(1, at=_utc(-30))
        assert turn_end_bus.ended_after(1, _utc(-10)) is False

    def test_threads_do_not_see_each_others_markers(self) -> None:
        turn_end_bus.forget(2)
        turn_end_bus.mark(2, at=_utc())
        assert turn_end_bus.ended_after(1, _utc(-10)) is False
        turn_end_bus.forget(2)

    def test_a_marker_with_no_timestamp_is_dated_when_it_was_read(self) -> None:
        """Some builds omit ``timestamp``; the read time is the best we have."""
        started = _utc(-1)
        turn_end_bus.mark(1)
        assert turn_end_bus.ended_after(1, started) is True

    def test_forget_drops_the_thread(self) -> None:
        turn_end_bus.mark(1, at=_utc())
        turn_end_bus.forget(1)
        assert turn_end_bus.ended_after(1, _utc(-10)) is False

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """Transcript timestamps are UTC ``…Z``; a parser that drops the zone
        must not make the comparison raise (or silently mis-order)."""
        turn_end_bus.mark(1, at=datetime.now(_UTC).replace(tzinfo=None))
        assert turn_end_bus.ended_after(1, _utc(-10)) is True

    def test_the_record_is_bounded(self) -> None:
        """One live process mirrors hundreds of threads; the map cannot grow
        without limit."""
        from c_lord.turn_end_bus import _MAX_THREADS

        for tid in range(10_000, 10_000 + _MAX_THREADS + 50):
            turn_end_bus.mark(tid, at=_utc())
        assert turn_end_bus.size() <= _MAX_THREADS
        for tid in range(10_000, 10_000 + _MAX_THREADS + 50):
            turn_end_bus.forget(tid)
