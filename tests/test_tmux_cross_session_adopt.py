"""#427: a thread whose repo binding changed must keep its existing tmux window.

Before this, ``create_session`` only ever looked inside its own session, so when
``resolve_tmux_manager`` started honouring thread bindings the window a thread
already had in the *parent channel's* session became invisible: a second window
(and a second Claude) was created for the same session dir.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager

WORKDIR = "/home/yousan/c-lord-sessions/1506819883797712998/1515732180549238985"
THREAD = 1515732180549238985


def _manager(session: str = "monitoring") -> TmuxSessionManager:
    mgr = TmuxSessionManager(session_name=session, mapping_path="")
    mgr._available = True
    mgr._sort_windows_unlocked = lambda: None  # type: ignore[method-assign]
    mgr._ensure_window_size_manual = lambda: None  # type: ignore[method-assign]
    mgr._fit_window_to_client = lambda *_: None  # type: ignore[method-assign]
    return mgr


def _router(all_windows: str, own_windows: str = ""):
    """Dispatch ``_run`` on the tmux subcommand instead of on call order."""
    calls: list[list[str]] = []

    def run(args, **_kwargs):
        calls.append(args)
        sub = args[1] if len(args) > 1 else ""
        if sub == "list-windows":
            return MagicMock(returncode=0, stdout=all_windows if "-a" in args else own_windows)
        if sub in ("has-session", "list-sessions"):
            return MagicMock(returncode=0, stdout="")
        return MagicMock(returncode=0, stdout="")

    return run, calls


class TestAdoptWindowFromOtherSession:
    def test_moves_existing_window_instead_of_creating_a_second(self) -> None:
        # The thread's window currently lives in the parent channel's session.
        elsewhere = f"games\t@42\tw5\t\t{WORKDIR}\n"
        run, calls = _router(all_windows=elsewhere)
        mgr = _manager()

        with patch("c_lord.tmux._run", side_effect=run):
            name = mgr.create_session(THREAD, WORKDIR)

        subs = [c[1] for c in calls if len(c) > 1]
        assert "move-window" in subs, "the existing window must be moved, not abandoned"
        assert "new-window" not in subs, "a second window would mean a second Claude"
        assert mgr._thread_to_window[THREAD] == name

        move = next(c for c in calls if c[1] == "move-window")
        # Addressed by window_id: "games:w5" is ambiguous the moment another
        # window shares the name — that is how this failed on staging.
        assert "@42" in move
        assert f"{mgr.session_name}:" in move
        assert "games:w5" not in move

    def test_tags_the_adopted_window_with_thread_id(self) -> None:
        """The games windows lost @thread_id to a tmux restart — repair it."""
        run, calls = _router(all_windows=f"games\t@42\tw5\t\t{WORKDIR}\n")
        mgr = _manager()

        with patch("c_lord.tmux._run", side_effect=run):
            mgr.create_session(THREAD, WORKDIR)

        assert "new-window" not in [c[1] for c in calls if len(c) > 1]
        tagged = [c for c in calls if c[1] == "set-option" and "@thread_id" in c]
        assert tagged, "adopted window must carry @thread_id so later lookups find it"
        assert any("@42" in c and str(THREAD) in c for c in tagged)

    def test_ignores_windows_in_a_different_working_dir(self) -> None:
        """Path equality is the safety anchor — never steal another bot's window."""
        run, calls = _router(all_windows=f"c-lord-staging-2\t@7\tw1\t{THREAD}\t/other/dir\n")
        mgr = _manager()

        with patch("c_lord.tmux._run", side_effect=run):
            mgr.create_session(THREAD, WORKDIR)

        subs = [c[1] for c in calls if len(c) > 1]
        assert "move-window" not in subs
        assert "new-window" in subs

    def test_does_not_move_a_window_already_in_this_session(self) -> None:
        own = f"w5\t{WORKDIR}\n"
        run, calls = _router(all_windows=f"monitoring\t@9\tw5\t{THREAD}\t{WORKDIR}\n", own_windows=own)
        mgr = _manager()

        with patch("c_lord.tmux._run", side_effect=run):
            mgr.create_session(THREAD, WORKDIR)

        subs = [c[1] for c in calls if len(c) > 1]
        assert "move-window" not in subs
        assert "new-window" not in subs


class TestAdoptionSafety:
    def test_adopts_a_window_whose_name_breaks_tmux_target_syntax(self) -> None:
        """``session:window.pane`` — a dotted name is unaddressable by name, but
        the window_id is not, and the post-move rename gives it a safe ``w{N}``."""
        run, calls = _router(all_windows=f"games\t@42\tw5.old\t{THREAD}\t{WORKDIR}\n")
        mgr = _manager()

        with patch("c_lord.tmux._run", side_effect=run):
            name = mgr.create_session(THREAD, WORKDIR)

        subs = [c[1] for c in calls if len(c) > 1]
        assert "move-window" in subs
        assert "new-window" not in subs
        rename = next(c for c in calls if c[1] == "rename-window")
        assert "@42" in rename and name in rename
        assert "." not in name

    def test_renames_only_after_a_successful_move(self) -> None:
        """A failed move must leave the source window's name untouched."""
        calls: list[list[str]] = []

        def run(args, **_kwargs):
            calls.append(args)
            if args[1] == "list-windows":
                out = f"games\t@42\tw5\t{THREAD}\t{WORKDIR}\n" if "-a" in args else ""
                return MagicMock(returncode=0, stdout=out)
            if args[1] == "move-window":
                return MagicMock(returncode=1, stdout="", stderr="can't find window")
            return MagicMock(returncode=0, stdout="")

        mgr = _manager()
        with patch("c_lord.tmux._run", side_effect=run):
            mgr.create_session(THREAD, WORKDIR)

        assert "rename-window" not in [c[1] for c in calls if len(c) > 1]
        assert "new-window" in [c[1] for c in calls if len(c) > 1]
