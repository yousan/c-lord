"""Tests for TmuxSessionManager (window-based architecture)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import SESSION_NAME, TmuxSessionManager


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

    def test_rebuild_mapping_recovers_from_pane_path(self) -> None:
        """When @thread_id is missing (e.g. after tmux server restart wiped
        user options), thread_id is recovered from the pane's current path.

        Regression for #69: tmux-resurrect restores window names but not
        ``@thread_id`` window options, so the lookup had to fall back to
        the working directory, which contains the thread ID by convention
        (``<base>/<thread_id>``).
        """
        mgr = TmuxSessionManager()

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # list-windows with pane_current_path
                MagicMock(
                    returncode=0,
                    stdout=(
                        "work1\t/home/u/c-lord-sessions/999/1501841644457300038\n"
                        "work2\t/home/u/other\n"
                    ),
                ),
                # show-option @thread_id for work1 → option unset (rc=1)
                MagicMock(returncode=1, stdout=""),
                # set-option to repair @thread_id for work1
                MagicMock(returncode=0),
                # show-option @thread_id for work2 → option unset (rc=1)
                MagicMock(returncode=1, stdout=""),
            ]
            mgr._rebuild_mapping()

        # Recovered from path
        assert mgr._thread_to_window == {1501841644457300038: "work1"}
        # work2 has no recoverable thread_id → not mapped
        assert 2 not in mgr._thread_to_window

    def test_rebuild_mapping_prefers_option_over_path(self) -> None:
        """When @thread_id is set, it wins over pane_current_path inference."""
        mgr = TmuxSessionManager()

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(
                    returncode=0,
                    stdout="work1\t/home/u/c-lord-sessions/999/1501841644457300038\n",
                ),
                # show-option returns the authoritative thread_id
                MagicMock(returncode=0, stdout="42\n"),
            ]
            mgr._rebuild_mapping()

        assert mgr._thread_to_window == {42: "work1"}

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

    # ── Claude execution methods ─────────────────────────────────────

    def test_start_claude_sends_command_with_prompt(self) -> None:
        """start_claude sends the claude command with the prompt as CLI arg."""
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: show-option verify
                MagicMock(returncode=0),  # send-keys for claude command
            ]
            result = mgr.start_claude(12345, "hello world", "sonnet")

        assert result is True

        # Verify the claude command was sent with prompt
        cmd_call = mock_run.call_args_list[1]
        args = cmd_call[0][0]
        assert "send-keys" in args
        assert f"{SESSION_NAME}:work1" in args
        # The command string should contain the prompt
        cmd_str = " ".join(args[3:])
        assert "claude --model sonnet" in cmd_str
        assert "hello world" in cmd_str
        assert "Enter" in args

    def test_start_claude_no_window_returns_false(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = mgr.start_claude(99999, "hello")

        assert result is False

    def test_start_claude_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = False

        assert mgr.start_claude(12345, "hello") is False

    def test_start_claude_dangerously_skip_permissions(self) -> None:
        """start_claude uses --dangerously-skip-permissions flag."""
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0),  # send-keys for claude command
            ]
            mgr.start_claude(12345, "hello", dangerously_skip_permissions=True)

        cmd_call = mock_run.call_args_list[1]
        args = cmd_call[0][0]
        cmd_str = " ".join(args[3:])  # everything after -t
        assert "--dangerously-skip-permissions" in cmd_str

    def test_start_claude_escapes_single_quotes(self) -> None:
        """Prompt with single quotes is properly escaped."""
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),
                MagicMock(returncode=0),
            ]
            mgr.start_claude(12345, "it's a test")

        cmd_call = mock_run.call_args_list[1]
        args = cmd_call[0][0]
        cmd_str = " ".join(args[3:])
        # Single quote should be escaped
        assert "'" in cmd_str

    def test_send_input_sends_text_and_enter(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0),  # send-keys -l (text)
                MagicMock(returncode=0),  # send-keys Enter
            ]
            result = mgr.send_input(12345, "my prompt")

        assert result is True

        # Verify send-keys -l was called with the text
        text_call = mock_run.call_args_list[1]
        args = text_call[0][0]
        assert "send-keys" in args
        assert "-l" in args
        assert "my prompt" in args

        # Verify Enter was sent
        enter_call = mock_run.call_args_list[2]
        args = enter_call[0][0]
        assert "Enter" in args

    def test_send_input_no_window(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.send_input(99999, "hello") is False

    def test_send_input_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = False
        assert mgr.send_input(12345, "hello") is False

    def test_capture_pane_returns_text(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0, stdout="Hello from Claude\n"),  # capture-pane
            ]
            text = mgr.capture_pane(12345)

        assert text == "Hello from Claude\n"

        # Verify capture-pane command
        cap_call = mock_run.call_args_list[1]
        args = cap_call[0][0]
        assert "capture-pane" in args
        assert "-p" in args
        assert f"{SESSION_NAME}:work1" in args
        assert "-S" in args

    def test_capture_pane_custom_history(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find
                MagicMock(returncode=0, stdout="text\n"),  # capture-pane
            ]
            mgr.capture_pane(12345, history_lines=100)

        cap_call = mock_run.call_args_list[1]
        args = cap_call[0][0]
        assert "-100" in args

    def test_capture_pane_no_window(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.capture_pane(99999) == ""

    def test_capture_pane_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = False
        assert mgr.capture_pane(12345) == ""

    def test_send_interrupt_sends_ctrl_c(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0),  # send-keys C-c
            ]
            result = mgr.send_interrupt(12345)

        assert result is True

        int_call = mock_run.call_args_list[1]
        args = int_call[0][0]
        assert "send-keys" in args
        assert "C-c" in args

    def test_send_interrupt_no_window(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.send_interrupt(99999) is False

    def test_send_interrupt_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = False
        assert mgr.send_interrupt(12345) is False

    def test_is_claude_running_true(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0, stdout="claude\n"),  # list-panes
            ]
            assert mgr.is_claude_running(12345) is True

    def test_is_claude_running_false_other_command(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0, stdout="bash\n"),  # list-panes
            ]
            assert mgr.is_claude_running(12345) is False

    def test_is_claude_running_no_window(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.is_claude_running(99999) is False

    def test_is_claude_running_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager()
        mgr._available = False
        assert mgr.is_claude_running(12345) is False

    # ── Custom session name ──────────────────────────────────────────

    def test_default_session_name(self) -> None:
        """Default session name is the SESSION_NAME constant."""
        mgr = TmuxSessionManager()
        assert mgr.session_name == SESSION_NAME

    def test_custom_session_name(self) -> None:
        """Custom session name is used."""
        mgr = TmuxSessionManager(session_name="mybot")
        assert mgr.session_name == "mybot"

    def test_custom_session_name_used_in_commands(self) -> None:
        """Custom session name appears in tmux commands."""
        mgr = TmuxSessionManager(session_name="mybot")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: show-option verify
                MagicMock(returncode=0),  # send-keys
            ]
            mgr.start_claude(12345, "hello", "sonnet")

        # Verify the custom session name is used in the target
        cmd_call = mock_run.call_args_list[0]
        args = cmd_call[0][0]
        assert "mybot:work1" in args

    def test_custom_session_name_in_ensure_session(self) -> None:
        """Custom session name is used when creating the tmux session."""
        mgr = TmuxSessionManager(session_name="custom")

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1),  # has-session → not exists
                MagicMock(returncode=0),  # new-session → success
            ]
            assert mgr._ensure_session() is True

        # Verify has-session used custom name
        has_call = mock_run.call_args_list[0]
        assert "custom" in has_call[0][0]

        # Verify new-session used custom name
        new_call = mock_run.call_args_list[1]
        assert "custom" in new_call[0][0]

    def test_none_session_name_uses_default(self) -> None:
        """Passing None as session_name uses the default."""
        mgr = TmuxSessionManager(session_name=None)
        assert mgr.session_name == SESSION_NAME

    # ── remap_window ────────────────────────────────────────────────

    def test_remap_window_success(self) -> None:
        """remap_window updates @thread_id and cache for an existing window."""
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # list-windows → window exists
                MagicMock(returncode=0, stdout="work1\n"),
                # set-option @thread_id
                MagicMock(returncode=0),
            ]
            result = mgr.remap_window(99999, "work1")

        assert result is True
        assert mgr._thread_to_window[99999] == "work1"

        # Verify set-option was called with new thread_id
        set_call = mock_run.call_args_list[1]
        args = set_call[0][0]
        assert "set-option" in args
        assert "@thread_id" in args
        assert "99999" in args

    def test_remap_window_not_found(self) -> None:
        """remap_window returns False when the window does not exist."""
        mgr = TmuxSessionManager()
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            # list-windows succeeds but does not contain target name
            mock_run.return_value = MagicMock(returncode=0, stdout="work1\nwork2\n")
            result = mgr.remap_window(99999, "nonexistent")

        assert result is False
        assert 99999 not in mgr._thread_to_window

    def test_remap_window_updates_cache(self) -> None:
        """remap_window removes old mapping and adds new one."""
        mgr = TmuxSessionManager()
        mgr._available = True
        # Pre-existing mapping: thread 11111 → work1
        mgr._thread_to_window[11111] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="work1\n"),  # list-windows → exists
                MagicMock(returncode=0),  # set-option
            ]
            result = mgr.remap_window(22222, "work1")

        assert result is True
        # Old thread removed from cache
        assert 11111 not in mgr._thread_to_window
        # New thread mapped
        assert mgr._thread_to_window[22222] == "work1"

    def test_remap_window_tmux_unavailable(self) -> None:
        """remap_window returns False when tmux is not installed."""
        mgr = TmuxSessionManager()
        mgr._available = False

        result = mgr.remap_window(12345, "work1")

        assert result is False

    def test_remap_window_manually_created(self) -> None:
        """remap_window succeeds for a window with no pre-existing @thread_id.

        Regression for issue #37: windows created manually via ``tmux new-window``
        do not have the ``@thread_id`` option set, so ``show-option -w @thread_id``
        returns rc=1 ("no such option"). Existence must instead be checked via
        ``list-windows``.
        """
        mgr = TmuxSessionManager()
        mgr._available = True

        def fake_run(argv: list[str], *args, **kwargs) -> MagicMock:
            # show-option for @thread_id on a manually-created window → rc=1
            if "show-option" in argv and "@thread_id" in argv:
                return MagicMock(returncode=1, stdout="")
            # list-windows → window exists in session
            if "list-windows" in argv:
                return MagicMock(returncode=0, stdout="work1\nproj34\nwork2\n")
            # set-option succeeds
            if "set-option" in argv:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        with patch("c_lord.tmux._run", side_effect=fake_run):
            result = mgr.remap_window(77777, "proj34")

        assert result is True
        assert mgr._thread_to_window[77777] == "proj34"

    def test_remap_window_truly_missing(self) -> None:
        """remap_window returns False when window genuinely does not exist."""
        mgr = TmuxSessionManager()
        mgr._available = True

        def fake_run(argv: list[str], *args, **kwargs) -> MagicMock:
            if "show-option" in argv:
                return MagicMock(returncode=1, stdout="")
            if "list-windows" in argv:
                # session listing does NOT contain target name
                return MagicMock(returncode=0, stdout="work1\nwork2\n")
            return MagicMock(returncode=0, stdout="")

        with patch("c_lord.tmux._run", side_effect=fake_run):
            result = mgr.remap_window(77777, "proj34")

        assert result is False
        assert 77777 not in mgr._thread_to_window
