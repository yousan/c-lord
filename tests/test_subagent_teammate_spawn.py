"""Review #1054: an in-process teammate's spawn receipt is not the end of its run."""

from __future__ import annotations

import json
from pathlib import Path

from c_lord.transcript.subagents import SubagentLog, foreground_results

from .test_subagent_log import (
    FIXTURES,
    TEAM_SESSION,
    Clock,
    FakePoster,
    _append,
    _copy_meta,
    _events,
    _project,
)

BOB = "abob-986a1b2d93bf8eee"


def _parent() -> list[dict]:
    return _events(FIXTURES / "teams" / "parent.jsonl")


def test_spawn_receipt_is_not_a_foreground_end() -> None:
    spawn = [
        r for r in _parent() if (r.get("toolUseResult") or {}).get("status") == "teammate_spawned"
    ]
    assert spawn, "fixture should hold the CLI 2.1.312 teammate_spawned receipts"
    assert [foreground_results(r) for r in spawn] == [[], []]


async def test_teammate_is_not_closed_by_its_spawn_receipt(tmp_path: Path) -> None:
    project, subdir = _project(tmp_path, TEAM_SESSION)
    src = FIXTURES / "teams" / "subagents"
    _copy_meta(src, subdir)
    clock = Clock()
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, clock=clock)
    await log.prime()
    bob = _events(src / f"agent-{BOB}.jsonl")
    # The teammate writes its first rows 4 ms after the parent's receipt (real timing).
    _append(subdir / f"agent-{BOB}.jsonl", bob[:3])
    for row in _parent():
        if (row.get("toolUseResult") or {}).get("status") == "teammate_spawned":
            log.observe_parent(row)
    await log.step()
    _append(subdir / f"agent-{BOB}.jsonl", bob[3:])
    await log.step()
    texts = [p.text for p in poster.statuses]
    # Still working: it has not gone idle yet.
    assert not any("終了" in t for t in texts), texts
    assert sum("再開" in t for t in texts) == 0, texts
