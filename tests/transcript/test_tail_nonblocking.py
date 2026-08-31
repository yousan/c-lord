"""Issue #537 (reopened): the tail *follow loop* must not block the event loop.

The first round of #537 moved the two **startup** scans off the loop
(``last_completed_final_answer`` and ``_seed_seen_uuids``) and the symptom went
away for three days.  It came back on 2026-08-29 — 26 consecutive
``heartbeat blocked`` warnings, backing off to 210 s, and two of yousan's
messages vanished for ~29 h — because the fix never reached the loop the tail
spends its whole life in.

``tail_events`` polls twice a second, per mirror, and production runs 252-255
mirrors at once.  Every poll did, **on the event loop**:

* ``latest_session_jsonl`` — ``glob`` + ``stat`` of every ``*.jsonl`` in the
  project dir (measured on the prod host: 2.9 ms for a 182-file dir, 28.9 ms
  for a 2481-file one — i.e. 57.8 ms of loop time per second from one mirror);
* ``stat`` of the active file;
* ``open().read()`` of everything appended since the last poll — and, whenever
  the mtime-latest file changed, of the **entire** file from byte 0 (measured:
  2.07 s for the 109 MB transcript on this host);
* ``json.loads`` of every line read.

These tests pin the property the first round missed: the follow loop's blocking
work happens in a worker thread, so no number of mirrors can starve the Discord
gateway heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

from c_lord.transcript import resolver as resolver_mod
from c_lord.transcript import tail as tail_mod
from c_lord.transcript.tail import tail_events


def _write_event(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


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


async def _drain(agen, n: int, timeout: float = 3.0) -> list:
    collected: list = []

    async def pull() -> None:
        async for ev in agen:
            collected.append(ev)
            if len(collected) >= n:
                return

    await asyncio.wait_for(pull(), timeout=timeout)
    return collected


# ── AC-a: no blocking primitive of the follow loop runs on the loop thread ──


async def test_follow_loop_resolves_the_active_jsonl_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-a: ``latest_session_jsonl`` (glob + stat of every jsonl) must not run
    on the event loop.  ``tail.py:128`` called it inline, 2x/second, per mirror
    — 14 of the 23 production heartbeat-block tracebacks were caught inside it.
    """
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    os.utime(jsonl, (1, 1))

    loop_thread = threading.get_ident()
    ran_on: list[int] = []
    real = tail_mod.latest_session_jsonl

    def spy(project_dir: Path):
        ran_on.append(threading.get_ident())
        return real(project_dir)

    monkeypatch.setattr(tail_mod, "latest_session_jsonl", spy)

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def producer() -> None:
        await asyncio.sleep(0.1)
        _write_event(jsonl, {"type": "assistant", "uuid": "u1"})

    prod = asyncio.create_task(producer())
    try:
        await _drain(agen, 1)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)
        await agen.aclose()

    assert ran_on, "the active-file resolution never ran"
    assert loop_thread not in ran_on, (
        "latest_session_jsonl ran on the event loop thread — this is the glob+stat "
        "that the production heartbeat-block tracebacks were caught inside"
    )


async def test_follow_loop_reads_and_parses_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-a: the ``open().read()`` + ``json.loads`` of appended bytes must not
    run on the event loop either.  A file switch makes that read the *whole*
    file — 2.07 s of solid loop block for the 109 MB prod transcript.
    """
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    os.utime(jsonl, (1, 1))

    loop_thread = threading.get_ident()
    ran_on: list[int] = []
    real_loads = tail_mod.json.loads

    def spy(*args, **kwargs):
        ran_on.append(threading.get_ident())
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(tail_mod.json, "loads", spy)

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def producer() -> None:
        await asyncio.sleep(0.1)
        _write_event(jsonl, {"type": "assistant", "uuid": "u1"})

    prod = asyncio.create_task(producer())
    try:
        await _drain(agen, 1)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)
        await agen.aclose()

    assert ran_on, "nothing was parsed"
    assert loop_thread not in ran_on, "json.loads of tailed lines ran on the event loop thread"


# ── AC-c: regression — a slow poll cycle does not stall other coroutines ────


async def test_a_slow_poll_cycle_does_not_stall_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-c: while one poll cycle is in flight, other coroutines keep running.

    The cost is injected into ``latest_session_jsonl`` as a plain ``sleep`` of a
    *known* size, so the assertion measures **where** the work runs rather than
    how fast the machine is: run inline it stalls the loop for its full cost;
    handed to a worker thread it costs the loop nothing.
    """
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    _write_event(jsonl, {"type": "assistant", "uuid": "seed"})
    os.utime(jsonl, (1, 1))

    poll_cost = 0.3  # stands in for glob+stat of a 2481-file project dir
    real = tail_mod.latest_session_jsonl

    def slow(project_dir: Path):
        time.sleep(poll_cost)
        return real(project_dir)

    monkeypatch.setattr(tail_mod, "latest_session_jsonl", slow)

    agen = tail_events(project, poll_interval=0.01).__aiter__()
    watchdog = _LoopWatchdog()

    async def producer() -> None:
        await asyncio.sleep(poll_cost * 1.5)
        _write_event(jsonl, {"type": "assistant", "uuid": "u1"})

    prod = asyncio.create_task(producer())
    watchdog.start()
    await asyncio.sleep(0.02)  # let the watchdog take its first sample
    try:
        events = await _drain(agen, 1, timeout=10.0)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)
        await watchdog.stop()
        await agen.aclose()

    assert [e["uuid"] for e in events] == ["u1"]
    assert watchdog.max_gap < poll_cost / 3, (
        f"loop blocked {watchdog.max_gap:.3f}s — comparable to the {poll_cost:.3f}s a "
        f"poll cycle costs, so the cycle is still running inline on the event loop"
    )


# ── AC-b (unit scale): many concurrent mirrors keep the loop responsive ─────


async def test_many_concurrent_tails_keep_the_event_loop_responsive(
    tmp_path: Path,
) -> None:
    """AC-b at unit scale: production runs 252-255 mirrors on one loop.

    Each project dir here holds enough jsonl files that the per-poll
    ``glob``+``stat`` is not free, which is exactly the shape that saturated the
    loop in production.  The watchdog must still be serviced.
    """
    mirrors = 60
    files_per_dir = 40
    projects: list[Path] = []
    for i in range(mirrors):
        project = tmp_path / f"p{i}"
        project.mkdir()
        for j in range(files_per_dir):
            decoy = project / f"other-{j}.jsonl"
            decoy.write_text("")
            os.utime(decoy, (1 + j, 1 + j))
        active = project / "active.jsonl"
        active.write_text("")
        os.utime(active, (1000, 1000))
        projects.append(project)

    agens = [tail_events(p, poll_interval=0.02).__aiter__() for p in projects]

    async def consume(agen) -> None:
        async for _ in agen:
            pass

    consumers = [asyncio.create_task(consume(a)) for a in agens]
    watchdog = _LoopWatchdog()
    watchdog.start()
    try:
        await asyncio.sleep(1.0)
    finally:
        await watchdog.stop()
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        for agen in agens:
            await agen.aclose()

    # The Discord heartbeat warning fires at 10 s; 0.25 s is a wide margin that
    # still fails loudly if 60 tails are polling inline.
    assert watchdog.max_gap < 0.25, (
        f"loop blocked {watchdog.max_gap:.3f}s with {mirrors} concurrent tails — "
        "production runs 4x this many"
    )


# ── The tail's thread pool must not starve unrelated to_thread work ─────────


async def test_tail_polling_does_not_starve_unrelated_to_thread_calls(
    tmp_path: Path,
) -> None:
    """250 pollers must not queue up in front of the default executor.

    ``asyncio.to_thread`` shares one bounded pool with the rest of c-lord (git
    checkouts, the #215 recovery scans, ``_transcript_has_ask_result``).  If the
    tail's 500 polls/second land in that pool, those calls wait behind them, so
    the tail gets a pool of its own.
    """
    mirrors = 60
    projects: list[Path] = []
    for i in range(mirrors):
        project = tmp_path / f"p{i}"
        project.mkdir()
        active = project / "active.jsonl"
        active.write_text("")
        os.utime(active, (1000, 1000))
        projects.append(project)

    agens = [tail_events(p, poll_interval=0.005).__aiter__() for p in projects]

    async def consume(agen) -> None:
        async for _ in agen:
            pass

    consumers = [asyncio.create_task(consume(a)) for a in agens]
    try:
        await asyncio.sleep(0.2)  # let the pollers get going
        started = time.perf_counter()
        await asyncio.to_thread(lambda: None)
        waited = time.perf_counter() - started
    finally:
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        for agen in agens:
            await agen.aclose()

    assert waited < 0.5, (
        f"an unrelated asyncio.to_thread call waited {waited:.3f}s behind the tail "
        "pollers — the tail must not share the default executor"
    )


# ── The tail executor is bounded and named (so it is visible in a stack) ────


def test_tail_executor_is_bounded_and_named() -> None:
    """The pool is bounded: an unbounded one would spawn a thread per mirror."""
    executor = tail_mod._tail_executor()
    assert executor is tail_mod._tail_executor(), "executor must be a singleton"
    assert executor._max_workers >= 1
    assert executor._max_workers <= 32, "an unbounded pool is one thread per mirror"
    assert "clord-tail" in (executor._thread_name_prefix or "")


def test_tail_worker_count_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators can size the pool for their host without a code change."""
    monkeypatch.setattr(tail_mod, "_TAIL_EXECUTOR", None)
    monkeypatch.setenv("CLORD_TAIL_WORKERS", "3")
    try:
        assert tail_mod._tail_executor()._max_workers == 3
    finally:
        tail_mod._tail_executor().shutdown(wait=False)
        monkeypatch.setattr(tail_mod, "_TAIL_EXECUTOR", None)


# ── Behaviour is unchanged: the resolver is still the one source of truth ───


def test_resolver_is_still_the_single_active_file_rule() -> None:
    """The follow loop must keep using the shared resolver, not its own copy.

    #627 changes how the active jsonl is chosen; that change has to land in one
    place, so the loop is not allowed to inline its own glob.
    """
    assert tail_mod.latest_session_jsonl is resolver_mod.latest_session_jsonl
