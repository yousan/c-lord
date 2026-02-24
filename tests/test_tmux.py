"""Tests for TmuxSessionManager."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from c_lord.tmux import SESSION_PREFIX, TmuxSessionManager


class TestTmuxSessionManager:
    def test_session_name_format(self) -> None:
        mgr = TmuxSessionManager()
        assert mgr._session_name(12345) == "clord-12345"

    def test_create_session(self) -> None:
        mgr = TmuxSessionManager()
        with patch("c_lord.tmux._tmux_available", return_value=True):
            mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            # has-session returns 1 (not exists), new-session returns 0
            mock_run.side_effect = [
                MagicMock(returncode=1),  # has-session
                MagicMock(returncode=0),  # new-session
            ]
            name = mgr.create_session(12345, "/work/dir")

        assert name == "clord-12345"
        # Verify new-session was called with correct args
        new_session_call = mock_run.call_args_list[1]
        args = new_session_call[0][0]
        assert "new-session" in args
        assert "-d" in args
        assert "-s" in args
        assert "clord-12345" in args
        assert "-c" in args
        assert "/work/dir" in args

    def test_create_session_already_exists(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            # has-session returns 0 (exists)
            mock_run.return_value = MagicMock(returncode=0)
            name = mgr.create_session(12345, "/work/dir")

        assert name == "clord-12345"
        # Only has-session should be called, not new-session
        assert mock_run.call_count == 1

    def test_create_session_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = False

        with patch("c_lord.tmux._run") as mock_run:
            name = mgr.create_session(12345, "/work/dir")

        assert name == "clord-12345"
        mock_run.assert_not_called()

    def test_session_exists(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert mgr.session_exists(12345) is True

            mock_run.return_value = MagicMock(returncode=1)
            assert mgr.session_exists(99999) is False

    def test_kill_session(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert mgr.kill_session(12345) is True

        args = mock_run.call_args[0][0]
        assert "kill-session" in args
        assert "clord-12345" in args

    def test_kill_session_not_found(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert mgr.kill_session(99999) is False

    def test_list_sessions(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="clord-111:/work/a\nclord-222:/work/b\nother-session:/tmp\n",
            )
            sessions = mgr.list_sessions()

        assert len(sessions) == 2
        assert sessions[0]["name"] == "clord-111"
        assert sessions[0]["working_dir"] == "/work/a"
        assert sessions[1]["name"] == "clord-222"

    def test_list_sessions_empty(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            sessions = mgr.list_sessions()

        assert sessions == []

    def test_cleanup_orphaned(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            # list-sessions, then kill-session for each orphan
            mock_run.side_effect = [
                MagicMock(
                    returncode=0,
                    stdout="clord-111:/a\nclord-222:/b\nclord-333:/c\n",
                ),
                MagicMock(returncode=0),  # kill 111
                MagicMock(returncode=0),  # kill 333
            ]
            # 222 is active, so 111 and 333 should be killed
            killed = mgr.cleanup_orphaned(active_thread_ids={222})

        assert killed == 2

    def test_cleanup_orphaned_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = False

        killed = mgr.cleanup_orphaned(active_thread_ids=set())
        assert killed == 0

    def test_graceful_degrade_when_not_installed(self) -> None:
        mgr = TmuxSessionManager()
        with patch("c_lord.tmux._tmux_available", return_value=False):
            mgr._available = None  # Reset to trigger check
            assert mgr.session_exists(12345) is False
            assert mgr.list_sessions() == []
            assert mgr.kill_session(12345) is False
