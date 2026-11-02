"""#1144: a child's start line must not wait for a row of the parent's.

Claude Code writes the parent's ``Agent`` rows only when the whole API message
is done — 9 launches in one message were on disk 80 s after the first child
started (W57, 2026-10-30). The parent's rows are what woke the log, so it slept
through all of them in its 300 s idle wait.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from c_lord.transcript import subagents as mod
from c_lord.transcript.subagents import SubagentLog

from .test_subagent_log import SESSION, FakePoster, _append, _project

AID = "a0123456789abcdef"


async def test_a_new_child_is_shown_without_any_parent_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "DIR_POLL_SECONDS", 0.05, raising=False)
    project, subdir = _project(tmp_path, SESSION)
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, poll_interval=0.05)
    log.start()
    try:
        await asyncio.sleep(0.3)  # primed, nothing running: in the idle wait
        _append(
            subdir / f"agent-{AID}.jsonl",
            [
                {
                    "type": "user",
                    "uuid": "u0",
                    "timestamp": "2026-10-30T05:21:20Z",
                    "isSidechain": True,
                    "message": {"role": "user", "content": "事後レビュー"},
                }
            ],
        )
        for _ in range(40):
            if poster.statuses:
                break
            await asyncio.sleep(0.05)
    finally:
        await log.stop()
    assert [p.text for p in poster.statuses] == ["-# 🤖 サブエージェント「?」開始"]


async def test_a_wake_that_lands_during_a_step_is_not_lost(tmp_path: Path) -> None:
    """The wake was cleared *after* each step, erasing one that came in meanwhile."""
    project, _ = _project(tmp_path, SESSION)
    log = SubagentLog(thread_id=1, project_dir=project, poster=FakePoster(), poll_interval=0.05)
    steps = 0
    real = log.step

    async def step() -> None:
        nonlocal steps
        steps += 1
        if steps == 2:
            log._wake.set()  # a parent row arriving while this step runs
        await real()

    log.step = step  # type: ignore[method-assign]
    log.start()
    try:
        await asyncio.sleep(0.2)
        log._wake.set()
        await asyncio.sleep(0.3)
    finally:
        await log.stop()
    assert steps >= 3, steps
