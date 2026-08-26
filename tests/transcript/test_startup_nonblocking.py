"""Issue #537: bot startup must not block the asyncio event loop.

``TranscriptMirrorCog.on_ready`` walks every session row and, for each one,
reads and JSON-parses a whole Claude Code transcript twice — once in
:func:`c_lord.transcript.recovery.last_completed_final_answer` (Issue #215
recovery) and once in :func:`c_lord.transcript.tail._seed_seen_uuids`
(Issue #433 replay safety).  On the production host that is ~1 GB of parsing
per pass.  Done on the event loop it starves the Discord gateway heartbeat,
the shard reconnects, and **messages sent while disconnected are lost for
good** — the user sees "I posted and got no answer".

These tests pin the two properties that prevent it: the heavy scans run in a
worker thread, and the loop is never blocked for a meaningful stretch during
startup.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.transcript import recovery as recovery_mod
from c_lord.transcript import tail as tail_mod

# A line shaped like a real transcript assistant event: big enough that a
# few tens of thousands of them cost measurable CPU to parse.
_LINE_TEXT = "x" * 400


def _assistant_line(i: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": f"{i:08d}-0000-4000-8000-000000000000",
            "parentUuid": f"{i - 1:08d}-0000-4000-8000-000000000000",
            "sessionId": "0f0f0f0f-0000-4000-8000-000000000000",
            "cwd": "/home/user/project",
            "timestamp": "2026-08-26T00:00:00.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": _LINE_TEXT}]},
        }
    )


def _write_big_transcript(project_dir: Path, *, lines: int = 30_000) -> Path:
    """Write a ~20 MB transcript ending in a completed turn."""
    project_dir.mkdir(parents=True, exist_ok=True)
    jsonl = project_dir / "session.jsonl"
    body = [_assistant_line(i) for i in range(lines)]
    body.append(json.dumps({"type": "system", "subtype": "turn_duration"}))
    jsonl.write_text("\n".join(body) + "\n", encoding="utf-8")
    return jsonl


class _LoopWatchdog:
    """Records the longest stretch the event loop went unserviced."""

    def __init__(self, tick: float = 0.005) -> None:
        self._tick = tick
        self.max_gap = 0.0
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(self._tick)
            now = time.perf_counter()
            self.max_gap = max(self.max_gap, now - last - self._tick)
            last = now

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="loop-watchdog")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


# ── AC1: the Issue #215 recovery scan runs off the event loop ────────────


async def test_recovery_scan_runs_in_a_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: ``last_completed_final_answer`` I/O + parse go through
    ``asyncio.to_thread`` — the awaitable entry point must not do the work on
    the calling (event loop) thread."""
    project = tmp_path / "proj"
    _write_big_transcript(project, lines=10)

    loop_thread = threading.get_ident()
    ran_on: list[int] = []
    real = recovery_mod.latest_session_jsonl

    def spy(project_dir: Path):
        ran_on.append(threading.get_ident())
        return real(project_dir)

    monkeypatch.setattr(recovery_mod, "latest_session_jsonl", spy)

    fa = await recovery_mod.last_completed_final_answer_async(project)

    assert fa is not None and fa.text == _LINE_TEXT
    assert ran_on, "the scan never ran"
    assert loop_thread not in ran_on, "scan ran on the event loop thread"


# ── AC2: the Issue #433 uuid seeding runs off the event loop ─────────────


async def test_seed_seen_uuids_runs_in_a_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: ``_seed_seen_uuids`` I/O + parse go through ``asyncio.to_thread``."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text(_assistant_line(1) + "\n", encoding="utf-8")

    loop_thread = threading.get_ident()
    ran_on: list[int] = []
    real = tail_mod._seed_seen_uuids

    def spy(path: Path, upto_offset: int, seen: set[str]) -> None:
        ran_on.append(threading.get_ident())
        return real(path, upto_offset, seen)

    monkeypatch.setattr(tail_mod, "_seed_seen_uuids", spy)

    agen = tail_mod.tail_events(project, poll_interval=0.05).__aiter__()

    async def append_later() -> None:
        await asyncio.sleep(0.15)
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_assistant_line(2) + "\n")

    producer = asyncio.create_task(append_later())
    try:
        event = await asyncio.wait_for(agen.__anext__(), timeout=3.0)
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        await agen.aclose()

    assert event["uuid"] == f"{2:08d}-0000-4000-8000-000000000000"
    assert ran_on, "seeding never ran"
    assert loop_thread not in ran_on, "seeding ran on the event loop thread"


# ── AC4: startup does not stall the loop ─────────────────────────────────


async def test_on_ready_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: startup must leave the event loop responsive.

    Both per-session scans are replaced by a blocking stub of *known* cost, so
    the assertion measures **where** the work runs rather than how fast the
    machine is: run inline, the stub stalls the loop for its full cost; handed
    to a worker thread, it costs the loop nothing.  (A threshold calibrated
    against real JSON parsing does hold on a dev box but is not reproducible on
    a shared CI runner, where the loop and the worker contend for a core.)
    The real functions are covered by the two tests above; the real cost is
    covered by the staging run in the PR.
    """
    from c_lord.cogs.transcript_mirror import TranscriptMirrorCog

    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / ".claude" / "projects" / "-some-cwd"
    _write_big_transcript(project, lines=10)

    # Costly enough to dwarf scheduler jitter, cheap enough for a unit test.
    blocking_cost = 0.4

    def blocking_scan(project_dir: Path):
        time.sleep(blocking_cost)  # stands in for reading + parsing a transcript
        return None

    def blocking_seed(path: Path, upto_offset: int, seen: set[str]) -> None:
        time.sleep(blocking_cost)

    monkeypatch.setattr(recovery_mod, "last_completed_final_answer", blocking_scan)
    monkeypatch.setattr(tail_mod, "_seed_seen_uuids", blocking_seed)

    row = MagicMock()
    row.thread_id = 11
    row.working_dir = "/some/cwd"
    row.closed_at = None
    row.mirror_replied_uuid = None

    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[row])
    repo.get = AsyncMock(return_value=None)
    repo.set_mirror_replied_uuid = AsyncMock()

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    cog = TranscriptMirrorCog(bot, session_repo=repo)
    watchdog = _LoopWatchdog()
    watchdog.start()
    await asyncio.sleep(0.02)  # let the watchdog take its first sample
    try:
        await cog.on_ready()
        # Let the mirror's tail task reach its seeding step too — that is the
        # second blocking scan, and the one the prod journal caught red-handed.
        await asyncio.sleep(blocking_cost * 2)
    finally:
        await watchdog.stop()
        await cog.cog_unload()

    assert watchdog.max_gap < 1.0, f"loop blocked {watchdog.max_gap:.3f}s during startup"
    assert watchdog.max_gap < blocking_cost / 4, (
        f"loop blocked {watchdog.max_gap:.3f}s during startup — comparable to the "
        f"{blocking_cost:.3f}s each scan costs, so a scan is still running inline"
    )
