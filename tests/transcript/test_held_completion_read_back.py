# ruff: noqa: F811  (the ``log`` fixture is imported, then requested by name)
"""A held completion goes out at startup when the agents came back while c-lord was down (#1281).

#1249 carries a completion held for background agents across a restart. But
when the agents reported back, and the turn Claude started on their report also
ended, while c-lord was down, nobody released it: the new process tails the
transcript from its end, so it never sees that turn end. The requester (a
dispatching agent, a webhook) waited up to an hour for work that was already
done, and then heard ``background_agents_unresolved`` about it.

The restored mirror already reads the transcript back (#1011/#1012). When it
finds no turn open, no agent out and every report answered by a turn end, the
held completion goes out at once, with the request's ``started_by`` and reason.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from c_lord import turn_events
from c_lord.turn_events import StartedBy

from .test_restart_crossed_turn import (  # noqa: F401
    _NOTIFICATION,
    THREAD_ID,
    TURN_END,
    _clean,
    _launch,
    _lines,
    _report,
    _Restored,
    _text,
    _thread,
    _tool_use,
    log,
)

_NOTIFICATION_TURN = {"type": "user", "message": {"role": "user", "content": _NOTIFICATION}}


def _hold_before_restart() -> None:
    """Old process: a webhook request ends with one background agent still out."""
    info = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
    turn_events.note_turn_end(THREAD_ID, 1)
    turn_events.end_turn(info)
    turn_events.finish_turn(info)


async def test_agent_returned_while_down_completion_goes_out_on_restart(
    tmp_path: Path, log: Path
) -> None:
    """RED on main: nothing goes out and the hold waits for its 1 h limit."""
    _hold_before_restart()
    await turn_events.drain()
    assert _lines(log) == []
    turn_events.reset()  # restart

    # What the old process already read, then what Claude wrote while c-lord was down.
    async with _Restored(
        tmp_path,
        _tool_use(),
        _launch("a1b2c3"),
        _text("エージェントを待っています。"),
        TURN_END,
        _report("a1b2c3"),
        _NOTIFICATION_TURN,
        _text("全部そろいました。"),
        TURN_END,
    ):
        turn_events.restore_held()  # TranscriptMirrorCog.on_ready
        await asyncio.sleep(0.5)
        await turn_events.drain()
        events = _lines(log)
    assert [(e["kind"], e["started_by"], e.get("reason")) for e in events] == [
        ("completion", {"type": "webhook", "id": "7", "name": "Captain Hook"}, None)
    ], (events, list(turn_events._HELD))
    assert not turn_events.held_state_path().exists()


async def test_the_hold_keeps_its_own_reason(tmp_path: Path, log: Path) -> None:
    """A turn that crossed an earlier restart was held as ``restart_crossed``."""
    info = turn_events.begin_turn(_thread(), StartedBy("user", "9"))
    turn_events.end_turn(info)
    turn_events._hold(info, 1, reason="restart_crossed")
    turn_events.reset()
    async with _Restored(
        tmp_path, _tool_use(), _launch("a1"), TURN_END, _report("a1"), _NOTIFICATION_TURN, TURN_END
    ):
        await asyncio.sleep(0.3)
        await turn_events.drain()
    events = _lines(log)
    assert [(e["started_by"]["id"], e.get("reason")) for e in events] == [
        ("9", "restart_crossed")
    ], events


async def test_an_agent_still_out_keeps_the_hold(tmp_path: Path, log: Path) -> None:
    _hold_before_restart()
    turn_events.reset()
    async with _Restored(
        tmp_path,
        _tool_use(),
        _launch("a1"),
        _launch("a2"),
        TURN_END,
        _report("a1"),
        _NOTIFICATION_TURN,
        TURN_END,
    ):
        turn_events.restore_held()
        await asyncio.sleep(0.3)
        await turn_events.drain()
        assert _lines(log) == []
        assert THREAD_ID in turn_events._HELD


async def test_the_report_turn_still_running_releases_at_its_end(tmp_path: Path, log: Path) -> None:
    """The agent is back but Claude is still writing its answer: wait for that turn's end."""
    _hold_before_restart()
    turn_events.reset()
    async with _Restored(
        tmp_path, _tool_use(), _launch("a1"), TURN_END, _report("a1"), _NOTIFICATION_TURN
    ) as restored:
        await asyncio.sleep(0.3)
        await turn_events.drain()
        assert _lines(log) == []
        await restored.feed(_text("全部そろいました。"), TURN_END)
    events = _lines(log)
    assert [(e["started_by"]["type"], e.get("reason")) for e in events] == [("webhook", None)]


async def test_a_report_not_yet_taken_up_waits_for_its_turn(tmp_path: Path, log: Path) -> None:
    """The report is queued but Claude has not opened its turn yet: the answer is not out."""
    _hold_before_restart()
    turn_events.reset()
    async with _Restored(tmp_path, _tool_use(), _launch("a1"), TURN_END, _report("a1")) as restored:
        await asyncio.sleep(0.3)
        await turn_events.drain()
        assert _lines(log) == []
        await restored.feed(_NOTIFICATION_TURN, _text("全部そろいました。"), TURN_END)
    events = _lines(log)
    assert [(e["started_by"]["type"], e.get("reason")) for e in events] == [("webhook", None)]
