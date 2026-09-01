"""Tests for ``TmuxSessionManager.capture_pane_tall`` (#468).

A full-screen TUI like Claude Code keeps no scrollback in its alternate screen,
so a normal ``capture_pane`` only ever returns the *visible* rows. When the prose
above an AskUserQuestion menu is taller than the window, its head scrolls off and
is unrecoverable — ``_extract_pane_context`` then returns "". Claude redraws its
whole conversation on SIGWINCH, so transiently enlarging the window reveals the
scrolled-off prose. ``capture_pane_tall`` grows the window, captures, then
restores the original size.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from c_lord.claude.tmux_runner import _parse_ask_from_pane
from c_lord.tmux import TmuxSessionManager

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "panes"


def _load_fixture(name: str) -> str:
    return (_FIXTURES_DIR / name).read_text()


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr._thread_to_window[12345] = "w1"
    # Isolate from window-resolution _run calls — tested elsewhere.
    mgr._find_window_for_thread = lambda tid: "w1"  # type: ignore[method-assign]
    return mgr


class TestCapturePaneTall:
    def test_grows_captures_then_restores_original_size(self) -> None:
        """The window is enlarged for the capture, then restored to its exact
        pre-capture size — and the captured (tall) text is returned."""
        tall_text = "● the full pre-menu prose that had scrolled off\n  more lines\n"
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="160 40\n"),  # display-message: current size
                MagicMock(returncode=0),  # resize-window UP
                MagicMock(returncode=0, stdout=tall_text),  # capture-pane (tall)
                MagicMock(returncode=0),  # resize-window RESTORE
            ]
            out = _mgr().capture_pane_tall(12345, tall_rows=240, settle_timeout=0.0)

        assert out == tall_text

        resize_calls = [c.args[0] for c in mock_run.call_args_list if "resize-window" in c.args[0]]
        assert len(resize_calls) == 2, f"expected grow+restore, got {resize_calls}"
        # First resize grows to the tall height; last restores the original 40.
        assert "240" in resize_calls[0]
        assert resize_calls[-1][-1] == "40", f"must restore original height: {resize_calls[-1]}"
        # Restore happens AFTER the capture (capture is between the two resizes).
        order = [
            ("resize" if "resize-window" in c.args[0] else "capture")
            for c in mock_run.call_args_list
            if "resize-window" in c.args[0] or "capture-pane" in c.args[0]
        ]
        assert order == ["resize", "capture", "resize"]

    def test_restores_size_even_if_capture_fails(self) -> None:
        """A failed capture must still restore the original window size."""
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="160 40\n"),  # current size
                MagicMock(returncode=0),  # resize UP
                MagicMock(returncode=1, stdout=""),  # capture FAILS
                MagicMock(returncode=0),  # resize RESTORE (must still happen)
            ]
            _mgr().capture_pane_tall(12345, tall_rows=240, settle_timeout=0.0)

        resize_calls = [c.args[0] for c in mock_run.call_args_list if "resize-window" in c.args[0]]
        assert len(resize_calls) == 2
        assert resize_calls[-1][-1] == "40"

    def test_unavailable_returns_empty_without_resize(self) -> None:
        mgr = _mgr()
        mgr._available = False
        with patch("c_lord.tmux._run") as mock_run:
            assert mgr.capture_pane_tall(12345) == ""
        mock_run.assert_not_called()


class TestLongProseContextFixtures:
    """Real captured panes (#468) showing WHY the tall capture is needed.

    Both fixtures are the SAME AskUserQuestion menu with a long (~58-line)
    prose block above it, captured from a live Claude TUI:
    - ``..._40rows``: a 40-row window. The prose head scrolled off the
      alternate screen, so ``_extract_pane_context`` recovers nothing.
    - ``..._tall``: the same window grown to 240 rows. Claude redrew the whole
      conversation, so the full prose is now captured and extracted.

    The menu itself (question/options) parses identically in both — proving the
    window height only affects the *context*, never menu detection (a question
    appearing is always bridged).
    """

    SHORT = "bug_468_long_prose_40rows.txt"
    TALL = "bug_468_long_prose_tall.txt"

    def test_short_pane_loses_context(self) -> None:
        q = _parse_ask_from_pane(_load_fixture(self.SHORT))
        assert q is not None  # the menu is still detected
        assert q.context == ""  # but the scrolled-off prose is lost (the bug)

    def test_tall_pane_recovers_full_context(self) -> None:
        q = _parse_ask_from_pane(_load_fixture(self.TALL))
        assert q is not None
        assert len(q.context) > 1000  # the full prose is back
        # Real prose content, not chrome.
        assert "internet" in q.context.lower()
        for chrome in ("❯", "☐", "Chat about this", "●"):
            assert chrome not in q.context

    def test_menu_parses_the_same_regardless_of_height(self) -> None:
        short = _parse_ask_from_pane(_load_fixture(self.SHORT))
        tall = _parse_ask_from_pane(_load_fixture(self.TALL))
        assert short is not None and tall is not None
        assert [o.label for o in short.options] == [o.label for o in tall.options]
