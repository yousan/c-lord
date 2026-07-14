"""send_input must never let a plain reply select an open menu (#485).

The phantom-answer incident's final step: a normal user reply was typed into a
pane whose AskUserQuestion menu was still open, so the trailing Enter selected
the highlighted default option ("本文を削除") — an answer the user never made.

send_input must detect an open menu and dismiss it (Esc) BEFORE typing, so the
message reaches Claude as text, never as a menu selection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager

_FIX = Path(__file__).parent / "fixtures" / "panes"


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda tid: "w1"  # type: ignore[method-assign]
    return mgr


def _sendkeys(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "send-keys" in c]


def test_send_input_dismisses_open_menu_before_typing() -> None:
    menu = (_FIX / "ask_context_prose_above_menu.txt").read_text()
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        if "capture-pane" in args:
            return MagicMock(returncode=0, stdout=menu)
        return MagicMock(returncode=0, stdout="")

    with patch("c_lord.tmux._run", side_effect=fake_run):
        _mgr().send_input(12345, "これは普通の返信です。選択する意図はありません")

    sk = _sendkeys(calls)
    esc_idx = next((i for i, c in enumerate(sk) if "Escape" in c), None)
    text_idx = next(
        (i for i, c in enumerate(sk) if "-l" in c and any("返信" in a for a in c)), None
    )
    assert esc_idx is not None, "send_input must Esc an open menu before typing (#485)"
    assert text_idx is not None, "the message text must still be sent"
    assert esc_idx < text_idx, "Escape must come BEFORE the message text"


def test_send_input_no_menu_sends_no_escape() -> None:
    """A normal (menu-less) pane must not get a spurious Esc."""
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        if "capture-pane" in args:
            return MagicMock(returncode=0, stdout="a normal shell, no menu here\n$ ")
        return MagicMock(returncode=0, stdout="")

    with patch("c_lord.tmux._run", side_effect=fake_run):
        _mgr().send_input(12345, "hello")

    assert not any("Escape" in c for c in _sendkeys(calls)), "must not Esc when no menu is open"
