"""#769: the hand-off from the transcript mirror to a turn's stall lamp."""

from __future__ import annotations

from c_lord.turn_activity import turn_activity


def test_note_reaches_the_threads_listener() -> None:
    seen: list[int] = []
    unsubscribe = turn_activity.subscribe(10, lambda: seen.append(10))
    try:
        turn_activity.note(10)
        turn_activity.note(10)
    finally:
        unsubscribe()
    assert seen == [10, 10]


def test_note_for_another_thread_is_not_delivered() -> None:
    seen: list[int] = []
    unsubscribe = turn_activity.subscribe(11, lambda: seen.append(11))
    try:
        turn_activity.note(12)
    finally:
        unsubscribe()
    assert seen == []


def test_note_without_a_listener_is_a_no_op() -> None:
    turn_activity.note(13)  # must not raise


def test_unsubscribe_stops_delivery() -> None:
    seen: list[int] = []
    unsubscribe = turn_activity.subscribe(14, lambda: seen.append(14))
    unsubscribe()
    turn_activity.note(14)
    assert seen == []


def test_a_stale_unsubscribe_does_not_drop_the_next_turns_listener() -> None:
    """The next turn subscribes before the previous one has unwound."""
    seen: list[str] = []
    old = turn_activity.subscribe(15, lambda: seen.append("old"))
    new = turn_activity.subscribe(15, lambda: seen.append("new"))
    old()
    turn_activity.note(15)
    new()
    assert seen == ["new"]


def test_a_failing_listener_does_not_propagate() -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    unsubscribe = turn_activity.subscribe(16, boom)
    try:
        turn_activity.note(16)  # the mirror must never die for the lamp's sake
    finally:
        unsubscribe()


class TestOpenToolCalls:
    """#905: a call whose result has not arrived is the session working."""

    def test_a_started_call_is_running_until_it_finishes(self) -> None:
        unsubscribe = turn_activity.subscribe(20, lambda: None)
        try:
            turn_activity.note(20, started=["toolu_a"])
            assert turn_activity.running(20)
            turn_activity.note(20, finished=["toolu_a"])
            assert not turn_activity.running(20)
        finally:
            unsubscribe()

    def test_parallel_calls_run_until_the_last_one_finishes(self) -> None:
        unsubscribe = turn_activity.subscribe(21, lambda: None)
        try:
            turn_activity.note(21, started=["a", "b"])
            turn_activity.note(21, finished=["a"])
            assert turn_activity.running(21)
            turn_activity.note(21, finished=["b"])
            assert not turn_activity.running(21)
        finally:
            unsubscribe()

    def test_a_new_turn_forgets_a_call_the_last_one_left_open(self) -> None:
        """The session died mid-call: its result is never written."""
        old = turn_activity.subscribe(22, lambda: None)
        turn_activity.note(22, started=["orphan"])
        old()
        new = turn_activity.subscribe(22, lambda: None)
        try:
            assert not turn_activity.running(22)
        finally:
            new()

    def test_threads_do_not_share_calls(self) -> None:
        unsubscribe = turn_activity.subscribe(23, lambda: None)
        try:
            turn_activity.note(23, started=["x"])
            assert not turn_activity.running(24)
        finally:
            unsubscribe()
