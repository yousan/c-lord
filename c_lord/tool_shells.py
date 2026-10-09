"""Find and stop the shells Claude Code's Bash tool started in a pane (#878).

A C-c in the pane interrupts Claude's *turn*, but not the commands Claude put
in the background (``run_in_background``, or a command the CLI moved there
itself after its 2-minute timeout — CLI 2.1.x also refuses a foreground
``sleep`` and backgrounds it). Those keep running, and when one finishes the
CLI hands Claude a ``<task-notification>`` that starts a new turn nobody asked
for. That is what "⏹ Stop does not stop" looked like on staging.

Every Bash tool command runs as ``<shell> -c source ~/.claude/shell-snapshots/…``
in its **own process group**, as a child of the ``claude`` process. So the
pane's process tree tells us exactly which groups are Claude's tool commands:
the MCP servers and ``claude`` itself share claude's group and never match.
"""

from __future__ import annotations

import logging
import os
import signal
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Present in the command line of every shell the Bash tool spawns.
SHELL_SNAPSHOT_MARKER = "/.claude/shell-snapshots/"


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    cmdline: str


def read_procs(proc_root: Path = Path("/proc")) -> list[ProcInfo]:
    """Snapshot pid / ppid / pgid / cmdline of every process, skipping the vanished."""
    procs: list[ProcInfo] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return procs
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            raw_cmd = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        # ``comm`` (field 2) is parenthesised and may itself contain ") ".
        fields = stat[stat.rfind(")") + 2 :].split()
        try:
            ppid, pgid = int(fields[1]), int(fields[2])
        except (IndexError, ValueError):
            continue
        cmdline = raw_cmd.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        procs.append(ProcInfo(pid=int(entry.name), ppid=ppid, pgid=pgid, cmdline=cmdline))
    return procs


def tool_shell_groups(root_pid: int, procs: Iterable[ProcInfo]) -> list[int]:
    """Process groups of the Bash tool shells under *root_pid* (the pane's process).

    Only a shell that leads its own group is returned: signalling a group that
    ``claude`` belongs to would kill Claude Code instead of its command.
    """
    children: dict[int, list[ProcInfo]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p)
    groups: list[int] = []
    stack = list(children.get(root_pid, []))
    while stack:
        p = stack.pop()
        if SHELL_SNAPSHOT_MARKER in p.cmdline and p.pgid == p.pid:
            groups.append(p.pgid)
            continue  # its descendants die with the group
        stack.extend(children.get(p.pid, []))
    return groups


def terminate_tool_shells(
    root_pid: int,
    *,
    procs: Iterable[ProcInfo] | None = None,
    killpg: Callable[[int, int], None] = os.killpg,
) -> int:
    """SIGTERM every Bash tool shell group under *root_pid*; return how many were signalled."""
    groups = tool_shell_groups(root_pid, read_procs() if procs is None else procs)
    stopped = 0
    for pgid in groups:
        try:
            killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            continue  # finished on its own in the meantime
        except OSError:
            logger.warning("could not stop tool shell group %d", pgid, exc_info=True)
            continue
        stopped += 1
    return stopped
