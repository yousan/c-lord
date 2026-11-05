"""A completion held for background agents survives a c-lord restart (#1249).

When a turn ends with background agents still out, its ``completion`` is held
until they report back (#917). The hold lived only in memory, so a restart
before the agents came back lost it: the request's end was never announced, and
the agents' return went out as Claude's own ``self_resumed_turn``. Whoever
dispatched the request (another Claude, a webhook) never heard that its work
was done.

Production 2026-11-05, thread=1564916260414951485 (v1.4.389 → v1.4.390):
  06:50:08 completion held — 2 background agent(s) still running (#917)
  06:52:31 restart
  07:07:32 completion started_by=self reason=self_resumed_turn
  (no completion with started_by=webhook for the 06:48:08 request)

``turn_events.reset()`` stands in for the restart: it empties every table in
memory, as a new process would. Only what is on disk carries over.
"""

from __future__ import annotations

import time

from c_lord import turn_events
from c_lord.turn_events import StartedBy
from tests.test_turn_events import THREAD_ID, _clean, _lines, _thread, log  # noqa: F401


async def test_held_completion_survives_a_restart(log):  # noqa: F811
    info = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
    turn_events.note_turn_end(THREAD_ID, 2)
    turn_events.end_turn(info)
    turn_events.finish_turn(info)
    await turn_events.drain()
    assert _lines(log) == []  # held, as in production 06:50:08

    turn_events.reset()  # c-lord restarts (06:52:31): a new process, nothing in memory

    # The agents report back; Claude opens a turn on the notification and ends
    # it with none left (07:07:32 in production).
    turn_events.note_self_turn_started(THREAD_ID)
    turn_events.note_turn_end(THREAD_ID, 0, self_started=True)
    await turn_events.drain()

    events = _lines(log)
    assert [e["started_by"]["type"] for e in events] == ["webhook"], events
    event = events[0]
    assert event["kind"] == "completion"
    assert event["started_by"] == {"type": "webhook", "id": "7", "name": "Captain Hook"}
    assert event["guild_id"] == "555"
    assert event["channel_id"] == "777"
    assert "reason" not in event


async def test_a_hold_whose_time_ran_out_while_down_is_released_at_startup(log, monkeypatch):  # noqa: F811
    monkeypatch.setenv(turn_events.ENV_DEFER_MAX, "60")
    info = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
    turn_events.note_turn_end(THREAD_ID, 1)
    turn_events.end_turn(info)
    turn_events.finish_turn(info)
    await turn_events.drain()
    assert _lines(log) == []

    turn_events.reset()
    # The bot stayed down past the limit.
    real = time.time
    monkeypatch.setattr(turn_events.time, "time", lambda: real() + 120)
    turn_events.restore_held()
    await turn_events.drain()

    events = _lines(log)
    assert [(e["kind"], e.get("reason")) for e in events] == [
        ("completion", "background_agents_unresolved")
    ], events
    assert events[0]["started_by"]["type"] == "webhook"


async def test_a_restored_hold_still_expires(log, monkeypatch):  # noqa: F811
    monkeypatch.setenv(turn_events.ENV_DEFER_MAX, "0.05")
    info = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
    turn_events.note_turn_end(THREAD_ID, 1)
    turn_events.end_turn(info)
    turn_events.finish_turn(info)
    turn_events.reset()

    turn_events.restore_held()
    import asyncio

    await asyncio.sleep(0.2)
    await turn_events.drain()
    assert [e.get("reason") for e in _lines(log)] == ["background_agents_unresolved"]


async def test_a_released_hold_is_not_sent_again_after_the_next_restart(log):  # noqa: F811
    info = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
    turn_events.note_turn_end(THREAD_ID, 1)
    turn_events.end_turn(info)
    turn_events.finish_turn(info)
    turn_events.note_turn_end(THREAD_ID, 0)  # the agent came back before any restart
    await turn_events.drain()
    assert len(_lines(log)) == 1

    turn_events.reset()
    turn_events.restore_held()
    turn_events.note_turn_end(THREAD_ID, 0)
    await turn_events.drain()
    assert len(_lines(log)) == 1


async def test_a_hold_a_new_turn_dropped_is_not_sent_after_a_restart(log):  # noqa: F811
    info = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
    turn_events.note_turn_end(THREAD_ID, 1)
    turn_events.end_turn(info)
    turn_events.finish_turn(info)
    turn_events.begin_turn(_thread(), StartedBy("user", "9"))  # a person asks again

    turn_events.reset()
    turn_events.note_turn_end(THREAD_ID, 0)
    await turn_events.drain()
    assert _lines(log) == []


async def test_an_unreadable_state_file_is_ignored(log):  # noqa: F811
    path = turn_events.held_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    turn_events.reset()
    turn_events.note_turn_end(THREAD_ID, 0)
    await turn_events.drain()
    assert _lines(log) == []


def _hold_for_agents(outstanding: int = 1) -> None:
    info = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
    turn_events.note_turn_end(THREAD_ID, outstanding)
    turn_events.end_turn(info)
    turn_events.finish_turn(info)


async def test_a_restart_crossed_turn_and_a_restored_hold_send_one_completion(
    log,  # noqa: F811
    monkeypatch,
):
    """Review of #1249: the turn that crossed the restart is the thread's end.

    Its ``restart_crossed`` completion and the restored hold must not both go
    out — the receiver would be told twice that the thread finished.
    """
    _hold_for_agents()
    await turn_events.drain()
    turn_events.reset()

    monkeypatch.setattr("c_lord.transcript.mirror.background_agents_running", lambda _thread_id: 0)
    turn_events.announce_recovered_turn(
        _thread(), StartedBy("user", "9"), started_at=None, failure_reason=None
    )
    turn_events.note_turn_end(THREAD_ID, 0)
    await turn_events.drain()

    events = [(e["kind"], e.get("reason")) for e in _lines(log)]
    assert events == [("completion", "restart_crossed")], events
    assert not turn_events.held_state_path().exists()


async def test_a_menu_wait_hold_is_not_carried_over_a_restart(log):  # noqa: F811
    """Review of #1249: the previous process's menus are retired at startup (#671).

    Nobody can answer them, so a hold waiting on one must not come back as a
    ``completion`` after the restart.
    """
    info = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
    turn_events.end_turn(info)
    turn_events.finish_turn(info, awaiting_answer=True)
    await turn_events.drain()
    assert _lines(log) == []

    turn_events.reset()
    turn_events.note_turn_end(THREAD_ID, 0)
    await turn_events.drain()
    assert _lines(log) == []


async def test_a_release_that_cannot_be_saved_says_a_restart_may_repeat_it(
    log,  # noqa: F811
    monkeypatch,
    caplog,
):
    import logging

    _hold_for_agents()

    def broken(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise OSError("disk full")

    # Releasing the only hold removes the file; that removal fails.
    monkeypatch.setattr(turn_events.Path, "unlink", broken)
    with caplog.at_level(logging.WARNING, logger="c_lord.turn_events"):
        turn_events.note_turn_end(THREAD_ID, 0)
        await turn_events.drain()

    assert len(_lines(log)) == 1
    assert "may be sent again after a restart" in caplog.text


async def test_startup_restores_the_held_completions(log, monkeypatch):  # noqa: F811
    """The real startup path (``TranscriptMirrorCog.on_ready``) reads the holds back.

    Without it, a hold whose limit ran out while c-lord was down would wait for
    a turn event that may never come.
    """
    from unittest.mock import AsyncMock, MagicMock

    from c_lord.cogs import transcript_mirror as cog_module

    monkeypatch.setenv(turn_events.ENV_DEFER_MAX, "60")
    _hold_for_agents()
    await turn_events.drain()
    turn_events.reset()
    real = time.time
    monkeypatch.setattr(turn_events.time, "time", lambda: real() + 120)

    monkeypatch.setattr(cog_module, "live_claude_panes", lambda: set())
    monkeypatch.setattr(cog_module, "notify_host_restart_stops", AsyncMock(return_value=0))
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[])
    cog = cog_module.TranscriptMirrorCog(MagicMock(), session_repo=repo)
    try:
        await cog.on_ready()
        await turn_events.drain()
    finally:
        await cog.cog_unload()

    assert [e.get("reason") for e in _lines(log)] == ["background_agents_unresolved"]
