"""#1193 — one stalled ``tmux -V`` disabled tmux for the life of the process.

#1095 says a tmux that does not answer is not a tmux that is missing. The
manager still cached the first check's "could not tell" as "not installed",
so after one moment of host stall that channel could not make a window until
the bot was restarted. And the pane listing asked ``tmux -V`` twice on a stall
(30 s × 2). Only a real answer is remembered; tmux that is truly absent still
reads as absent. No real tmux.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import c_lord.tmux as T


def test_manager_caches_version_timeout_as_not_installed() -> None:
    calls = {"n": 0}

    def run(argv: list[str]) -> MagicMock:
        if argv[:2] == ["tmux", "-V"]:
            calls["n"] += 1
            if calls["n"] == 1:  # the host stalled for a moment
                return MagicMock(
                    returncode=T._RUN_TIMEOUT_RETURNCODE, stdout="", stderr="timed out"
                )
            return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    m = T.TmuxSessionManager(session_name="clord", mapping_path="")
    with patch("c_lord.tmux._run", side_effect=run):
        assert m._check_available() is False
        later = [m._check_available() for _ in range(3)]
    assert later == [True, True, True], f"stall at first check disabled tmux for good: {later}"
    assert calls["n"] == 2  # the real answer is then remembered


def test_tmux_not_installed_is_still_remembered_as_absent() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> MagicMock:
        seen.append(argv[:2])
        return MagicMock(returncode=127, stdout="", stderr="not found")

    m = T.TmuxSessionManager(session_name="clord", mapping_path="")
    with patch("c_lord.tmux._run", side_effect=run):
        assert [m._check_available() for _ in range(3)] == [False, False, False]
        assert T._claude_panes_listing() == {}
    assert seen.count(["tmux", "-V"]) == 2  # once for the manager, once for the listing


def test_listing_asks_version_twice_on_stall() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> MagicMock:
        seen.append(argv[:2])
        return MagicMock(returncode=T._RUN_TIMEOUT_RETURNCODE, stdout="", stderr="timed out")

    with patch("c_lord.tmux._run", side_effect=run):
        assert T._claude_panes_listing() is None
    assert seen.count(["tmux", "-V"]) == 1, seen  # was 2 (30 s × 2)
