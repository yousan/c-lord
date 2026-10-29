"""Window lifecycle and reaper decisions in ``c_lord/tmux.py`` that had no test.

Every scenario here comes from an incident or spec that motivated the code:

* #374 / #649 — ``create_session`` re-checks under the per-session lock, so two
  managers racing on one thread never make two windows (two Claudes on one
  checkout).
* #501 — two windows claiming one ``@thread_id`` after a tmux-resurrect restore;
  without positive "claude is running" evidence the existing binding is kept and
  no tag is stripped.
* #649 — names are not unique; lookups resolve to ``window_id`` and a stale id
  reports "no window" instead of a sibling's.
* #427 / #570 / #677 / #790 — the reaper sweeps every session, and one session
  failing must not stop the sweep (``docs/specs/tmux-window-reaping.md`` →
  エラー時「1つのセッションの掃除に失敗しても、他のセッションの掃除は続けます」).
* #576 / #742 — the resident cap and the eviction guard read panes host-wide and
  only count panes *positively* running claude.

No real tmux server is touched: ``_run`` is replaced in every test.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import (
    TmuxSessionManager,
    cleanup_orphaned_all_sessions,
    resident_thread_ids,
    running_thread_ids,
)


def _ok(stdout: str = "") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "") -> MagicMock:
    return MagicMock(returncode=1, stdout="", stderr=stderr)


def _mgr(session_name: str = "c-lord") -> TmuxSessionManager:
    mgr = TmuxSessionManager(session_name=session_name, mapping_path="")
    mgr._available = True
    return mgr


def _argvs(mock_run: MagicMock) -> list[list[str]]:
    return [c.args[0] for c in mock_run.call_args_list]


# ── create_session ────────────────────────────────────────────────────────


class TestCreateSessionGuards:
    def test_recheck_under_lock_reuses_a_window_a_racing_manager_just_made(self) -> None:
        """#374/#649: the second lookup, taken under the session lock, wins.

        The racing callers are *separate* managers pointing at one tmux session.
        If the thread's window appeared while we queued for the lock, creating
        another would put two Claude processes on one checkout.
        """
        mgr = _mgr()
        lookups = iter([None, "@42"])
        with (
            patch.object(mgr, "_find_window_for_thread", side_effect=lambda _t: next(lookups)),
            patch.object(mgr, "_ensure_session", return_value=True),
            patch.object(mgr, "_window_name", return_value="w42"),
            patch("c_lord.tmux._run", return_value=_ok()) as mock_run,
        ):
            name = mgr.create_session(1555395291811942420, "/sessions/ch/thr")

        assert name == "w42"
        assert not any("new-window" in a for a in _argvs(mock_run)), (
            "a window created by the racing manager must be reused, not duplicated"
        )

    def test_session_that_cannot_be_ensured_creates_no_window(self) -> None:
        """No session → no ``new-window`` aimed at a session that is not there."""
        mgr = _mgr()
        with (
            patch.object(mgr, "_find_window_for_thread", return_value=None),
            patch.object(mgr, "_ensure_session", return_value=False),
            patch("c_lord.tmux._run", return_value=_ok()) as mock_run,
        ):
            name = mgr.create_session(1, "/sessions/ch/thr")

        assert name == "w0"
        assert not any("new-window" in a for a in _argvs(mock_run))

    def test_failed_new_window_is_not_recorded_as_the_threads_window(self) -> None:
        """A refused ``new-window`` must not leave a mapping to a window that never existed.

        The name is still returned (the documented return type is the name), but
        nothing is tagged, mapped or saved — the next lookup finds no window and
        the runner reports it instead of typing into somebody else's pane (#649).
        """
        mgr = _mgr()

        def run(argv: list[str]) -> MagicMock:
            if "new-window" in argv:
                return _fail("create window failed: index 3 in use")
            return _ok()

        with (
            patch.object(mgr, "_find_window_for_thread", return_value=None),
            patch.object(mgr, "_ensure_session", return_value=True),
            patch.object(mgr, "_find_window_by_working_dir", return_value=None),
            patch.object(mgr, "_adopt_window_from_other_session", return_value=None),
            patch.object(mgr, "_next_window_name", return_value="w7"),
            patch.object(mgr, "_save_mapping") as save,
            patch.object(mgr, "_tag_window") as tag,
            patch("c_lord.tmux._run", side_effect=run),
        ):
            name = mgr.create_session(7, "/sessions/ch/thr")

        assert name == "w7"
        assert 7 not in mgr._thread_to_window
        save.assert_not_called()
        tag.assert_not_called()


# ── duplicate @thread_id resolution (#501) ────────────────────────────────


class TestResolveClaimWithoutClaudeEvidence:
    def test_existing_binding_wins_and_no_tag_is_stripped(self) -> None:
        """#501: resurrect restored ``work2`` and ``w8`` onto one thread's dir.

        The fix prefers the window running claude. When *neither* is (both sit
        at a shell, e.g. right after a reboot before Claude was restarted), the
        window the map already pointed at is kept for continuity — and nothing is
        un-tagged, because only positive "claude runs here" evidence may strip a
        window's tag.
        """
        mgr = _mgr("claude_base")
        thread_id = 1515873566166483074
        mgr._thread_to_window[thread_id] = "@8"

        def run(argv: list[str]) -> MagicMock:
            if "list-panes" in argv:
                return _ok("zsh\n")
            return _ok()

        with patch("c_lord.tmux._run", side_effect=run) as mock_run:
            winner = mgr._resolve_claim(
                thread_id,
                ["@1", "@8"],
                clear_losers=True,
                names={"@1": "work2", "@8": "w8"},
            )

        assert winner == "@8", "index order is not a signal; the existing binding is"
        assert not any("-uw" in a for a in _argvs(mock_run)), (
            "without a live claude there is no evidence to un-tag a window"
        )

    def test_failed_pane_probe_is_not_claude_evidence(self) -> None:
        """A ``list-panes`` error must not crown a window as "running claude"."""
        mgr = _mgr("claude_base")
        with patch("c_lord.tmux._run", return_value=_fail("can't find window")):
            assert mgr._window_has_claude("@1") is False


# ── #649 lookups by id ───────────────────────────────────────────────────


class TestLookupsById:
    def test_get_window_info_reports_stable_work_number(self) -> None:
        """``W<N>`` comes from the window *name*, not the volatile index."""
        mgr = _mgr()
        with (
            patch.object(mgr, "_find_window_for_thread", return_value="@218"),
            patch("c_lord.tmux._run", return_value=_ok("@217\tw133\n@218\tw134\n")),
        ):
            assert mgr.get_window_info(5) == ("@218", 134)

    def test_get_window_info_legacy_and_unnumbered_names(self) -> None:
        mgr = _mgr()
        with (
            patch.object(mgr, "_find_window_for_thread", return_value="@3"),
            patch("c_lord.tmux._run", return_value=_ok("@3\twork12\n")),
        ):
            assert mgr.get_window_info(5) == ("@3", 12)
        with (
            patch.object(mgr, "_find_window_for_thread", return_value="@3"),
            patch("c_lord.tmux._run", return_value=_ok("@3\tfactorio3\n")),
        ):
            assert mgr.get_window_info(5) == ("@3", None)

    def test_get_window_info_stale_id_reports_no_window(self) -> None:
        """A window killed since the last rebuild must not resolve to anything."""
        mgr = _mgr()
        with (
            patch.object(mgr, "_find_window_for_thread", return_value="@218"),
            patch("c_lord.tmux._run", return_value=_ok("@217\tw133\n")),
        ):
            assert mgr.get_window_info(5) is None

    def test_get_window_info_without_tmux_or_window(self) -> None:
        mgr = _mgr()
        mgr._available = False
        assert mgr.get_window_info(5) is None
        mgr._available = True
        with patch.object(mgr, "_find_window_for_thread", return_value=None):
            assert mgr.get_window_info(5) is None

    def test_window_name_is_the_human_handle(self) -> None:
        mgr = _mgr()
        with (
            patch.object(mgr, "_find_window_for_thread", return_value="@218"),
            patch("c_lord.tmux._run", return_value=_ok("w134\n")),
        ):
            assert mgr.window_name(5) == "w134"
        with patch.object(mgr, "_find_window_for_thread", return_value=None):
            assert mgr.window_name(5) is None
        mgr._available = False
        assert mgr.window_name(5) is None

    def test_remap_to_a_missing_window_name_changes_nothing(self) -> None:
        """#37: an operator naming a window that does not exist gets False, no tag."""
        mgr = _mgr()
        mgr._thread_to_window[9] = "@1"
        with patch("c_lord.tmux._run", return_value=_ok("@1\tw1\n@2\tw2\n")) as mock_run:
            assert mgr.remap_window(10, "w9") is False

        assert mgr._thread_to_window == {9: "@1"}
        assert not any("set-option" in a for a in _argvs(mock_run))

    def test_duplicate_window_names_without_tmux(self) -> None:
        mgr = _mgr()
        mgr._available = False
        assert mgr.duplicate_window_names() == []


class TestListWindowsFull:
    """The state-sync loop cross-references these rows with DB rows."""

    def test_rows_are_parsed_by_tab_and_short_rows_skipped(self) -> None:
        mgr = _mgr()
        out = "w1\t@1\t1\t111\nw2\t@2\t2\t\nzsh\t@0\t0\ngarbage\n"
        with patch("c_lord.tmux._run", return_value=_ok(out)):
            rows = mgr.list_windows_full()

        assert rows == [
            {"window_name": "w1", "window_id": "@1", "window_index": "1", "thread_id": "111"},
            {"window_name": "w2", "window_id": "@2", "window_index": "2", "thread_id": ""},
            {"window_name": "zsh", "window_id": "@0", "window_index": "0", "thread_id": ""},
        ]

    def test_missing_session_or_tmux_is_an_empty_list(self) -> None:
        mgr = _mgr()
        with patch("c_lord.tmux._run", return_value=_fail("can't find session: c-lord")):
            assert mgr.list_windows_full() == []
        mgr._available = False
        assert mgr.list_windows_full() == []


# ── reaper (#427 / #570 / #677 / #790) ───────────────────────────────────


class TestReaperSweepKeepsGoing:
    def test_one_failing_session_does_not_stop_the_sweep(self) -> None:
        """Spec エラー時: 1つのセッションの掃除に失敗しても、他のセッションの掃除は続けます.

        The per-repo session names (#427) collide with hand-made sessions, any
        of which can misbehave; one bad session must not leave every other
        session's corpses in ``tmux ls`` (#616/#618).
        """
        seen: list[tuple[str, str]] = []

        def fake_cleanup(self: TmuxSessionManager, active: set[int]) -> int:
            seen.append((self.session_name, self._mapping_path))
            if self.session_name == "games":
                raise RuntimeError("tmux exploded")
            return 2

        with (
            patch("c_lord.tmux.list_tmux_sessions", return_value=["games", "c-lord", "pt-jp"]),
            patch.object(TmuxSessionManager, "cleanup_orphaned", fake_cleanup),
        ):
            total = cleanup_orphaned_all_sessions(set())

        assert total == 4
        assert [s for s, _ in seen] == ["games", "c-lord", "pt-jp"]
        assert all(path == "" for _, path in seen), (
            "the sweep must not rewrite another manager's on-disk window map"
        )

    def test_untagged_row_without_a_target_is_never_reaped(self) -> None:
        """#677: no id and no name means nothing to address — and nothing to kill."""
        mgr = _mgr()
        with patch("c_lord.tmux._run", return_value=_ok("zsh\n")) as mock_run:
            assert mgr._is_reapable_untagged({"window_name": "w3"}, "") is False
        mock_run.assert_not_called()

    def test_untagged_window_whose_probe_fails_is_kept(self) -> None:
        """Spec: tmux が答えないときは、消さずに残します (#677).

        A failed ``list-panes`` is "do not know what runs here", not "empty".
        """
        mgr = _mgr()
        killed: list[list[str]] = []

        def run(argv: list[str]) -> MagicMock:
            if "list-windows" in argv:
                return _ok("w3\t\t@3\t\t/home/yousan\n")
            if "list-panes" in argv:
                return _fail("server busy")
            if "kill-window" in argv:
                killed.append(argv)
            return _ok()

        with patch("c_lord.tmux._run", side_effect=run):
            assert mgr.cleanup_orphaned(set()) == 0
        assert killed == []


# ── host-wide pane readers (#576 / #742) ─────────────────────────────────


class TestHostWidePaneReaders:
    def test_resident_counts_only_tagged_panes_running_claude(self) -> None:
        """#576: a shell holds a window, not the ~400 MB the cap is about.

        Counting it would evict a live workspace to make room for a corpse; an
        untagged pane is a hand-made one and none of c-lord's business.
        """
        out = "111\tclaude\t%1\n\tclaude\t%2\n222\tzsh\t%3\n333\tClaude\t%4\n"
        with (
            patch("c_lord.tmux._tmux_available", return_value=True),
            patch("c_lord.tmux._run", return_value=_ok(out)),
        ):
            assert resident_thread_ids() == {111, 333}

    def test_resident_is_empty_when_tmux_cannot_answer(self) -> None:
        with (
            patch("c_lord.tmux._tmux_available", return_value=True),
            patch("c_lord.tmux._run", return_value=_fail("no server running")),
        ):
            assert resident_thread_ids() == set()

    def test_running_never_puts_an_unvalidated_pane_id_into_argv(self) -> None:
        """#742: the eviction guard captures each candidate's pane by id.

        ``pane_id`` comes from tmux, but this is not the place to trust a token
        into an argv — anything not shaped like ``%N`` is skipped.
        """
        listing = "111\tclaude\t%5\n222\tclaude\t-t;kill-server\n"
        captured: list[str] = []

        def run(argv: list[str]) -> MagicMock:
            if "list-panes" in argv:
                return _ok(listing)
            if "capture-pane" in argv:
                captured.append(argv[argv.index("-t") + 1])
                return _ok("✻ Working… (31m 2s · esc to interrupt)\n")
            return _ok()

        with (
            patch("c_lord.tmux._tmux_available", return_value=True),
            patch("c_lord.tmux._run", side_effect=run),
            patch("c_lord.tmux.pane_shows_running", return_value=True),
        ):
            assert running_thread_ids() == {111}

        assert captured == ["%5"]

    def test_running_only_reads_the_candidates_it_was_given(self) -> None:
        """#742: one capture per candidate — callers pass the set they act on."""
        listing = "111\tclaude\t%5\n222\tclaude\t%6\n"
        captured: list[str] = []

        def run(argv: list[str]) -> MagicMock:
            if "list-panes" in argv:
                return _ok(listing)
            captured.append(argv[argv.index("-t") + 1])
            return _ok("")

        with (
            patch("c_lord.tmux._tmux_available", return_value=True),
            patch("c_lord.tmux._run", side_effect=run),
            patch("c_lord.tmux.pane_shows_running", return_value=True),
        ):
            assert running_thread_ids([222]) == {222}

        assert captured == ["%6"]
