"""The sub-agent log (#971) under load, at startup and across restarts.

- #1025: building the attachment, the ``glob`` of the agents' directory and the
  agents' line buffers must not stall the event loop or grow without bound.
- #1026: a row half-written when the log starts is read once it is finished.
- #1027: one run, one start line and one end line — a resumed agent whose meta
  still says ``finished`` is not closed by a quiet spell, and a run that crosses
  a restart does not leave its old start line reading "作業中".
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from c_lord.transcript import subagents as subagents_mod
from c_lord.transcript.subagents import MAX_FILE_BYTES, SubagentLog

from .test_subagent_log import (
    ASKER_B,
    CALC_A,
    FIXTURES,
    SESSION,
    Clock,
    FakePoster,
    _append,
    _copy_meta,
    _events,
    _project,
)
from .transcript.test_tail_nonblocking import _LoopWatchdog

SRC = FIXTURES / "plain" / "subagents"


def _rows(agent: str) -> list[dict]:
    return _events(SRC / f"agent-{agent}.jsonl")


def _line(row: dict) -> bytes:
    return (json.dumps(row, ensure_ascii=False) + "\n").encode()


# --- #1026: a row half-written at startup ------------------------------------


async def test_a_row_half_written_at_startup_is_read_once_finished(tmp_path: Path) -> None:
    project, subdir = _project(tmp_path, SESSION)
    _copy_meta(SRC, subdir)
    rows = _rows(ASKER_B)
    done, nxt = rows[:1], rows[1]  # the assignment is on disk; a tool call is arriving
    assert nxt["message"]["content"][0]["type"] == "tool_use"
    path = subdir / f"agent-{ASKER_B}.jsonl"
    whole = _line(nxt)
    path.write_bytes(b"".join(_line(r) for r in done) + whole[:20])
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster)
    await log.prime()
    with path.open("ab") as f:
        f.write(whole[20:])
    await log.step()
    agent = log._agents[ASKER_B]
    assert agent.run is not None, "the finished row was dropped"
    assert agent.run.tools == 1
    assert [p.text for p in poster.statuses] == [
        "-# 🤖 サブエージェント「probe child B (asks A)」再開"
    ]


async def test_rows_finished_before_startup_are_not_read_again(tmp_path: Path) -> None:
    project, subdir = _project(tmp_path, SESSION)
    for f in SRC.iterdir():
        shutil.copy(f, subdir / f.name)
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster)
    await log.prime()
    await log.step()
    assert poster.statuses == []


# --- #1027: one start and one end per run -------------------------------------


async def test_resumed_agent_with_a_stale_finished_meta_survives_a_quiet_spell(
    tmp_path: Path,
) -> None:
    """The meta still says ``finished`` from the previous run; a long Bash is quiet."""
    project, subdir = _project(tmp_path, SESSION)
    shutil.copy(SRC / f"agent-{CALC_A}.meta.json", subdir)  # finished: true
    clock = Clock()
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, clock=clock)
    await log.prime()
    rows = _rows(CALC_A)
    first, resumed = rows[:3], rows[3:7]  # ... up to its first tool_use
    assert resumed[-1]["message"]["content"][0]["type"] == "tool_use"
    _append(subdir / f"agent-{CALC_A}.jsonl", first)
    await log.step()
    notice = {
        "type": "queue-operation",
        "operation": "enqueue",
        "content": (
            f"<task-notification><task-id>{CALC_A}</task-id>"
            "<status>completed</status></task-notification>"
        ),
    }
    log.observe_parent(notice)
    await log.step()
    _append(subdir / f"agent-{CALC_A}.jsonl", resumed)
    await log.step()
    clock.now += 31  # the tool runs for a while
    await log.step()
    texts = [p.text for p in poster.statuses]
    assert sum("終了" in t for t in texts) == 1, texts  # only the first run's
    assert log._agents[CALC_A].run is not None


async def test_a_finished_meta_still_closes_a_run_that_ended_its_turn(tmp_path: Path) -> None:
    """The quiet-spell close still works when the rows say the run is over."""
    project, subdir = _project(tmp_path, SESSION)
    shutil.copy(SRC / f"agent-{ASKER_B}.meta.json", subdir)  # finished: true
    clock = Clock()
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, clock=clock)
    await log.prime()
    _append(subdir / f"agent-{ASKER_B}.jsonl", _rows(ASKER_B))  # ends in end_turn text
    await log.step()
    clock.now += 31
    await log.step()
    assert sum("終了" in p.text for p in poster.statuses) == 1


@dataclass
class _Msg:
    id: int


@dataclass
class RestartPoster(FakePoster):
    """A poster whose lines can be found again by id after a restart."""

    restored: list[int] = field(default_factory=list)
    base: int = 1000

    async def post_status(self, text: str, file):
        await super().post_status(text, file)
        return _Msg(self.base + len(self.statuses))

    async def restore_handle(self, message_id: int):
        self.restored.append(message_id)
        return _Msg(message_id)


async def test_a_run_crossing_a_restart_does_not_leave_its_start_line_working(
    tmp_path: Path,
) -> None:
    project, subdir = _project(tmp_path, SESSION)
    _copy_meta(SRC, subdir)
    rows = _rows(ASKER_B)
    before = RestartPoster()
    old = SubagentLog(thread_id=1, project_dir=project, poster=before)
    await old.prime()
    _append(subdir / f"agent-{ASKER_B}.jsonl", rows[:3])
    await old.step()
    assert [p.text for p in before.statuses] == [
        "-# 🤖 サブエージェント「probe child B (asks A)」開始"
    ]
    await old.stop()  # the bot restarts here, the agent keeps working

    after = RestartPoster(base=2000)
    new = SubagentLog(thread_id=1, project_dir=project, poster=after)
    await new.prime()
    assert after.restored == [1001]
    (handle, edited) = after.edits[-1]
    assert handle == _Msg(1001)
    assert "開始" in edited.text and "作業中" not in edited.text and "途切れ" in edited.text

    _append(subdir / f"agent-{ASKER_B}.jsonl", rows[3:])
    await new.step()
    assert [p.text for p in after.statuses][:1] == [
        "-# 🤖 サブエージェント「probe child B (asks A)」再開"
    ]

    # The old line is closed once: a further restart does not touch it again.
    await new.stop()
    again = RestartPoster(base=3000)
    third = SubagentLog(thread_id=1, project_dir=project, poster=again)
    await third.prime()
    assert 1001 not in again.restored
    assert again.restored == [2001]  # the 再開 line the second process left open


# --- #1025: the event loop, and memory --------------------------------------


async def test_the_glob_of_the_agents_directory_runs_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, subdir = _project(tmp_path, SESSION)
    _copy_meta(SRC, subdir)
    _append(subdir / f"agent-{CALC_A}.jsonl", _rows(CALC_A)[:3])
    log = SubagentLog(thread_id=1, project_dir=project, poster=FakePoster())
    await log.prime()
    loop_thread = threading.get_ident()
    on_loop: list[str] = []
    real_glob = Path.glob

    def spy(self: Path, pattern: str, *a, **kw):
        if threading.get_ident() == loop_thread:
            on_loop.append(pattern)
        return real_glob(self, pattern, *a, **kw)

    monkeypatch.setattr(Path, "glob", spy)
    await log.step()
    assert log._agents, "the agent should have been found"
    assert on_loop == []


def test_an_agents_lines_stay_bounded() -> None:
    agent = subagents_mod._Agent(agent_id="a1", path=Path("/nonexistent"))
    for i in range(5000):
        agent.lines.append(f"{i:05d} " + "x" * 4000)
    kept = list(agent.lines)
    assert sum(len(s) for s in kept) <= MAX_FILE_BYTES * 1.1
    assert kept[0].startswith("00000 ")  # the beginning stays
    assert kept[-1].startswith("04999 ")  # and so does the latest
    assert len(agent.lines) == 5000  # still counts what came in


async def test_attachments_for_many_large_agents_do_not_stall_the_loop(tmp_path: Path) -> None:
    """20 agents with 5 MB of progress each, all due for an attachment at once."""
    project, subdir = _project(tmp_path, SESSION)
    clock = Clock()
    poster = FakePoster()
    log = SubagentLog(
        thread_id=1, project_dir=project, poster=poster, clock=clock, progress_seconds=10.0
    )
    await log.prime()
    chunk = "🔧 Bash: " + "ログ" * 1300 + " agent-a201a36f65822ff1e"
    for n in range(20):
        agent_id = f"a{n:016x}"
        agent = subagents_mod._Agent(
            agent_id=agent_id,
            path=subdir / f"agent-{agent_id}.jsonl",
            meta=subagents_mod.AgentMeta(name=f"worker {n}"),
            ordinal=n + 1,
        )
        for _ in range(5_000_000 // (len(chunk) * 3)):
            agent.lines.append(chunk)
        agent.run = subagents_mod._Run(
            started_at=None, resumed=False, started_clock=clock.now, last_row_clock=clock.now
        )
        agent.run.handle = n
        log._agents[agent_id] = agent
    clock.now += 11
    watchdog = _LoopWatchdog()
    watchdog.start()
    await log._refresh_running()
    await watchdog.stop()
    assert len(poster.edits) == 20
    assert watchdog.max_gap < 0.1, f"event loop stalled for {watchdog.max_gap:.3f}s"
    assert all(len((p.file or "").encode()) <= MAX_FILE_BYTES * 1.1 for _, p in poster.edits)
    await asyncio.sleep(0)


# --- #1027 AC3: a foreground Agent call (CLI 2.1.312, measured 2026-10-29) ---

FG_AGENT = "a2526ba2a98b15d03"
FG_SESSION = "ba75feb4-1b3a-498c-816d-8eed71b6586e"


def test_foreground_result_names_the_agent_and_a_launch_does_not() -> None:
    from c_lord.transcript.subagents import foreground_results

    rows = _events(FIXTURES / "foreground" / "parent.jsonl")
    assert [r for row in rows for r in foreground_results(row)] == [(FG_AGENT, "completed")]
    # A background launch's receipt carries a toolUseResult too — not an end.
    launch = {
        "type": "user",
        "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": FG_AGENT},
    }
    assert foreground_results(launch) == []


async def test_a_foreground_agent_gets_one_start_and_one_end(tmp_path: Path) -> None:
    """No <task-notification> comes for it: the parent's tool_result is the end."""
    project, subdir = _project(tmp_path, FG_SESSION)
    src = FIXTURES / "foreground" / "subagents"
    clock = Clock()
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, clock=clock)
    await log.prime()
    shutil.copy(src / f"agent-{FG_AGENT}.meta.json", subdir)
    _append(subdir / f"agent-{FG_AGENT}.jsonl", _events(src / f"agent-{FG_AGENT}.jsonl"))
    await log.step()
    for row in _events(FIXTURES / "foreground" / "parent.jsonl"):
        log.observe_parent(row)
    await log.step()
    # Closed by the parent's tool_result itself, not 30 s later by the meta.
    assert sum("終了" in p.text for p in poster.statuses) == 1
    for _ in range(3):
        clock.now += 3600
        await log.step()
    texts = [p.text for p in poster.statuses]
    assert texts[0] == "-# 🤖 サブエージェント「fg probe explicit」開始"
    assert sum("終了" in t for t in texts) == 1, texts
    assert not any("記録が止まりました" in t for t in texts)
