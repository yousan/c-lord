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


# -- #510: a corpse pane's leftover menu resolves the bridge -------------------
# An in-flight bridge polls peek_menu_state to notice the menu being answered in
# the TUI. When claude is gone (reboot + tmux-resurrect restores only the shell
# and the saved screen), the frozen text kept reading as "still open", so the
# bridge sat on the 24h timeout instead of winding down.


def _runner_with_command(capture_text: str, foreground):
    tmux = MagicMock()
    tmux.capture_pane.return_value = capture_text
    tmux.pane_foreground_command.return_value = foreground
    return TmuxClaudeRunner(tmux_manager=tmux, thread_id=123)


async def test_menu_in_dead_pane_is_gone() -> None:
    ghost = (_FIX / "ghost_menu_dead_pane.txt").read_text()
    menu, capture_ok = await _runner_with_command(ghost, "zsh").peek_menu_state()
    assert menu is None  # claude is not running → the text is a corpse, not a menu
    assert capture_ok is True  # healthy read → the bridge may wind down


async def test_menu_in_live_claude_pane_is_still_open() -> None:
    menu_text = (_FIX / "ask_context_prose_above_menu.txt").read_text()
    menu, capture_ok = await _runner_with_command(menu_text, "claude").peek_menu_state()
    assert menu is not None
    assert capture_ok is True


async def test_unknown_foreground_command_does_not_suppress_menu() -> None:
    """None = could not read the pane command = UNKNOWN, not 'dead'."""
    menu_text = (_FIX / "ask_context_prose_above_menu.txt").read_text()
    menu, capture_ok = await _runner_with_command(menu_text, None).peek_menu_state()
    assert menu is not None
    assert capture_ok is True


async def test_peek_pending_ask_ignores_dead_pane() -> None:
    """The post-turn recovery peek must not resurrect a corpse either."""
    ghost = (_FIX / "ghost_menu_dead_pane.txt").read_text()
    assert await _runner_with_command(ghost, "zsh").peek_pending_ask() is None
