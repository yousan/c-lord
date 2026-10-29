"""The startup trust handling reads the pane with its colours stripped (#1046).

``capture_pane`` keeps the escape sequences (``-e``). Every other place that
answers the folder-trust dialog normalises the capture first; the startup loop
(``_handle_startup_prompts``) did not, and the live dialog colours its cursor
line — ``tests/fixtures/panes/trust_prompt_live_cursor_on_no_ansi_v2_1_312.txt``
is a real ``capture-pane -e`` of Claude Code 2.1.312 in an isolated tmux socket
(2026-10-29): `` ESC[38;5;153m❯ESC[39m ESC[38;5;153mNo,ESC[39m …``.

Unnormalised, the two shapes failed differently. The current (unnumbered) shape
needs the ``Enter to confirm`` footer, which is coloured word by word too, so the
startup loop never noticed the dialog and sent nothing — the run loop answered it
later. The numbered shape (≤ 2.1.247) does not need the footer: the dialog was
seen, the coloured cursor line was not, and a bare Enter answered "No, exit".

The panes below stay coloured and only change once a key has been sent, as the
real CLI does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from c_lord.claude.tmux_runner import TmuxClaudeRunner, _normalize_capture

_P = Path(__file__).parent / "fixtures" / "panes"
LIVE_ON_NO_ANSI = (_P / "trust_prompt_live_cursor_on_no_ansi_v2_1_312.txt").read_text()
NUMBERED_ON_NO = (_P / "trust_prompt_numbered_cursor_on_no.txt").read_text()
NUMBERED_ON_YES = (_P / "trust_prompt_at_top.txt").read_text()
C, R = "\x1b[38;5;153m", "\x1b[39m"
# What the real CLI draws after Down: the same colouring, cursor on Yes.
LIVE_ON_YES_ANSI = LIVE_ON_NO_ANSI.replace(
    f" {C}❯{R} {C}No,{R} {C}exit{R}\n   Yes, I trust this folder",
    f"   No, exit\n {C}❯{R} {C}Yes,{R} {C}I{R} {C}trust{R} {C}this{R} {C}folder{R}",
)
# The same colouring on the numbered dialog (the review's synthetic pane).
NUMBERED_ON_NO_ANSI = NUMBERED_ON_NO.replace(" ❯ 2. No, exit", f" {C}❯{R} {C}2. No, exit{R}")
NUMBERED_ON_YES_ANSI = NUMBERED_ON_YES.replace(
    " ❯ 1. Yes, I trust this folder", f" {C}❯{R} {C}1. Yes, I trust this folder{R}"
)


def test_the_real_capture_is_coloured_and_reads_once_normalised() -> None:
    assert "\x1b[" in LIVE_ON_NO_ANSI
    assert TmuxClaudeRunner._trust_cursor_label(_normalize_capture(LIVE_ON_NO_ANSI)) == "No, exit"


def _startup(initial: str, after_key: str) -> list[str]:
    """The pane shows ``initial`` until a key is sent, then ``after_key``."""
    keys: list[str] = []
    mgr = MagicMock()
    mgr.capture_pane.side_effect = lambda _tid=None: after_key if keys else initial
    mgr.send_keys.side_effect = lambda _t, k: keys.append(k) or True
    mgr.is_claude_running.return_value = True
    runner = TmuxClaudeRunner(tmux_manager=mgr, thread_id=1046, model="sonnet")
    with (
        patch("c_lord.claude.tmux_runner._MENU_NAV_DELAY", 0.0),
        patch("c_lord.claude.tmux_runner.asyncio.sleep", return_value=None),
    ):
        asyncio.run(runner._handle_startup_prompts())
    return keys


def test_the_live_coloured_dialog_is_steered_to_yes_before_enter() -> None:
    assert LIVE_ON_YES_ANSI != LIVE_ON_NO_ANSI
    assert _startup(LIVE_ON_NO_ANSI, LIVE_ON_YES_ANSI) == ["Down", "Enter"]


def test_a_coloured_numbered_dialog_on_no_moves_up_first() -> None:
    assert NUMBERED_ON_YES_ANSI != NUMBERED_ON_YES
    assert _startup(NUMBERED_ON_NO_ANSI, NUMBERED_ON_YES_ANSI) == ["Up", "Enter"]
