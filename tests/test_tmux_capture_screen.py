"""Tests for TmuxSessionManager.capture_screen (tmux screenshot, #285).

Kept in a dedicated module so the screenshot work doesn't have to touch the
large, pre-existing test_tmux.py (which carries lint debt the pre-commit hook
would flag on any edit).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import DEFAULT_SCREENSHOT_ROWS, SESSION_NAME, TmuxSessionManager


def _mgr(**kwargs: object) -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="", **kwargs)  # type: ignore[arg-type]
    mgr._available = True
    # Isolate from window-resolution _run calls — tested elsewhere.
    mgr._find_window_for_thread = lambda tid: "work1"  # type: ignore[method-assign]
    return mgr


class TestCaptureScreen:
    def test_grows_window_then_captures_visible_screen(self) -> None:
        """By default capture_screen transiently grows the window (so Claude
        redraws more conversation history), captures the taller *visible* screen
        with ANSI escapes preserved, then restores the exact original size.

        The capture stays visible-region-only — no -S (scrollback) and no -J
        (wrapped-line join) — so the PNG reproduces the grown on-screen layout.
        """
        mgr = _mgr()
        assert mgr.screenshot_rows > 40  # default is taller than the live window

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="160 40\n"),  # display-message: size
                MagicMock(returncode=0),  # resize-window UP
                MagicMock(returncode=0, stdout="\x1b[31mRED\x1b[0m\n"),  # capture (tall)
                MagicMock(returncode=0),  # resize-window RESTORE
            ]
            text = mgr.capture_screen(12345, settle_timeout=0.0)

        assert text == "\x1b[31mRED\x1b[0m\n"

        # Grow → capture → restore, in that order.
        order = [
            ("resize" if "resize-window" in c.args[0] else "capture")
            for c in mock_run.call_args_list
            if "resize-window" in c.args[0] or "capture-pane" in c.args[0]
        ]
        assert order == ["resize", "capture", "resize"]

        resize_calls = [c.args[0] for c in mock_run.call_args_list if "resize-window" in c.args[0]]
        assert str(mgr.screenshot_rows) in resize_calls[0]  # grow to the configured height
        assert resize_calls[-1][-1] == "40"  # restore the original height

        cap_args = [c.args[0] for c in mock_run.call_args_list if "capture-pane" in c.args[0]][0]
        assert "-e" in cap_args  # preserve ANSI colors/hyperlinks
        assert "-p" in cap_args
        assert f"{SESSION_NAME}:work1" in cap_args
        assert "-S" not in cap_args  # exact visible screen, not a scrollback dump
        assert "-J" not in cap_args  # no wrapped-line joining

    def test_rows_zero_disables_growth(self) -> None:
        """rows=0 captures the current window as-is (no resize round-trip)."""
        mgr = _mgr()

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="\x1b[31mRED\x1b[0m\n")
            text = mgr.capture_screen(12345, rows=0)

        assert text == "\x1b[31mRED\x1b[0m\n"
        assert all("resize-window" not in c.args[0] for c in mock_run.call_args_list)
        assert all("display-message" not in c.args[0] for c in mock_run.call_args_list)
        cap_args = mock_run.call_args_list[0].args[0]
        assert "capture-pane" in cap_args
        assert "-S" not in cap_args
        assert "-J" not in cap_args

    def test_growth_skipped_when_window_already_tall(self) -> None:
        """No resize when the live window is already at/above the target — just
        a plain visible capture (a resize round-trip would gain nothing)."""
        mgr = _mgr(screenshot_rows=80)

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="160 120\n"),  # size: 120 >= 80
                MagicMock(returncode=0, stdout="\x1b[31mRED\x1b[0m\n"),  # plain capture
            ]
            text = mgr.capture_screen(12345, settle_timeout=0.0)

        assert text == "\x1b[31mRED\x1b[0m\n"
        assert all("resize-window" not in c.args[0] for c in mock_run.call_args_list)
        cap_args = [c.args[0] for c in mock_run.call_args_list if "capture-pane" in c.args[0]][0]
        assert "-S" not in cap_args
        assert "-J" not in cap_args

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
        mgr = _mgr()

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.capture_screen(12345, rows=0) == ""


class TestScreenshotRowsConfig:
    def test_default_is_taller_than_live_window(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("CLORD_TMUX_SCREENSHOT_ROWS", raising=False)
        mgr = TmuxSessionManager(mapping_path="")
        assert mgr.screenshot_rows == DEFAULT_SCREENSHOT_ROWS
        assert DEFAULT_SCREENSHOT_ROWS > 40  # taller than DEFAULT_MANAGED_WINDOW_SIZE height

    def test_env_override(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("CLORD_TMUX_SCREENSHOT_ROWS", "150")
        mgr = TmuxSessionManager(mapping_path="")
        assert mgr.screenshot_rows == 150

    def test_env_zero_disables_growth(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("CLORD_TMUX_SCREENSHOT_ROWS", "0")
        mgr = TmuxSessionManager(mapping_path="")
        assert mgr.screenshot_rows == 0

    def test_constructor_override_wins(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("CLORD_TMUX_SCREENSHOT_ROWS", "150")
        mgr = TmuxSessionManager(mapping_path="", screenshot_rows=200)
        assert mgr.screenshot_rows == 200

    def test_invalid_env_falls_back_to_default(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("CLORD_TMUX_SCREENSHOT_ROWS", "not-a-number")
        mgr = TmuxSessionManager(mapping_path="")
        assert mgr.screenshot_rows == DEFAULT_SCREENSHOT_ROWS

    def test_negative_env_falls_back_to_default(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("CLORD_TMUX_SCREENSHOT_ROWS", "-5")
        mgr = TmuxSessionManager(mapping_path="")
        assert mgr.screenshot_rows == DEFAULT_SCREENSHOT_ROWS


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
