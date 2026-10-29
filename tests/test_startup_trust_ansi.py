"""The startup trust handling reads the pane with its colours stripped (#1046).

``capture_pane`` keeps the escape sequences (``-e``). Every other place that
answers the folder-trust dialog normalises the capture first; the startup loop
(``_handle_startup_prompts``) did not, and the live dialog colours its cursor
line — ``tests/fixtures/panes/trust_prompt_live_cursor_on_no_ansi_v2_1_312.txt``
is a real ``capture-pane -e`` of Claude Code 2.1.312 in an isolated tmux socket
(2026-10-29): `` ESC[38;5;153m❯ESC[39m ESC[38;5;153mNo,ESC[39m …``. Unreadable,
the cursor was taken to be on Yes and a bare Enter answered "No, exit".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from c_lord.claude.tmux_runner import TmuxClaudeRunner, _normalize_capture

_P = Path(__file__).parent / "fixtures" / "panes"
LIVE_ON_NO_ANSI = (_P / "trust_prompt_live_cursor_on_no_ansi_v2_1_312.txt").read_text()
ON_YES = (_P / "trust_prompt_unnumbered_cursor_on_yes.txt").read_text()
NUMBERED_ON_NO = (_P / "trust_prompt_numbered_cursor_on_no.txt").read_text()
NUMBERED_ON_YES = (_P / "trust_prompt_at_top.txt").read_text()
C, R = "\x1b[38;5;153m", "\x1b[39m"
# The same colouring on the numbered dialog (the review's synthetic pane).
NUMBERED_ON_NO_ANSI = NUMBERED_ON_NO.replace(" ❯ 2. No, exit", f" {C}❯{R} {C}2. No, exit{R}")


def test_the_real_capture_is_coloured_and_reads_once_normalised() -> None:
    assert "\x1b[" in LIVE_ON_NO_ANSI
    assert TmuxClaudeRunner._trust_cursor_label(_normalize_capture(LIVE_ON_NO_ANSI)) == "No, exit"


def _startup(panes: list[str]) -> list[str]:
    keys: list[str] = []
    seq = iter(panes)
    last = [panes[0]]

    def capture(_tid=None) -> str:
        last[0] = next(seq, last[0])
        return last[0]

    mgr = MagicMock()
    mgr.capture_pane.side_effect = capture
    mgr.send_keys.side_effect = lambda _t, k: keys.append(k) or True
    mgr.is_claude_running.return_value = True
    runner = TmuxClaudeRunner(tmux_manager=mgr, thread_id=1046, model="sonnet")
    with (
        patch("c_lord.claude.tmux_runner._MENU_NAV_DELAY", 0.0),
        patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.0),
        patch("c_lord.claude.tmux_runner.asyncio.sleep", return_value=None),
    ):
        asyncio.run(runner._handle_startup_prompts())
    return keys


def test_the_live_coloured_dialog_is_steered_to_yes_before_enter() -> None:
    assert _startup([LIVE_ON_NO_ANSI, ON_YES, ON_YES]) == ["Down", "Enter"]


def test_a_coloured_numbered_dialog_on_no_moves_up_first() -> None:
    assert _startup([NUMBERED_ON_NO_ANSI, NUMBERED_ON_YES, NUMBERED_ON_YES]) == ["Up", "Enter"]
