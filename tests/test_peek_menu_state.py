"""peek_menu_state distinguishes 'menu gone' from 'capture failed' (#485).

The bridge's resolve-watcher must not treat an EMPTY pane capture (a transient
window-mapping/tmux hiccup) as 'the menu closed'. peek_menu_state returns the
open menu (if any) plus a ``capture_ok`` flag so the watcher can ignore empty
captures instead of falsely resolving a still-open menu.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from c_lord.claude.tmux_runner import TmuxClaudeRunner

_FIX = Path(__file__).parent / "fixtures" / "panes"


def _runner(capture_text: str) -> TmuxClaudeRunner:
    tmux = MagicMock()
    tmux.capture_pane.return_value = capture_text
    return TmuxClaudeRunner(tmux_manager=tmux, thread_id=123)


async def test_empty_capture_is_unknown_not_gone() -> None:
    menu, capture_ok = await _runner("").peek_menu_state()
    assert menu is None
    assert capture_ok is False  # empty = capture failed = UNKNOWN, not "menu gone"


async def test_whitespace_only_capture_is_unknown() -> None:
    menu, capture_ok = await _runner("   \n  \n").peek_menu_state()
    assert menu is None
    assert capture_ok is False


async def test_open_menu_detected_with_healthy_capture() -> None:
    menu_text = (_FIX / "ask_context_prose_above_menu.txt").read_text()
    menu, capture_ok = await _runner(menu_text).peek_menu_state()
    assert menu is not None
    assert capture_ok is True


async def test_no_menu_healthy_capture_is_gone() -> None:
    menu, capture_ok = await _runner("a normal shell pane, no menu here\n$ ").peek_menu_state()
    assert menu is None
    assert capture_ok is True  # healthy capture, genuinely no menu → resolvable
