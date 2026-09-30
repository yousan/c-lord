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
