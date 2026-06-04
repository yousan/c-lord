"""Tests for TmuxSessionManager.capture_screen (tmux screenshot, #285).

Kept in a dedicated module so the screenshot work doesn't have to touch the
large, pre-existing test_tmux.py (which carries lint debt the pre-commit hook
would flag on any edit).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import SESSION_NAME, TmuxSessionManager


class TestCaptureScreen:
    def test_visible_region_only(self) -> None:
        """capture_screen grabs the visible region with ANSI escapes preserved."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0, stdout="\x1b[31mRED\x1b[0m\n"),  # capture-pane
            ]
            text = mgr.capture_screen(12345)

        assert text == "\x1b[31mRED\x1b[0m\n"

        cap_args = mock_run.call_args_list[1][0][0]
        assert "capture-pane" in cap_args
        assert "-e" in cap_args  # preserve ANSI colors/hyperlinks
        assert "-p" in cap_args
        assert f"{SESSION_NAME}:work1" in cap_args
        # Current screen = visible region only, exact on-screen layout.
        assert "-S" not in cap_args  # no scrollback
        assert "-J" not in cap_args  # no wrapped-line joining

    def test_no_window_returns_empty(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.capture_screen(99999) == ""

    def test_tmux_unavailable_returns_empty(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False
        assert mgr.capture_screen(12345) == ""

    def test_capture_failure_returns_empty(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=1, stdout=""),  # capture-pane fails
            ]
            assert mgr.capture_screen(12345) == ""


class TestListWindowTabs:
    def test_parses_index_name_active(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="1\tzsh\t0\n5\twork4\t0\n7\twork6\t1\n"
            )
            tabs = mgr.list_window_tabs()

        assert tabs == [(1, "zsh", False), (5, "work4", False), (7, "work6", True)]
        args = mock_run.call_args[0][0]
        assert "list-windows" in args
        assert SESSION_NAME in args

    def test_unavailable_returns_empty(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False
        assert mgr.list_window_tabs() == []

    def test_failure_returns_empty(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.list_window_tabs() == []
