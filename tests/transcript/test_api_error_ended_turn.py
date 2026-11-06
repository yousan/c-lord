"""#1294 — after a turn that stopped on an API error, Claude's own next turn went silent.

Local transcripts (CLI 2.1.300 / 2.1.302 / 2.1.305 / 2.1.309, shape only — 7 cases)
show a main-chain ``isApiErrorMessage`` row (``server_error`` / ``invalid_request``)
followed by the next plain ``user`` row with no ``turn_duration`` in between: the
turn stopped on the error and the CLI wrote no end, so the mirror still thought the
person's turn was open. When the next turn was one Claude started itself (a
background task's notification, or a scheduled wakeup) and it failed the same way,
the thread got nothing: #1292 (#1285) no longer forgot the ⚠️ line already posted,
and the turn was taken for the person's — so the ❌ report of a self-started turn
(#1131) did not go out either. Before #1292 the ⚠️ line was posted again.

docs/specs/turn-end-notification.md — an API-error row that nothing follows (no
tool result, no answer) ends the turn as far as the next notification / wakeup is
concerned. A notification after a refusal the turn kept going past (#1251/#1285)
still leaves the person's turn as it is.
"""

from __future__ import annotations

import pytest

from .test_api_error_repeat import (
    _NOTIF,
    _TIMES,
    _TURN,
    _api_lines,
    _base,
    _turn_end,
    _user_text,
    _wakeup,
)

_SERVER = "API Error: 500 Internal server error. Double press esc to edit your last message."


def _api_error_end(ts: str, n: int) -> list[dict]:
    """A turn's request failed: the synthetic error row, and nothing after it."""
    err = _base("assistant", ts, f"err{n}", f"p{n}")
    err.update(
        isApiErrorMessage=True,
        error="server_error",
        message={
            "role": "assistant",
            "model": "<synthetic>",
            "type": "message",
            "content": [{"type": "text", "text": _SERVER}],
        },
    )
    return [err]


def _rows(start: str) -> list[dict | str]:
    rows: list[dict | str] = [_TURN]
    rows += _api_error_end(_TIMES[0], 0)  # the person's turn dies here, no turn_duration
    rows.append(
        _user_text("n1", "err0", _NOTIF) if start == "notification" else _wakeup("w", "err0")
    )
    rows += _api_error_end(_TIMES[1], 1)  # Claude's own next turn fails the same way
    rows.append(_turn_end("end", "err1"))
    return rows


@pytest.mark.parametrize("start", ["notification", "wakeup"])
async def test_self_started_turn_after_an_api_error_ended_turn_posts_again(
    tmp_path, start: str
) -> None:
    lines = await _api_lines(tmp_path, _rows(start))
    assert len(lines) == 2, lines


@pytest.mark.parametrize("start", ["notification", "wakeup"])
async def test_self_started_turn_after_an_api_error_ended_turn_reports_its_failure(
    tmp_path, start: str
) -> None:
    """#1131: the turn Claude started is reported as failed — it is not the person's."""
    reports: list[str] = []
    await _api_lines(tmp_path, _rows(start), failure_reports=reports)
    assert reports == [_SERVER], reports


async def test_a_self_started_turn_that_writes_no_reply_does_not_report_the_old_error(
    tmp_path,
) -> None:
    """Review of #1294: the person's error belongs to the person's turn (their runner's ❌).

    The notification starts a new turn; if that turn ends before Claude writes
    anything, it did not stop on an API error — no ❌ for the old one.
    """
    rows: list[dict | str] = [_TURN]
    rows += _api_error_end(_TIMES[0], 0)
    rows.append(_user_text("n1", "err0", _NOTIF))
    rows.append(_turn_end("end", "n1"))
    reports: list[str] = []
    await _api_lines(tmp_path, rows, failure_reports=reports)
    assert reports == [], reports


async def test_restart_after_an_api_error_ended_turn_still_reports_the_next_self_turn(
    tmp_path, monkeypatch
) -> None:
    """Review of #1294: the old process saw the person's turn die on the error.

    The new process reads that back (#1011), so the notification after the
    restart still starts Claude's own turn, and its failure is reported once.
    """
    from c_lord import turn_events
    from c_lord.skills import reply_tracker

    from . import test_self_turn_api_error as a
    from .test_restart_self_turn_api_error import _lines, _Restored

    log = tmp_path / "turn-events.jsonl"
    monkeypatch.setenv(turn_events.ENV_LOG, str(log))
    turn_events.reset()
    reply_tracker.reset_tracker()
    person = a._row(
        "user", "2026-10-29T08:21:05.000Z", message={"role": "user", "content": "確認して"}
    )
    try:
        async with _Restored(tmp_path, person, a._api_error()) as r:
            await r.feed(
                *a._notification("2026-10-29T08:40:00.000Z", "b1"),
                {**a._api_error(), "uuid": "err-after-restart"},  # a row of its own
                a._end("2026-10-29T08:40:05.000Z"),
            )
        assert len(r.notices) == 1, r.notices
        assert [(e["kind"], e.get("reason")) for e in _lines(log)] == [("failure", "api_error")]
    finally:
        turn_events.reset()
        reply_tracker.reset_tracker()


def test_read_back_replays_the_api_error_turn_end(tmp_path) -> None:
    """The read-back follows the live rule (#1011): a notification after the stop is Claude's."""
    import json

    from c_lord.transcript.mirror import _read_back_state, _turn_state_at_end

    from . import test_self_turn_api_error as a

    person = a._row("user", "2026-10-29T08:21:05.000Z", message={"role": "user", "content": "x"})
    rows = [person, a._api_error()]
    path = tmp_path / "s.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    assert _read_back_state(path)[4] is True
    rows += a._notification("2026-10-29T08:40:00.000Z", "b1")
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    assert _turn_state_at_end(path) == ("notification", "2026-10-29T08:40:00.000Z", set())
    assert _read_back_state(path)[4] is False


async def test_restart_after_the_notification_does_not_report_the_persons_error(
    tmp_path, monkeypatch
) -> None:
    """Review of 04788c4: the read-back drops the old error at the new turn, like the live loop.

    Before the restart: the person's turn dies on the error, then a notification
    starts Claude's own turn. After it: that turn ends with no reply — no ❌.
    """
    from c_lord import turn_events
    from c_lord.skills import reply_tracker

    from . import test_self_turn_api_error as a
    from .test_restart_self_turn_api_error import _lines, _Restored

    log = tmp_path / "turn-events.jsonl"
    monkeypatch.setenv(turn_events.ENV_LOG, str(log))
    turn_events.reset()
    reply_tracker.reset_tracker()
    person = a._row(
        "user", "2026-10-29T08:21:05.000Z", message={"role": "user", "content": "確認して"}
    )
    before = (person, a._api_error(), *a._notification("2026-10-29T08:40:00.000Z", "b1"))
    try:
        async with _Restored(tmp_path, *before) as r:
            await r.feed(a._end("2026-10-29T08:40:05.000Z"))
        assert r.notices == [], r.notices
        assert [e["kind"] for e in _lines(log)] == ["completion"], _lines(log)
    finally:
        turn_events.reset()
        reply_tracker.reset_tracker()
