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

    def test_nothing_recorded_means_the_turn_is_still_open(self) -> None:
        assert turn_end_bus.ended_after(1, _utc(-10)) is False

    def test_a_prompt_then_an_ending_after_the_run_started_ends_the_turn(self) -> None:
        started = _utc(-10)
        turn_end_bus.note_prompt(1, at=_utc(-5))
        turn_end_bus.mark(1, at=_utc(-1))
        assert turn_end_bus.ended_after(1, started) is True

    def test_an_ending_with_no_prompt_of_ours_is_not_our_turn(self) -> None:
        """The #365 case: a displaced turn writes its own ``turn_duration``
        while Claude has not read our instruction yet.  Counting it would
        finalize this turn on its first poll and fire "🟡 Claude has finished"
        before Claude had said anything."""
        turn_end_bus.mark(1, at=_utc(-1))
        assert turn_end_bus.ended_after(1, _utc(-10)) is False

    def test_an_ending_before_our_prompt_is_not_our_turn(self) -> None:
        """Order, not just presence: the predecessor ends, *then* Claude reads
        our instruction — its ending is still not ours."""
        turn_end_bus.mark(1, at=_utc(-5))
        turn_end_bus.note_prompt(1, at=_utc(-1))
        assert turn_end_bus.ended_after(1, _utc(-10)) is False

    def test_a_turn_that_finished_before_this_run_is_ignored(self) -> None:
        """Both halves are from the PREVIOUS turn — this run has not begun."""
        turn_end_bus.note_prompt(1, at=_utc(-40))
        turn_end_bus.mark(1, at=_utc(-30))
        assert turn_end_bus.ended_after(1, _utc(-10)) is False

    def test_threads_do_not_see_each_others_turns(self) -> None:
        turn_end_bus.forget(2)
        turn_end_bus.note_prompt(2, at=_utc(-2))
        turn_end_bus.mark(2, at=_utc())
        assert turn_end_bus.ended_after(1, _utc(-10)) is False
        turn_end_bus.forget(2)

    def test_events_with_no_timestamp_are_dated_when_they_were_read(self) -> None:
        """Some builds omit ``timestamp``; the read time is the best we have."""
        started = _utc(-1)
        turn_end_bus.note_prompt(1)
        turn_end_bus.mark(1)
        assert turn_end_bus.ended_after(1, started) is True

    def test_forget_drops_the_thread(self) -> None:
        turn_end_bus.note_prompt(1, at=_utc(-2))
        turn_end_bus.mark(1, at=_utc())
        turn_end_bus.forget(1)
        assert turn_end_bus.ended_after(1, _utc(-10)) is False

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """Transcript timestamps are UTC ``…Z``; a parser that drops the zone
        must not make the comparison raise (or silently mis-order)."""
        naive_now = datetime.now(_UTC).replace(tzinfo=None)
        turn_end_bus.note_prompt(1, at=naive_now)
        turn_end_bus.mark(1, at=naive_now)
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


class TestApiErrorEnding:
    """#908: whether this turn's last reply was Claude Code's synthetic API error."""

    def _bus(self):
        from c_lord.turn_end_bus import TurnEndBus

        return TurnEndBus()

    def test_an_error_as_the_last_reply_is_reported(self) -> None:
        from datetime import datetime, timedelta, timezone

        bus = self._bus()
        start = datetime.now(timezone.utc) - timedelta(seconds=5)  # noqa: UP017
        bus.note_prompt(1)
        bus.note_reply(1, api_error="API Error: Connection lost mid-response.")
        bus.mark(1)
        assert bus.api_error_since(1, start) == "API Error: Connection lost mid-response."

    def test_a_reply_after_the_error_clears_it(self) -> None:
        from datetime import datetime, timedelta, timezone

        bus = self._bus()
        start = datetime.now(timezone.utc) - timedelta(seconds=5)  # noqa: UP017
        bus.note_prompt(1)
        bus.note_reply(1, api_error="API Error: Server error mid-response.")
        bus.note_reply(1, api_error=None)
        bus.mark(1)
        assert bus.api_error_since(1, start) is None

    def test_the_previous_turns_error_is_not_this_ones(self) -> None:
        from datetime import datetime, timedelta, timezone

        bus = self._bus()
        bus.note_prompt(1)
        bus.note_reply(1, api_error="API Error: old")
        bus.mark(1)
        later = datetime.now(timezone.utc) + timedelta(seconds=1)  # noqa: UP017
        assert bus.api_error_since(1, later) is None

    def test_a_new_prompt_starts_clean(self) -> None:
        from datetime import datetime, timedelta, timezone

        bus = self._bus()
        start = datetime.now(timezone.utc) - timedelta(seconds=5)  # noqa: UP017
        bus.note_prompt(1)
        bus.note_reply(1, api_error="API Error: old")
        bus.note_prompt(1)
        bus.mark(1)
        assert bus.api_error_since(1, start) is None
