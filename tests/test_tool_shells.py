"""Tests for c_lord/tool_shells.py — finding Claude's Bash tool shells (#878)."""

from __future__ import annotations

import signal
from pathlib import Path

from c_lord.tool_shells import (
    ProcInfo,
    read_procs,
    terminate_tool_shells,
    tool_shell_groups,
)

SNAP = (
    "/usr/bin/zsh -c source /home/u/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'sleep 600'"
)


def _tree() -> list[ProcInfo]:
    """The shape observed on staging (#878): pane zsh → claude → shells / MCP."""
    return [
        ProcInfo(pid=100, ppid=1, pgid=100, cmdline="-zsh"),  # pane shell
        ProcInfo(pid=200, ppid=100, pgid=200, cmdline="claude --session-id x"),
        ProcInfo(pid=300, ppid=200, pgid=200, cmdline="bun mcp-server.ts"),  # MCP server
        ProcInfo(pid=400, ppid=200, pgid=400, cmdline=SNAP),  # background Bash tool
        ProcInfo(pid=401, ppid=400, pgid=400, cmdline="sleep 600"),
        ProcInfo(pid=500, ppid=200, pgid=500, cmdline=SNAP),  # foreground Bash tool
        ProcInfo(pid=900, ppid=1, pgid=900, cmdline=SNAP),  # another pane's shell
    ]


class TestToolShellGroups:
    def test_finds_the_tool_shells_under_the_pane(self) -> None:
        assert sorted(tool_shell_groups(100, _tree())) == [400, 500]

    def test_never_returns_the_claude_or_mcp_group(self) -> None:
        groups = tool_shell_groups(100, _tree())
        assert 200 not in groups

    def test_ignores_shells_of_other_panes(self) -> None:
        assert 900 not in tool_shell_groups(100, _tree())

    def test_shell_that_shares_claudes_group_is_not_killed(self) -> None:
        """A shell that is not its own group leader would take claude down with it."""
        procs = [
            ProcInfo(pid=100, ppid=1, pgid=100, cmdline="-zsh"),
            ProcInfo(pid=200, ppid=100, pgid=200, cmdline="claude"),
            ProcInfo(pid=400, ppid=200, pgid=200, cmdline=SNAP),
        ]
        assert tool_shell_groups(100, procs) == []

    def test_unknown_pane_pid_yields_nothing(self) -> None:
        assert tool_shell_groups(12345, _tree()) == []


class TestTerminateToolShells:
    def test_signals_each_group_once(self) -> None:
        sent: list[tuple[int, int]] = []
        n = terminate_tool_shells(
            100, procs=_tree(), killpg=lambda pgid, sig: sent.append((pgid, sig))
        )
        assert n == 2
        assert sorted(sent) == [(400, signal.SIGTERM), (500, signal.SIGTERM)]

    def test_a_group_that_already_exited_is_not_counted(self) -> None:
        def killpg(pgid: int, sig: int) -> None:
            if pgid == 500:
                raise ProcessLookupError

        assert terminate_tool_shells(100, procs=_tree(), killpg=killpg) == 1


class TestReadProcs:
    def test_parses_stat_and_cmdline(self, tmp_path: Path) -> None:
        d = tmp_path / "4242"
        d.mkdir()
        # comm may contain spaces and parentheses — the parser must split after the last ')'.
        (d / "stat").write_text("4242 (my (odd) cmd) S 77 4240 4240 0 -1 4194560 0 0")
        (d / "cmdline").write_bytes(b"zsh\x00-c\x00source x\x00")
        (tmp_path / "self").mkdir()
        (tmp_path / "notapid").write_text("")
        assert read_procs(tmp_path) == [
            ProcInfo(pid=4242, ppid=77, pgid=4240, cmdline="zsh -c source x")
        ]

    def test_vanished_process_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "5").mkdir()  # no stat file — exited between listdir and read
        assert read_procs(tmp_path) == []
