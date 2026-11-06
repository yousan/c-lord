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
