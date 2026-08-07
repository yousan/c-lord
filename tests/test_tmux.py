"""Tests for TmuxSessionManager (window-based architecture)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import (
    SESSION_NAME,
    TmuxSessionManager,
    _pane_in_insert_mode,
    parse_work_number,
)


class TestTmuxSessionManager:
    """Tests for the window-based TmuxSessionManager."""

    def test_create_session_new_window(self) -> None:
        """First call creates global session + new window with @thread_id."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        # Isolate from the post-create window sort (#374) — tested separately.
        mgr._sort_windows_unlocked = lambda: None  # type: ignore[method-assign]
        # Isolate from the window-size policy (#403) — tested separately.
        mgr._ensure_window_size_manual = lambda: None  # type: ignore[method-assign]
        mgr._fit_window_to_client = lambda *_: None  # type: ignore[method-assign]

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # _find_window_for_thread → _rebuild_mapping → list-windows (no session yet)
                MagicMock(returncode=1, stdout=""),
                # _ensure_session → has-session (not exists)
                MagicMock(returncode=1),
                # _ensure_session → new-session
                MagicMock(returncode=0),
                # _find_window_by_working_dir → list-windows (no windows yet)
                MagicMock(returncode=1, stdout=""),
                # new-window
                MagicMock(returncode=0),
                # set-option @thread_id
                MagicMock(returncode=0),
            ]
            name = mgr.create_session(12345, "/work/dir")

        assert name == "w1"
        assert mgr._thread_to_window[12345] == "w1"

        # Verify new-window was called correctly
        new_window_call = mock_run.call_args_list[4]
        args = new_window_call[0][0]
        assert "new-window" in args
        assert SESSION_NAME in args
        assert "w1" in args
        assert "/work/dir" in args
        # #374: created detached so a new thread doesn't steal the attached
        # user's tmux focus.
        assert "-d" in args

        # Verify set-option was called to store thread_id
        set_opt_call = mock_run.call_args_list[5]
        args = set_opt_call[0][0]
        assert "set-option" in args
        assert "@thread_id" in args
        assert "12345" in args

    def test_create_session_already_exists(self) -> None:
        """When a window already exists for the thread, re-use it."""
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False

        with patch("c_lord.tmux._run") as mock_run:
            name = mgr.create_session(12345, "/work/dir")

        assert name == "w0"
        mock_run.assert_not_called()

    def test_create_session_increments_counter(self) -> None:
        """Each new window gets an incrementing work number."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        # Isolate from the post-create window sort (#374) — tested separately.
        mgr._sort_windows_unlocked = lambda: None  # type: ignore[method-assign]
        # Isolate from the window-size policy (#403) — tested separately.
        mgr._ensure_window_size_manual = lambda: None  # type: ignore[method-assign]
        mgr._fit_window_to_client = lambda *_: None  # type: ignore[method-assign]

        with patch("c_lord.tmux._run") as mock_run:
            # First thread
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout=""),  # rebuild: list-windows
                MagicMock(returncode=0),  # has-session (exists)
                MagicMock(returncode=0, stdout=""),  # _find_window_by_working_dir: no match for "/a"
                MagicMock(returncode=0),  # new-window
                MagicMock(returncode=0),  # set-option
            ]
            name1 = mgr.create_session(111, "/a")

            # Second thread
            mock_run.side_effect = [
                # _find_window_for_thread cache miss → _rebuild_mapping
                MagicMock(returncode=0, stdout="w1\n"),  # list-windows
                MagicMock(returncode=0, stdout="111\n"),  # show-option w1
                MagicMock(returncode=0),  # has-session (exists)
                MagicMock(returncode=0, stdout="w1\t/a\n"),  # _find_window_by_working_dir: no match for "/b"
                MagicMock(returncode=0),  # new-window
                MagicMock(returncode=0),  # set-option
            ]
            name2 = mgr.create_session(222, "/b")

        assert name1 == "w1"
        assert name2 == "w2"

    def test_session_exists_true(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
            assert mgr.session_exists(12345) is True

    def test_session_exists_false(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            # rebuild: list-windows returns empty
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.session_exists(99999) is False

    def test_kill_session(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            # rebuild: list-windows returns empty
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.kill_session(99999) is False

    def test_list_sessions(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            windows = mgr.list_sessions()

        assert windows == []

    def test_cleanup_orphaned(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False

        killed = mgr.cleanup_orphaned(active_thread_ids=set())
        assert killed == 0

    def test_find_window_for_thread_cache_hit(self) -> None:
        """Cache hit with valid verification returns the window name."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work3"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
            result = mgr._find_window_for_thread(12345)

        assert result == "work3"

    def test_find_window_for_thread_stale_cache(self) -> None:
        """Stale cache entry triggers rebuild from tmux."""
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")

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
        mgr = TmuxSessionManager(mapping_path="")

        # Dispatch on the command rather than a fixed call sequence: #501 moved
        # the repairing set-option after the scan, and an ordered side_effect
        # list silently hands a set-option's reply to the next show-option.
        def fake_run(args: list[str]) -> MagicMock:
            if "list-windows" in args:
                return MagicMock(
                    returncode=0,
                    stdout=(
                        "work1\t/home/u/c-lord-sessions/999/1501841644457300038\n"
                        "work2\t/home/u/other\n"
                    ),
                )
            if "show-option" in args:
                return MagicMock(returncode=1, stdout="")  # option unset
            return MagicMock(returncode=0, stdout="")

        with patch("c_lord.tmux._run", side_effect=fake_run) as mock_run:
            mgr._rebuild_mapping()

        # Recovered from path
        assert mgr._thread_to_window == {1501841644457300038: "work1"}
        # work2 has no recoverable thread_id → not mapped
        assert 2 not in mgr._thread_to_window
        # …and the option was repaired on work1 so later lookups stay cheap.
        assert any(
            "set-option" in call.args[0] and call.args[0][-1] == "1501841644457300038"
            for call in mock_run.call_args_list
        )

    def test_rebuild_mapping_prefers_option_over_path(self) -> None:
        """When @thread_id is set, it wins over pane_current_path inference."""
        mgr = TmuxSessionManager(mapping_path="")

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
        mgr = TmuxSessionManager(mapping_path="")

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            mgr._rebuild_mapping()

        assert mgr._thread_to_window == {}
        assert mgr._next_work_id == 1

    def test_ensure_session_already_exists(self) -> None:
        """_ensure_session returns True when session already exists."""
        mgr = TmuxSessionManager(mapping_path="")
        # Isolate from the window-size policy (#403) — tested separately.
        mgr._ensure_window_size_manual = lambda: None  # type: ignore[method-assign]

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert mgr._ensure_session() is True

        # Only has-session called, no new-session
        assert mock_run.call_count == 1

    def test_ensure_session_creates_new(self) -> None:
        """_ensure_session creates session when it doesn't exist."""
        mgr = TmuxSessionManager(mapping_path="")
        # Isolate from the window-size policy (#403) — tested separately.
        mgr._ensure_window_size_manual = lambda: None  # type: ignore[method-assign]

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1),  # has-session → not exists
                MagicMock(returncode=0),  # new-session → success
            ]
            assert mgr._ensure_session() is True

    def test_ensure_session_creation_fails(self) -> None:
        """_ensure_session returns False when creation fails."""
        mgr = TmuxSessionManager(mapping_path="")

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1),  # has-session → not exists
                MagicMock(returncode=1, stderr="error"),  # new-session → fail
            ]
            assert mgr._ensure_session() is False

    def test_graceful_degrade_when_not_installed(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        with patch("c_lord.tmux._tmux_available", return_value=False):
            mgr._available = None  # Reset to trigger check
            assert mgr.session_exists(12345) is False
            assert mgr.list_sessions() == []
            assert mgr.kill_session(12345) is False

    # ── Claude execution methods ─────────────────────────────────────

    def test_start_claude_sends_command_with_prompt(self) -> None:
        """start_claude sends the claude command with the prompt as CLI arg."""
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = mgr.start_claude(99999, "hello")

        assert result is False

    def test_start_claude_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False

        assert mgr.start_claude(12345, "hello") is False

    def test_start_claude_dangerously_skip_permissions(self) -> None:
        """start_claude uses --dangerously-skip-permissions flag."""
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
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

    def test_start_claude_with_try_continue_flag(self) -> None:
        """start_claude with try_continue=True includes --continue in the command."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0),  # send-keys
            ]
            result = mgr.start_claude(12345, "hello", try_continue=True)

        assert result is True
        cmd_call = mock_run.call_args_list[1]
        args = cmd_call[0][0]
        cmd_str = " ".join(args[3:])
        assert "--continue" in cmd_str

    def test_start_claude_default_no_try_continue(self) -> None:
        """start_claude by default does NOT include --continue (fresh start)."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0),  # send-keys
            ]
            mgr.start_claude(12345, "hello")

        cmd_call = mock_run.call_args_list[1]
        args = cmd_call[0][0]
        cmd_str = " ".join(args[3:])
        assert "--continue" not in cmd_str

    def test_start_claude_includes_effort_flag(self) -> None:
        """start_claude passes a valid effort level as --effort <level>."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0),  # send-keys
            ]
            mgr.start_claude(12345, "hello", effort="max")

        cmd_call = mock_run.call_args_list[1]
        args = cmd_call[0][0]
        cmd_str = " ".join(args[3:])
        assert "--effort max" in cmd_str

    def test_start_claude_no_effort_flag_when_none(self) -> None:
        """effort=None (default) omits the --effort flag entirely."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0),  # send-keys
            ]
            mgr.start_claude(12345, "hello")

        cmd_call = mock_run.call_args_list[1]
        args = cmd_call[0][0]
        cmd_str = " ".join(args[3:])
        assert "--effort" not in cmd_str

    def test_start_claude_invalid_effort_is_skipped(self) -> None:
        """An effort the --effort flag rejects (e.g. ultracode) is dropped, not
        passed — passing it would hard-error the claude process and leave the
        thread with no response. The command is still built without it."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0),  # send-keys
            ]
            result = mgr.start_claude(12345, "hello", effort="ultracode")

        assert result is True
        cmd_call = mock_run.call_args_list[1]
        args = cmd_call[0][0]
        cmd_str = " ".join(args[3:])
        assert "--effort" not in cmd_str

    def test_send_input_sends_text_and_enter(self, monkeypatch) -> None:
        # #492: pin skill mode — this test checks general send_input mechanics,
        # not the jsonl ZWSP marker (that has its own dedicated tests below).
        monkeypatch.setenv("CLORD_BRIDGE_MODE", "skill")
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0, stdout=self._INSERT_PANE),  # capture-pane (mode)
                MagicMock(returncode=0),  # send-keys -l (text)
                MagicMock(returncode=0),  # send-keys Enter
            ]
            result = mgr.send_input(12345, "my prompt")

        assert result is True

        # Verify send-keys -l was called with the text
        text_call = mock_run.call_args_list[2]
        args = text_call[0][0]
        assert "send-keys" in args
        assert "-l" in args
        assert "my prompt" in args

        # Verify Enter was sent
        enter_call = mock_run.call_args_list[3]
        args = enter_call[0][0]
        assert "Enter" in args

    def test_send_input_prefixes_zwsp_marker_under_jsonl_mode(
        self, monkeypatch=None
    ) -> None:
        # In CLORD_BRIDGE_MODE=jsonl the input must be prefixed with a
        # zero-width-space so the resulting JSONL ``user`` event is recognised
        # as c-lord-originated and not double-posted back to Discord (#71).
        import os

        prev = os.environ.get("CLORD_BRIDGE_MODE")
        os.environ["CLORD_BRIDGE_MODE"] = "jsonl"
        try:
            mgr = TmuxSessionManager(mapping_path="")
            mgr._available = True
            mgr._thread_to_window[12345] = "work1"

            with patch("c_lord.tmux._run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="12345\n"),
                    MagicMock(returncode=0, stdout=self._INSERT_PANE),  # capture-pane (mode)
                    MagicMock(returncode=0),
                    MagicMock(returncode=0),
                ]
                assert mgr.send_input(12345, "hi") is True

            text_call = mock_run.call_args_list[2]
            args = text_call[0][0]
            # ZWSP (U+200B) is prepended to the literal text.
            assert "​hi" in args
        finally:
            if prev is None:
                os.environ.pop("CLORD_BRIDGE_MODE", None)
            else:
                os.environ["CLORD_BRIDGE_MODE"] = prev

    def test_send_input_no_marker_under_skill_mode(self) -> None:
        import os

        prev = os.environ.get("CLORD_BRIDGE_MODE")
        os.environ["CLORD_BRIDGE_MODE"] = "skill"
        try:
            mgr = TmuxSessionManager(mapping_path="")
            mgr._available = True
            mgr._thread_to_window[12345] = "work1"

            with patch("c_lord.tmux._run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="12345\n"),
                    MagicMock(returncode=0, stdout=self._INSERT_PANE),  # capture-pane (mode)
                    MagicMock(returncode=0),
                    MagicMock(returncode=0),
                ]
                assert mgr.send_input(12345, "hi") is True

            text_call = mock_run.call_args_list[2]
            args = text_call[0][0]
            assert "hi" in args
            # No ZWSP under skill mode.
            assert "​hi" not in args
        finally:
            if prev is None:
                os.environ.pop("CLORD_BRIDGE_MODE", None)
            else:
                os.environ["CLORD_BRIDGE_MODE"] = prev

    # -- #147: vim NORMAL-mode correction before literal input --------------
    #
    # Claude Code runs with ``editorMode: vim``. When the input box is in
    # NORMAL mode, ``send-keys -l`` characters are interpreted as vim commands
    # and the message is corrupted. This Claude version (v2.1.150) shows
    # ``-- INSERT`` in the status bar only in INSERT mode; NORMAL omits it
    # entirely (no ``-- NORMAL`` marker). So send_input must capture the pane,
    # and if not in INSERT, press ``i`` to enter INSERT before the literal text.

    _INSERT_PANE = (
        "❯ \n"
        "─────────────────────────────\n"
        "   Model: Opus 4.7  v2.1.150  Style: default\n"
        "   ⎇ no git  cwd: /tmp  Skill: none\n"
        "  -- INSERT -- ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    )
    _NORMAL_PANE = (
        "❯ \n"
        "─────────────────────────────\n"
        "   Model: Opus 4.7  v2.1.150  Style: default\n"
        "   ⎇ no git  cwd: /tmp  Skill: none\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    )

    def test_send_input_enters_insert_when_normal_mode(self, monkeypatch) -> None:
        """NORMAL mode → press ``i`` (key) before sending the literal text (#147)."""
        monkeypatch.setenv("CLORD_BRIDGE_MODE", "skill")
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0, stdout=self._NORMAL_PANE),  # capture-pane (mode)
                MagicMock(returncode=0),  # send-keys i
                MagicMock(returncode=0),  # send-keys -l (text)
                MagicMock(returncode=0),  # send-keys Enter
            ]
            assert mgr.send_input(12345, "melon") is True

        calls = mock_run.call_args_list
        # The bare ``i`` keypress (NON-literal) must precede the literal text.
        i_call = calls[2][0][0]
        assert i_call[:2] == ["tmux", "send-keys"]
        assert "-l" not in i_call  # ``i`` is a key, not literal
        assert i_call[-1] == "i"
        # Then the literal text.
        text_call = calls[3][0][0]
        assert "-l" in text_call and "melon" in text_call
        # Then Enter.
        assert "Enter" in calls[4][0][0]

    def test_send_input_no_extra_i_when_insert_mode(self, monkeypatch) -> None:
        """INSERT mode → no extra ``i`` injected (AC2: no regression / double-i) (#147)."""
        monkeypatch.setenv("CLORD_BRIDGE_MODE", "skill")
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0, stdout=self._INSERT_PANE),  # capture-pane (mode)
                MagicMock(returncode=0),  # send-keys -l (text)
                MagicMock(returncode=0),  # send-keys Enter
            ]
            assert mgr.send_input(12345, "melon") is True

        calls = mock_run.call_args_list
        # No bare ``i`` keypress anywhere — the text goes out literally first.
        for c in calls:
            args = c[0][0]
            if args[:2] == ["tmux", "send-keys"] and "-l" not in args and args[-1] == "i":
                raise AssertionError("unexpected bare 'i' sent while already in INSERT mode")
        # send-keys -l with the text comes right after the capture.
        text_call = calls[2][0][0]
        assert "-l" in text_call and "melon" in text_call
        assert "Enter" in calls[3][0][0]

    # -- #172: send_literal (type onto a TUI menu's free-text row) ----------
    #
    # The AskUserQuestion "Type something." row is NOT submitted as a message:
    # typing the literal text replaces the highlighted row's label, then a
    # separate Enter records it as the answer. send_literal sends raw
    # ``send-keys -l`` only — no Enter and no jsonl ZWSP marker.

    def test_send_literal_sends_text_without_enter(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0),  # send-keys -l (text)
            ]
            assert mgr.send_literal(12345, "メロン") is True

        calls = mock_run.call_args_list
        # Exactly one send-keys after the window verify: the literal text.
        assert len(calls) == 2
        text_call = calls[1][0][0]
        assert text_call[:3] == ["tmux", "send-keys", "-l"]
        assert "メロン" in text_call
        # No Enter is sent (the caller confirms separately).
        for c in calls:
            assert "Enter" not in c[0][0]

    def test_send_literal_no_zwsp_under_jsonl_mode(self) -> None:
        """send_literal must NOT prepend the jsonl ZWSP marker (#172).

        The ZWSP exists to dedup c-lord-originated *user* turns; a menu free-text
        answer is not a user turn, so a stray ZWSP would only corrupt the answer.
        """
        import os

        prev = os.environ.get("CLORD_BRIDGE_MODE")
        os.environ["CLORD_BRIDGE_MODE"] = "jsonl"
        try:
            mgr = TmuxSessionManager(mapping_path="")
            mgr._available = True
            mgr._thread_to_window[12345] = "work1"

            with patch("c_lord.tmux._run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="12345\n"),
                    MagicMock(returncode=0),
                ]
                assert mgr.send_literal(12345, "hi") is True

            text_call = mock_run.call_args_list[1][0][0]
            assert "hi" in text_call
            assert "​hi" not in text_call  # no ZWSP (U+200B) prefix
        finally:
            if prev is None:
                os.environ.pop("CLORD_BRIDGE_MODE", None)
            else:
                os.environ["CLORD_BRIDGE_MODE"] = prev

    def test_send_literal_no_window(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.send_literal(99999, "hello") is False

    def test_send_input_no_window(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.send_input(99999, "hello") is False

    def test_send_input_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False
        assert mgr.send_input(12345, "hello") is False

    def test_capture_pane_returns_text(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.capture_pane(99999) == ""

    def test_capture_pane_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False
        assert mgr.capture_pane(12345) == ""

    def test_send_interrupt_sends_ctrl_c(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.send_interrupt(99999) is False

    def test_send_interrupt_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False
        assert mgr.send_interrupt(12345) is False

    def test_is_claude_running_true(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0, stdout="claude\n"),  # list-panes
            ]
            assert mgr.is_claude_running(12345) is True

    def test_is_claude_running_false_other_command(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),  # _find: verify
                MagicMock(returncode=0, stdout="bash\n"),  # list-panes
            ]
            assert mgr.is_claude_running(12345) is False

    def test_is_claude_running_no_window(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.is_claude_running(99999) is False

    def test_is_claude_running_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False
        assert mgr.is_claude_running(12345) is False

    # ── Custom session name ──────────────────────────────────────────

    def test_default_session_name(self) -> None:
        """Default session name is the SESSION_NAME constant."""
        mgr = TmuxSessionManager(mapping_path="")
        assert mgr.session_name == SESSION_NAME

    def test_custom_session_name(self) -> None:
        """Custom session name is used."""
        mgr = TmuxSessionManager(session_name="mybot", mapping_path="")
        assert mgr.session_name == "mybot"

    def test_custom_session_name_used_in_commands(self) -> None:
        """Custom session name appears in tmux commands."""
        mgr = TmuxSessionManager(session_name="mybot", mapping_path="")
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
        mgr = TmuxSessionManager(session_name="custom", mapping_path="")
        # Isolate from the window-size policy (#403) — tested separately.
        mgr._ensure_window_size_manual = lambda: None  # type: ignore[method-assign]

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
        mgr = TmuxSessionManager(session_name=None, mapping_path="")
        assert mgr.session_name == SESSION_NAME

    # ── remap_window ────────────────────────────────────────────────

    def test_remap_window_success(self) -> None:
        """remap_window updates @thread_id and cache for an existing window."""
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with patch("c_lord.tmux._run") as mock_run:
            # list-windows succeeds but does not contain target name
            mock_run.return_value = MagicMock(returncode=0, stdout="work1\nwork2\n")
            result = mgr.remap_window(99999, "nonexistent")

        assert result is False
        assert 99999 not in mgr._thread_to_window

    def test_remap_window_updates_cache(self) -> None:
        """remap_window removes old mapping and adds new one."""
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
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
        mgr = TmuxSessionManager(mapping_path="")
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

    # ── Issue #113: Fix A — subdirectory cd + restart ────────────────

    def test_create_session_adopts_window_pane_in_subdir(self) -> None:
        """After tmux restart, adopt window even when pane has cd'd into a subdir.

        Regression for #113 / Fix-A: pane_current_path == working_dir/subdir
        must still be recognised as the same session window.
        """
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        WORKING_DIR = "/tmp/c-lord-test/mydir"
        SUBDIR = f"{WORKING_DIR}/src/components"
        THREAD_ID = 22222

        recorded_calls: list[list[str]] = []

        def fake_run(argv: list[str], *_args: object, **_kwargs: object) -> MagicMock:
            recorded_calls.append(list(argv))
            cmd = argv[1] if len(argv) > 1 else ""
            if cmd == "list-windows":
                # pane has cd'd into a subdirectory
                return MagicMock(returncode=0, stdout=f"work1\t{SUBDIR}\n")
            if cmd == "show-option":
                return MagicMock(returncode=1, stdout="")
            if cmd == "has-session":
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        with patch("c_lord.tmux._run", side_effect=fake_run):
            name = mgr.create_session(THREAD_ID, WORKING_DIR)

        assert name == "work1", f"Expected work1 (adopted), got {name}"
        assert mgr._thread_to_window[THREAD_ID] == "work1"
        new_window_calls = [c for c in recorded_calls if "new-window" in c]
        assert new_window_calls == [], f"new-window was unexpectedly called: {new_window_calls}"

    def test_create_session_does_not_adopt_unrelated_dir(self) -> None:
        """A window cd'd to a completely different directory must NOT be adopted."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        WORKING_DIR = "/tmp/c-lord-test/mydir"
        THREAD_ID = 33333

        recorded_calls: list[list[str]] = []

        def fake_run(argv: list[str], *_args: object, **_kwargs: object) -> MagicMock:
            recorded_calls.append(list(argv))
            cmd = argv[1] if len(argv) > 1 else ""
            if cmd == "list-windows":
                # pane is at a completely unrelated path
                return MagicMock(returncode=0, stdout="work1\t/home/user/other-project\n")
            if cmd == "show-option":
                return MagicMock(returncode=1, stdout="")
            if cmd == "has-session":
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        with patch("c_lord.tmux._run", side_effect=fake_run):
            name = mgr.create_session(THREAD_ID, WORKING_DIR)

        # Must create a new window, not adopt the unrelated one.
        # The pre-existing legacy ``work1`` window counts toward the sequence,
        # so the new short-prefixed window is ``w2``.
        new_window_calls = [c for c in recorded_calls if "new-window" in c]
        assert new_window_calls != [], "new-window should have been called for unrelated dir"
        assert name == "w2"

    # ── Issue #113: Fix B — persistent window mapping file ───────────

    def test_rebuild_mapping_restores_from_file(self) -> None:
        """_rebuild_mapping restores @thread_id from persistent mapping file.

        Regression for #113 / Fix-B: when @thread_id is cleared (tmux restart)
        and the pane has cd'd away from the session dir, the mapping file is the
        fallback source of truth. _rebuild_mapping must read it and repair
        @thread_id so create_session can find the window without creating a twin.
        """
        import json
        import tempfile

        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"22222": "work1"}, f)
            mapping_path = f.name

        mgr._mapping_path = mapping_path

        recorded_calls: list[list[str]] = []

        def fake_run(argv: list[str], *_args: object, **_kwargs: object) -> MagicMock:
            recorded_calls.append(list(argv))
            cmd = argv[1] if len(argv) > 1 else ""
            if cmd == "list-windows":
                # pane is at /tmp (totally different from session dir)
                return MagicMock(returncode=0, stdout="work1\t/tmp\n")
            if cmd == "show-option":
                return MagicMock(returncode=1, stdout="")
            if cmd == "set-option":
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        with patch("c_lord.tmux._run", side_effect=fake_run):
            mgr._rebuild_mapping()

        # thread_id 22222 must have been restored from file
        assert mgr._thread_to_window.get(22222) == "work1"
        set_calls = [c for c in recorded_calls if "set-option" in c and "@thread_id" in c]
        assert any("22222" in c for c in set_calls), "@thread_id not restored from file"

        import os
        os.unlink(mapping_path)

    def test_create_session_adopts_window_via_mapping_file(self) -> None:
        """create_session finds the window via mapping file when pane has cd'd away.

        Regression for #113 / Fix-B end-to-end: @thread_id cleared, pane at /tmp,
        but mapping file has the correct thread→window entry.
        """
        import json
        import tempfile

        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        WORKING_DIR = "/tmp/c-lord-test/session"
        THREAD_ID = 44444

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({str(THREAD_ID): "work1"}, f)
            mapping_path = f.name

        mgr._mapping_path = mapping_path

        recorded_calls: list[list[str]] = []

        def fake_run(argv: list[str], *_args: object, **_kwargs: object) -> MagicMock:
            recorded_calls.append(list(argv))
            cmd = argv[1] if len(argv) > 1 else ""
            if cmd == "list-windows":
                # pane at /tmp, not WORKING_DIR (cd'd away) — but file has the mapping
                return MagicMock(returncode=0, stdout="work1\t/tmp\n")
            if cmd == "show-option":
                return MagicMock(returncode=1, stdout="")
            if cmd == "has-session":
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        with patch("c_lord.tmux._run", side_effect=fake_run):
            name = mgr.create_session(THREAD_ID, WORKING_DIR)

        assert name == "work1", f"Expected work1 (found via mapping file), got {name}"
        new_window_calls = [c for c in recorded_calls if "new-window" in c]
        assert new_window_calls == [], f"new-window was called despite mapping file entry"

        import os
        os.unlink(mapping_path)

    # ── Issue #111: duplicate window prevention after tmux restart ───

    def test_create_session_adopts_window_by_dir_on_restart(self) -> None:
        """After tmux restart, create_session must adopt existing window by dir match.

        Regression for #111: when @thread_id is cleared (tmux restart) and the
        pane is still in the original working_dir, create_session must re-use
        the existing window rather than creating a duplicate (2:1 twin window).
        """
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        # Path with no 10+-digit suffix so _rebuild_mapping path-regex won't recover it.
        WORKING_DIR = "/tmp/c-lord-test/mydir"
        THREAD_ID = 12345

        recorded_calls: list[list[str]] = []

        def fake_run(argv: list[str], *_args: object, **_kwargs: object) -> MagicMock:
            recorded_calls.append(list(argv))
            cmd = argv[1] if len(argv) > 1 else ""
            if cmd == "list-windows":
                # work1 exists; pane is still at WORKING_DIR (post-restart, pane not moved)
                return MagicMock(returncode=0, stdout=f"work1\t{WORKING_DIR}\n")
            if cmd == "show-option":
                # @thread_id was cleared by tmux restart
                return MagicMock(returncode=1, stdout="")
            if cmd == "has-session":
                return MagicMock(returncode=0, stdout="")
            # new-window and set-option both succeed
            return MagicMock(returncode=0, stdout="")

        with patch("c_lord.tmux._run", side_effect=fake_run):
            name = mgr.create_session(THREAD_ID, WORKING_DIR)

        # Must adopt work1 — NOT create a new work2
        assert name == "work1"
        assert mgr._thread_to_window[THREAD_ID] == "work1"
        new_window_calls = [c for c in recorded_calls if "new-window" in c]
        assert new_window_calls == [], f"new-window was unexpectedly called: {new_window_calls}"
        # @thread_id must be re-attached to work1
        set_option_calls = [c for c in recorded_calls if "set-option" in c and "@thread_id" in c]
        assert any(str(THREAD_ID) in c for c in set_option_calls), (
            "@thread_id was not set on the adopted window"
        )


class TestWindowSizePolicy:
    """#403: pin bot session to a fixed (manual) window size so a client (the
    human's SSH terminal) resizing does not SIGWINCH-storm every idle Claude TUI
    into ghosting its status line."""

    def test_ensure_window_size_manual_sets_option(self) -> None:
        """window-size is pinned to ``manual`` on the bot session."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            mgr._ensure_window_size_manual()
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert any(
            "set-option" in a and "window-size" in a and "manual" in a for a in calls
        ), calls

    def test_ensure_window_size_manual_noop_when_unavailable(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False
        with patch("c_lord.tmux._run") as mock_run:
            mgr._ensure_window_size_manual()
        mock_run.assert_not_called()

    def test_current_client_size_parses_list_clients(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="146 38\n")
            assert mgr._current_client_size() == (146, 38)

    def test_current_client_size_none_when_no_client(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            assert mgr._current_client_size() is None

    def test_fit_window_to_client_uses_client_size(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="146 38\n"),  # list-clients
                MagicMock(returncode=0),  # resize-window
            ]
            mgr._fit_window_to_client("w1")
        resize = mock_run.call_args_list[-1].args[0]
        assert "resize-window" in resize
        assert "146" in resize and "38" in resize

    def test_fit_window_to_client_falls_back_to_default(self) -> None:
        from c_lord.tmux import DEFAULT_MANAGED_WINDOW_SIZE

        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=""),  # list-clients: none attached
                MagicMock(returncode=0),  # resize-window
            ]
            mgr._fit_window_to_client("w1")
        resize = mock_run.call_args_list[-1].args[0]
        assert str(DEFAULT_MANAGED_WINDOW_SIZE[0]) in resize
        assert str(DEFAULT_MANAGED_WINDOW_SIZE[1]) in resize

    def test_ensure_session_pins_window_size_manual(self) -> None:
        """_ensure_session wires in the manual window-size policy (#403)."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            mgr._ensure_session()
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert any(
            "set-option" in a and "window-size" in a and "manual" in a for a in calls
        ), calls


class TestPaneInsertModeDetection:
    """#147: detect vim INSERT vs NORMAL from the pane status bar.

    Claude Code (v2.1.150) shows ``-- INSERT`` in the status bar only in
    INSERT mode. NORMAL mode omits it — there is NO ``-- NORMAL`` marker —
    so detection keys on the presence of ``-- INSERT`` plus a recognisable
    status-bar anchor for the NORMAL case.
    """

    _INSERT = (
        "❯ some text\n"
        "──────────\n"
        "   Model: Opus 4.7  v2.1.150  Style: default\n"
        "  -- INSERT -- ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    )
    _NORMAL = (
        "❯ some text\n"
        "──────────\n"
        "   Model: Opus 4.7  v2.1.150  Style: default\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    )

    def test_insert_marker_present(self) -> None:
        assert _pane_in_insert_mode(self._INSERT) is True

    def test_normal_no_insert_marker(self) -> None:
        assert _pane_in_insert_mode(self._NORMAL) is False

    def test_old_insert_in_scrollback_ignored(self) -> None:
        """A stale ``-- INSERT`` far above the current status bar must not win.

        Only the bottom status zone decides the mode; an old INSERT frame in
        scrollback above a current NORMAL status bar should read as NORMAL.
        """
        stale = "  -- INSERT -- ⏵⏵ old frame\n" + ("filler\n" * 30) + self._NORMAL
        assert _pane_in_insert_mode(stale) is False

    def test_empty_or_unknown_returns_none(self) -> None:
        assert _pane_in_insert_mode("") is None
        assert _pane_in_insert_mode("just some\nresponse text\n") is None


class TestParseWorkNumber:
    """The W<N> thread-name label must track the stable work{N} window name."""

    def test_parses_short_window_number(self) -> None:
        # New short prefix (#356): windows are named w1, w2, ... not work1.
        assert parse_work_number("w1") == 1
        assert parse_work_number("w5") == 5
        assert parse_work_number("w73") == 73

    def test_parses_legacy_work_number(self) -> None:
        # Windows created before the prefix was shortened are named work{N}.
        # parse_work_number must keep recognizing them so already-running
        # windows keep their W<N> Discord label across the transition.
        assert parse_work_number("work1") == 1
        assert parse_work_number("work5") == 5
        assert parse_work_number("work42") == 42

    def test_returns_none_for_non_work_names(self) -> None:
        assert parse_work_number("zsh") is None
        assert parse_work_number("bash") is None
        assert parse_work_number("") is None

    def test_returns_none_for_non_numeric_suffix(self) -> None:
        assert parse_work_number("w") is None
        assert parse_work_number("work") is None
        assert parse_work_number("workbench") is None
        assert parse_work_number("wibble") is None


class TestWindowNameGeneration:
    """New windows use the short ``w{N}`` prefix (#356)."""

    def test_next_window_name_uses_short_prefix(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        assert mgr._next_window_name() == "w1"
        assert mgr._next_window_name() == "w2"

    def test_rebuild_continues_sequence_after_legacy_work_window(self) -> None:
        """A legacy ``work{N}`` window left over from before the rename still
        counts toward ``_next_work_id`` so the next new window is ``w{N+1}`` —
        the numbering stays monotonic across the transition (no w1 colliding
        with an existing work3)."""
        mgr = TmuxSessionManager(mapping_path="")

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # list-windows: one legacy work3 window survives
                MagicMock(returncode=0, stdout="work3\n"),
                # show-option @thread_id for work3
                MagicMock(returncode=0, stdout="333\n"),
            ]
            mgr._rebuild_mapping()

        assert mgr._thread_to_window == {333: "work3"}
        assert mgr._next_work_id == 4
        assert mgr._next_window_name() == "w4"


class TestSortWindows:
    """Windows are kept in ascending ``w{N}`` order on add (#374)."""

    @staticmethod
    def _avail_mgr() -> TmuxSessionManager:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        return mgr

    @staticmethod
    def _move_sources(mock_run: MagicMock) -> list[str]:
        """Return the ``-s`` source of each ``move-window`` call, in order."""
        sources: list[str] = []
        for c in mock_run.call_args_list:
            argv = c[0][0]
            if "move-window" in argv and "-s" in argv:
                sources.append(argv[argv.index("-s") + 1])
        return sources

    def test_orders_numbered_windows_ascending(self) -> None:
        mgr = self._avail_mgr()
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # list-windows: window_id \t name (unordered: w30, w5, w12)
                MagicMock(returncode=0, stdout="@5\tw30\n@6\tw5\n@7\tw12\n"),
                MagicMock(returncode=0),  # move w5
                MagicMock(returncode=0),  # move w12
                MagicMock(returncode=0),  # move w30
                MagicMock(returncode=0),  # move-window -r
            ]
            mgr._sort_windows_unlocked()
        # sources must be ascending by number: w5(@6), w12(@7), w30(@5)
        assert self._move_sources(mock_run) == ["@6", "@7", "@5"]
        assert any(
            "move-window" in c[0][0] and "-r" in c[0][0] for c in mock_run.call_args_list
        )

    def test_non_numbered_windows_go_to_end(self) -> None:
        mgr = self._avail_mgr()
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="@1\tzsh\n@2\tw9\n@3\tw3\n"),
                MagicMock(returncode=0),
                MagicMock(returncode=0),
                MagicMock(returncode=0),
                MagicMock(returncode=0),
            ]
            mgr._sort_windows_unlocked()
        # numbered ascending (w3@3, w9@2) then non-numbered zsh(@1) last
        assert self._move_sources(mock_run) == ["@3", "@2", "@1"]

    def test_noop_when_already_sorted(self) -> None:
        mgr = self._avail_mgr()
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="@1\tw3\n@2\tw9\n@3\tw30\n"),
            ]
            mgr._sort_windows_unlocked()
        assert not any("move-window" in c[0][0] for c in mock_run.call_args_list)

    def test_noop_single_window(self) -> None:
        mgr = self._avail_mgr()
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [MagicMock(returncode=0, stdout="@1\tw1\n")]
            mgr._sort_windows_unlocked()
        assert not any("move-window" in c[0][0] for c in mock_run.call_args_list)

    def test_noop_when_tmux_unavailable(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = False
        with patch("c_lord.tmux._run") as mock_run:
            mgr._sort_windows_unlocked()
        mock_run.assert_not_called()

    def test_only_moves_never_renames_or_kills(self) -> None:
        """Sorting must change indices/focus only — never rename, kill, or
        re-option windows (so names / @thread_id / panes are preserved)."""
        mgr = self._avail_mgr()
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="@1\tw9\n@2\tw3\n"),
                MagicMock(returncode=0),
                MagicMock(returncode=0),
                MagicMock(returncode=0),
            ]
            mgr._sort_windows_unlocked()
        verbs = {c[0][0][1] for c in mock_run.call_args_list if len(c[0][0]) > 1}
        assert verbs <= {"list-windows", "move-window", "select-window"}

    def test_restores_active_window_after_sort(self) -> None:
        """Parking/renumbering resets the session's current window; the sort
        must re-select whatever was active so an attached user's view is not
        yanked away (#374)."""
        mgr = self._avail_mgr()
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # window_id \t name \t active — @6 (w3) is the active window
                MagicMock(returncode=0, stdout="@5\tw9\t0\n@6\tw3\t1\n"),
                MagicMock(returncode=0),  # move w3
                MagicMock(returncode=0),  # move w9
                MagicMock(returncode=0),  # move-window -r
                MagicMock(returncode=0),  # select-window restore
            ]
            mgr._sort_windows_unlocked()
        select_calls = [c[0][0] for c in mock_run.call_args_list if "select-window" in c[0][0]]
        assert len(select_calls) == 1
        assert select_calls[0][-1] == "@6"  # the previously-active window

    def test_no_select_window_when_none_active(self) -> None:
        """If no window is reported active, the sort doesn't emit select-window."""
        mgr = self._avail_mgr()
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="@5\tw9\t0\n@6\tw3\t0\n"),
                MagicMock(returncode=0),
                MagicMock(returncode=0),
                MagicMock(returncode=0),
            ]
            mgr._sort_windows_unlocked()
        assert not any("select-window" in c[0][0] for c in mock_run.call_args_list)

    def test_create_session_sorts_after_new_window(self) -> None:
        """The new-window path triggers a sort; the existing-window path does not."""
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        # Isolate from the window-size policy (#403) — tested separately.
        mgr._ensure_window_size_manual = lambda: None  # type: ignore[method-assign]
        mgr._fit_window_to_client = lambda *_: None  # type: ignore[method-assign]
        called = {"n": 0}
        mgr._sort_windows_unlocked = lambda: called.__setitem__("n", called["n"] + 1)  # type: ignore[method-assign]
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout=""),  # rebuild list-windows (no session)
                MagicMock(returncode=1),  # has-session (no)
                MagicMock(returncode=0),  # new-session
                MagicMock(returncode=1, stdout=""),  # _find_window_by_working_dir
                MagicMock(returncode=0),  # new-window
                MagicMock(returncode=0),  # set-option
            ]
            mgr.create_session(12345, "/work/dir")
        assert called["n"] == 1


class TestGetWindowInfo:
    """get_window_info returns the stable work{N} number, not the volatile index."""

    def test_returns_work_number_not_window_index(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work3"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                # _find_window_for_thread cache hit → show-option verify
                MagicMock(returncode=0, stdout="12345\n"),
                # list-windows for window_id lookup — index 0 diverges from work3
                MagicMock(returncode=0, stdout="work3\t@7\n"),
            ]
            info = mgr.get_window_info(12345)

        assert info == ("@7", 3)

    def test_returns_none_work_number_for_non_work_window(self) -> None:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "adopted-window"

        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),
                MagicMock(returncode=0, stdout="adopted-window\t@2\n"),
            ]
            info = mgr.get_window_info(12345)

        assert info == ("@2", None)
