"""A message the TUI takes a while to accept is delivered, not failed (#1169).

Production, 2026-11-02 22:54 (``fixtures/prod_logs/i1169_preempt_send_false_failure.log``):
a follow-up interrupted a running turn, c-lord typed it and pressed Enter, and
the box still showed it after three looks (~1.35s) — so ``send_input`` reported
a delivery failure: ❌ on the message and a ``failure`` turn event. The
transcript has the instruction 10 seconds after the Enter; Claude went on and
did the work (a production deploy) while the dispatcher was told it failed.

Right after an interrupt Claude Code is still winding the old turn down (and
runs its ``UserPromptSubmit`` hook) before the box lets go of what it accepted.
``send_input`` now keeps watching the box — without pressing Enter again — for
a while before it calls the message lost, and counts the transcript having the
message (``pane_echo`` consumed by the mirror) as delivery too.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import TmuxSessionManager, shield_input
from c_lord.transcript.pane_echo import pane_echo

_FIX = Path(__file__).parent / "fixtures"
PROD_LOG = (_FIX / "prod_logs" / "i1169_preempt_send_false_failure.log").read_text()
BOX_EMPTY = (_FIX / "panes" / "input_box_empty.txt").read_text()
BOX_STUCK_PASTE = (_FIX / "panes" / "input_box_stuck_pasted_placeholder.txt").read_text()

THREAD = 1566096200359219310


def _accept_delay() -> float:
    """Seconds from the turn's ``run_claude: enter`` to the transcript having the prompt.

    Read from the real lines. The Enter itself went out a little after the
    ``enter`` line, so the time the box really held the message was a bit
    shorter than this — the test is on the safe side.
    """
    enter = re.search(r"^(\S+ \S+) .*run_claude: enter \(prompt=676 chars\)", PROD_LOG, re.M)
    accepted = re.search(r"^transcript (\S+) user <the 676-character", PROD_LOG, re.M)
    assert enter and accepted
    jst = datetime.strptime(enter.group(1), "%Y-%m-%d %H:%M:%S")
    utc = datetime.strptime(accepted.group(1), "%Y-%m-%dT%H:%M:%S.%fZ")
    return (utc + timedelta(hours=9) - jst).total_seconds()


ACCEPT_DELAY = _accept_delay()
# Same length and shape (multi-line, so it folds) as the production instruction.
PAYLOAD = "\n".join(f"{n}. 追加の指示の行 " + "あ" * 120 for n in range(1, 7))[:676]


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]  # noqa: ARG005
    mgr._vim_mode["w1"] = False
    return mgr


class _ClockPane:
    """The pane as it was: the box holds the message until *accept_at*, then empties.

    ``time.sleep`` is patched to advance the clock, so the replay follows the
    real timing without waiting for it.
    """

    def __init__(self, accept_at: float | None, *, box_after: str = BOX_EMPTY) -> None:
        self.now = 0.0
        self.accept_at = accept_at
        self.box_after = box_after
        self.enters = 0
        self.on_tick: list = []

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for hook in self.on_tick:
            hook(self.now)

    def __call__(self, args: list[str]) -> MagicMock:
        if "capture-pane" in args:
            accepted = self.accept_at is not None and self.now >= self.accept_at
            return MagicMock(returncode=0, stdout=self.box_after if accepted else BOX_STUCK_PASTE)
        if "send-keys" in args and args[-1] == "Enter":
            self.enters += 1
        return MagicMock(returncode=0, stdout="")


def _confirm(pane: _ClockPane) -> bool:
    with (
        patch("c_lord.tmux._run", side_effect=pane),
        patch("c_lord.tmux.time.sleep", side_effect=pane.sleep),
    ):
        return _mgr()._confirm_submitted("t:w1", shield_input(PAYLOAD), THREAD)


@pytest.fixture(autouse=True)
def _clean_echo() -> Iterator[None]:
    pane_echo.clear()
    yield
    pane_echo.clear()


def test_the_fixture_says_ten_seconds() -> None:
    assert 10.0 < ACCEPT_DELAY < 10.5
    assert len(PAYLOAD) == 676


def test_the_production_send_is_delivered() -> None:
    """AC1: the box lets go 10.3s after the Enter — that is a delivery."""
    pane = _ClockPane(accept_at=ACCEPT_DELAY)
    assert _confirm(pane) is True


def test_no_extra_enters_while_waiting_for_the_box() -> None:
    """Only the #560 retries press Enter; the long wait just looks."""
    pane = _ClockPane(accept_at=ACCEPT_DELAY)
    _confirm(pane)
    assert pane.enters == 2  # the two #560 retries, as before


def test_transcript_having_the_message_is_delivery(caplog) -> None:  # noqa: ANN001
    """AC2: the mirror consuming the echo proves Claude has the message."""
    pane = _ClockPane(accept_at=None)  # the box never visibly empties
    pane_echo.register(THREAD, PAYLOAD)

    def mirror_reads_it(now: float) -> None:
        if now >= ACCEPT_DELAY:
            pane_echo.consume_match(THREAD, PAYLOAD)

    pane.on_tick.append(mirror_reads_it)
    with caplog.at_level("WARNING", logger="c_lord.tmux"):
        assert _confirm(pane) is True
    assert any("transcript" in r.getMessage() for r in caplog.records)


def test_a_message_that_never_goes_is_still_a_failure() -> None:
    """AC3: still in the box and never in the transcript → not delivered (#560)."""
    pane = _ClockPane(accept_at=None)
    pane_echo.register(THREAD, PAYLOAD)
    assert _confirm(pane) is False


def test_the_late_delivery_is_logged(caplog) -> None:  # noqa: ANN001
    """AC4: one WARNING says the box let go late, and how late."""
    pane = _ClockPane(accept_at=ACCEPT_DELAY)
    with caplog.at_level("WARNING", logger="c_lord.tmux"):
        _confirm(pane)
    late = [r.getMessage() for r in caplog.records if "left the input box" in r.getMessage()]
    assert len(late) == 1
    assert f"thread={THREAD}" in late[0]
    assert not any("never left" in r.getMessage() for r in caplog.records)
