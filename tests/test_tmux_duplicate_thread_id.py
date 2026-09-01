"""Duplicate ``@thread_id`` claims must resolve to the live window (#501).

Two windows can end up pointing at the same session dir — tmux-resurrect
restores a stale window next to the live one. ``_rebuild_mapping`` used to
stamp ``@thread_id`` on every path match and then let
``new_map[thread_id] = window_name`` overwrite as it walked ``list-windows``
output, so the LAST window in index order won. In the #501 incident that was
``w8``, a dead zsh, while Claude ran in ``work2``: Discord got screenshots of
the wrong pane (``tmux-claude_base-w8.png``), ``is_claude_running`` said False
for a live session, and the menu bridge — seeing no menu in the dead pane —
re-posted the same AskUserQuestion every 59s marked "端末で回答済み".

The binding must follow the window that is actually running Claude, never
list order.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager

_THREAD = 1515873566166483074
_DIR = f"/home/yousan/c-lord-sessions/1504670029923352597/{_THREAD}"

# Reproduces the incident: the live window comes FIRST in list order, the dead
# one last, so a last-write-wins rebuild picks the dead one. Windows are keyed by
# tmux ``window_id`` (#649) — a name identifies no single window.
_LIVE = "@2"
_DEAD = "@8"
_NAMES = {_LIVE: "work2", _DEAD: "w8"}


def _make_run(options: dict[str, str], *, live_window: str):
    """Fake ``_run`` for a session holding one live and one dead window."""

    def _fake_run(args: list[str]) -> MagicMock:
        if "list-windows" in args:
            fmt = args[args.index("-F") + 1] if "-F" in args else "#{window_name}"
            rows = ""
            for wid in (_LIVE, _DEAD):
                row = fmt
                for token, value in (
                    ("#{window_id}", wid),
                    ("#{window_name}", _NAMES[wid]),
                    ("#{@thread_id}", options.get(wid, "")),
                    ("#{pane_current_path}", _DIR),
                ):
                    row = row.replace(token, value)
                rows += row + "\n"
            return MagicMock(returncode=0, stdout=rows)

        if "show-option" in args and "@thread_id" in args:
            win = args[args.index("-t") + 1]
            val = options.get(win, "")
            # tmux exits non-zero when the option is unset.
            return MagicMock(returncode=0 if val else 1, stdout=f"{val}\n" if val else "")

        if "set-option" in args and "@thread_id" in args:
            win = args[args.index("-t") + 1]
            if "-uw" in args:
                options.pop(win, None)
            else:
                options[win] = args[-1]
            return MagicMock(returncode=0, stdout="")

        if "list-panes" in args:
            win = args[args.index("-t") + 1]
            return MagicMock(returncode=0, stdout="claude\n" if win == live_window else "zsh\n")

        return MagicMock(returncode=0, stdout="")

    return _fake_run


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "claude_base"
    return mgr


def test_option_conflict_binds_to_the_window_running_claude() -> None:
    """Both windows carry @thread_id; the one running Claude must win."""
    options = {_LIVE: str(_THREAD), _DEAD: str(_THREAD)}
    mgr = _mgr()

    with patch("c_lord.tmux._run", side_effect=_make_run(options, live_window=_LIVE)):
        assert mgr._find_window_for_thread(_THREAD) == _LIVE
        assert mgr.is_claude_running(_THREAD) is True

    # The losing claim is cleared so the conflict does not come back next poll.
    assert _DEAD not in options


def test_path_recovery_does_not_steal_a_thread_already_claimed() -> None:
    """A pathmatching window must not overwrite a window that owns the option."""
    options = {_LIVE: str(_THREAD)}  # dead window's option was cleared by a restart
    mgr = _mgr()

    with patch("c_lord.tmux._run", side_effect=_make_run(options, live_window=_LIVE)):
        assert mgr._find_window_for_thread(_THREAD) == _LIVE

    # The dead window must not have been stamped with the thread id.
    assert options.get(_DEAD) is None


def test_conflict_without_a_live_window_is_stable_and_non_destructive() -> None:
    """No Claude anywhere: bind deterministically and leave the options alone."""
    options = {_LIVE: str(_THREAD), _DEAD: str(_THREAD)}
    mgr = _mgr()

    with patch("c_lord.tmux._run", side_effect=_make_run(options, live_window="nobody")):
        first = mgr._find_window_for_thread(_THREAD)
        mgr._thread_to_window.pop(_THREAD, None)
        second = mgr._find_window_for_thread(_THREAD)

    assert first == second == _LIVE  # first in list order
    # Nothing was unset — we have no evidence about which window is the real one.
    assert options == {_LIVE: str(_THREAD), _DEAD: str(_THREAD)}
