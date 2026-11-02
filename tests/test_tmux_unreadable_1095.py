"""#1095 — tmux が詰まっているのを「誰も走っていない」と読まない。

#1034 の守りは「読めないペインは眠らせない」。ホストが詰まって tmux が遅い・
接続を拒む場面はまさに緊急ブレーキが働く場面で、そこで「誰も走っていない」と
読むと走っているターンが眠らされ得る。

* ``tmux -V`` の時間切れ → 読めない（tmux が無いのではない）
* ``error connecting to … (Resource temporarily unavailable)`` → 読めない
  （サーバはある。無いのは ``no server running`` と、ソケットが無い ENOENT /
  ECONNREFUSED のときだけ）
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from c_lord import tmux
from c_lord.tmux import live_claude_panes, pane_turn_states


def test_tmux_version_timeout_reads_as_unreadable() -> None:
    def run(argv: list[str]) -> MagicMock:
        if argv[:2] == ["tmux", "-V"]:
            return MagicMock(returncode=124, stdout="", stderr="timed out")
        raise AssertionError(argv)

    with patch("c_lord.tmux._run", side_effect=run):
        s = pane_turn_states([111])
        assert live_claude_panes() is None
    assert s.unreadable == frozenset({111}), s


def test_connect_eagain_reads_as_unreadable() -> None:
    eagain = MagicMock(
        returncode=1,
        stdout="",
        stderr="error connecting to /tmp/tmux-1000/default (Resource temporarily unavailable)",
    )
    with (
        patch("c_lord.tmux._tmux_available", return_value=True),
        patch("c_lord.tmux._run", return_value=eagain),
    ):
        s = pane_turn_states([111])
        assert live_claude_panes() is None
    assert s.unreadable == frozenset({111}), s


@pytest.mark.parametrize(
    "stderr",
    [
        "no server running on /tmp/tmux-1000/default",
        # what tmux prints when the socket is not there at all (host reboot, #807)
        "error connecting to /tmp/tmux-1000/default (No such file or directory)",
        "error connecting to /tmp/tmux-1000/default (Connection refused)",
    ],
)
def test_no_server_is_still_an_answer(stderr: str) -> None:
    with (
        patch("c_lord.tmux._tmux_available", return_value=True),
        patch("c_lord.tmux._run", return_value=MagicMock(returncode=1, stdout="", stderr=stderr)),
    ):
        s = pane_turn_states([111])
        live = live_claude_panes()
    assert s.unreadable == frozenset(), s
    assert live is not None and not live.thread_ids


def test_tmux_not_installed_is_still_nobody_running() -> None:
    with patch(
        "c_lord.tmux._run",
        return_value=MagicMock(returncode=127, stdout="", stderr="tmux: not found"),
    ):
        assert tmux._claude_panes_listing() == {}
