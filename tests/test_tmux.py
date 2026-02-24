"""Tests for TmuxSessionManager (window-based architecture)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import SESSION_NAME, WINDOW_PREFIX, TmuxSessionManager


class TestTmuxSessionManager:
    """Tests for the window-based TmuxSessionManager."""

    def test_create_session_new_window(self) -> None:
        """First call creates global session + new window with @thread_id."""
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # _find_window_for_thread → _rebuild_mapping → list-windows (no session yet)
                MagicMock(returncode=1, stdout=""),
                # _ensure_session → has-session (not exists)
                MagicMock(returncode=1),
                # _ensure_session → new-session
                MagicMock(returncode=0),
                # new-window
                MagicMock(returncode=0),
                # set-option @thread_id
                MagicMock(returncode=0),
            ]
            name = mgr.create_session(12345, "/work/dir")

        assert name == "work1"
        assert mgr._thread_to_window[12345] == "work1"

        # Verify new-window was called correctly
        new_window_call = mock_run.call_args_list[3]
        args = new_window_call[0][0]
        assert "new-window" in args
        assert SESSION_NAME in args
        assert "work1" in args
        assert "/work/dir" in args

        # Verify set-option was called to store thread_id
        set_opt_call = mock_run.call_args_list[4]
        args = set_opt_call[0][0]
        assert "set-option" in args
        assert "@thread_id" in args
        assert "12345" in args

    def test_create_session_already_exists(self) -> None:
        """When a window already exists for the thread, re-use it."""
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            # Cache hit → show-option to verify
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
            name = mgr.create_session(12345, "/work/dir")

        assert name == "work1"
        # Only the show-option verification call should be made
        assert mock_run.call_count == 1

    def test_create_session_tmux_unavailable(self) -> None:
        """When tmux is not installed, return a fallback name."""
        mgr = TmuxSessionManager()
        mgr._available = False

        with patch("c_lord.tmux._run") as mock_run:
            name = mgr.create_session(12345, "/work/dir")

        assert name == "work0"
        mock_run.assert_not_called()

    def test_create_session_increments_counter(self) -> None:
        """Each new window gets an incrementing work number."""
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            # First thread
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout=""),  # rebuild: list-windows
                MagicMock(returncode=0),  # has-session (exists)
                MagicMock(returncode=0),  # new-window
                MagicMock(returncode=0),  # set-option
            ]
            name1 = mgr.create_session(111, "/a")

            # Second thread
            mock_run.side_effect = [
                # _find_window_for_thread cache miss → _rebuild_mapping
                MagicMock(returncode=0, stdout="work1\n"),  # list-windows
                MagicMock(returncode=0, stdout="111\n"),  # show-option work1
                MagicMock(returncode=0),  # has-session (exists)
                MagicMock(returncode=0),  # new-window
                MagicMock(returncode=0),  # set-option
            ]
            name2 = mgr.create_session(222, "/b")

        assert name1 == "work1"
        assert name2 == "work2"

    def test_session_exists_true(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
            assert mgr.session_exists(12345) is True

    def test_session_exists_false(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            # rebuild: list-windows returns empty
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.session_exists(99999) is False

    def test_kill_session(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # show-option verify
                MagicMock(returncode=0),  # kill-window
            ]
            assert mgr.kill_session(12345) is True

        # Cache should be cleared
        assert 12345 not in mgr._thread_to_window

        # Verify kill-window was called
        kill_call = mock_run.call_args_list[1]
        args = kill_call[0][0]
        assert "kill-window" in args
        assert f"{SESSION_NAME}:work1" in args

    def test_kill_session_not_found(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            # rebuild: list-windows returns empty
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.kill_session(99999) is False

    def test_list_sessions(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # list-windows
                MagicMock(
                    returncode=0,
                    stdout="work1:/work/a\nwork2:/work/b\n",
                ),
                # show-option for work1
                MagicMock(returncode=0, stdout="111\n"),
                # show-option for work2
                MagicMock(returncode=0, stdout="222\n"),
            ]
            windows = mgr.list_sessions()

        assert len(windows) == 2
        assert windows[0]["window_name"] == "work1"
        assert windows[0]["working_dir"] == "/work/a"
        assert windows[0]["thread_id"] == "111"
        assert windows[1]["window_name"] == "work2"
        assert windows[1]["working_dir"] == "/work/b"
        assert windows[1]["thread_id"] == "222"

    def test_list_sessions_empty(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            windows = mgr.list_sessions()

        assert windows == []

    def test_cleanup_orphaned(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # list_sessions: list-windows
                MagicMock(
                    returncode=0,
                    stdout="work1:/a\nwork2:/b\nwork3:/c\n",
                ),
                # show-option for work1
                MagicMock(returncode=0, stdout="111\n"),
                # show-option for work2
                MagicMock(returncode=0, stdout="222\n"),
                # show-option for work3
                MagicMock(returncode=0, stdout="333\n"),
                # kill_session(111): _find → cache miss → rebuild list-windows
                MagicMock(returncode=0, stdout="work1:/a\nwork2:/b\nwork3:/c\n"),
                MagicMock(returncode=0, stdout="111\n"),  # show-option work1
                MagicMock(returncode=0, stdout="222\n"),  # show-option work2
                MagicMock(returncode=0, stdout="333\n"),  # show-option work3
                MagicMock(returncode=0),  # kill-window work1
                # kill_session(333): _find → cache hit → show-option verify
                MagicMock(returncode=0, stdout="333\n"),
                MagicMock(returncode=0),  # kill-window work3
            ]
            killed = mgr.cleanup_orphaned(active_thread_ids={222})

        assert killed == 2

    def test_cleanup_orphaned_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = False

        killed = mgr.cleanup_orphaned(active_thread_ids=set())
        assert killed == 0

    def test_find_window_for_thread_cache_hit(self) -> None:
        """Cache hit with valid verification returns the window name."""
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work3"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
            result = mgr._find_window_for_thread(12345)

        assert result == "work3"

    def test_find_window_for_thread_stale_cache(self) -> None:
        """Stale cache entry triggers rebuild from tmux."""
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # show-option verify → wrong thread (stale)
                MagicMock(returncode=0, stdout="99999\n"),
                # rebuild: list-windows
                MagicMock(returncode=0, stdout="work2\n"),
                # rebuild: show-option for work2
                MagicMock(returncode=0, stdout="12345\n"),
            ]
            result = mgr._find_window_for_thread(12345)

        assert result == "work2"
        assert mgr._thread_to_window[12345] == "work2"

    def test_rebuild_mapping(self) -> None:
        """_rebuild_mapping populates _thread_to_window and updates _next_work_id."""
        mgr = TmuxSessionManager()

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # list-windows
                MagicMock(returncode=0, stdout="work1\nwork3\n"),
                # show-option work1
                MagicMock(returncode=0, stdout="111\n"),
                # show-option work3
                MagicMock(returncode=0, stdout="333\n"),
            ]
            mgr._rebuild_mapping()

        assert mgr._thread_to_window == {111: "work1", 333: "work3"}
        assert mgr._next_work_id == 4  # one past highest (3)

    def test_rebuild_mapping_empty(self) -> None:
        """_rebuild_mapping with no windows results in empty state."""
        mgr = TmuxSessionManager()

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            mgr._rebuild_mapping()

        assert mgr._thread_to_window == {}
        assert mgr._next_work_id == 1

    def test_ensure_session_already_exists(self) -> None:
        """_ensure_session returns True when session already exists."""
        mgr = TmuxSessionManager()

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert mgr._ensure_session() is True

        # Only has-session called, no new-session
        assert mock_run.call_count == 1

    def test_ensure_session_creates_new(self) -> None:
        """_ensure_session creates session when it doesn't exist."""
        mgr = TmuxSessionManager()

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1),  # has-session → not exists
                MagicMock(returncode=0),  # new-session → success
            ]
            assert mgr._ensure_session() is True

    def test_ensure_session_creation_fails(self) -> None:
        """_ensure_session returns False when creation fails."""
        mgr = TmuxSessionManager()

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1),  # has-session → not exists
                MagicMock(returncode=1, stderr="error"),  # new-session → fail
            ]
            assert mgr._ensure_session() is False

    def test_graceful_degrade_when_not_installed(self) -> None:
        mgr = TmuxSessionManager()
        with patch("c_lord.tmux._tmux_available", return_value=False):
            mgr._available = None  # Reset to trigger check
            assert mgr.session_exists(12345) is False
            assert mgr.list_sessions() == []
            assert mgr.kill_session(12345) is False
