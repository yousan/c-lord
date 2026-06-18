"""Tests for c_lord.transcript.tail — async follower of the active session JSONL."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from c_lord.transcript.tail import tail_events


def _write_event(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


async def _drain(agen, n: int, timeout: float = 2.0) -> list:
    collected: list = []

    async def pull() -> None:
        async for ev in agen:
            collected.append(ev)
            if len(collected) >= n:
                return

    await asyncio.wait_for(pull(), timeout=timeout)
    return collected


async def _collect_for(agen, duration: float) -> list:
    """Collect every event yielded within *duration* seconds (then stop)."""
    collected: list = []

    async def pull() -> None:
        async for ev in agen:
            collected.append(ev)

    task = asyncio.create_task(pull())
    await asyncio.sleep(duration)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return collected


async def test_tail_yields_lines_appended_after_start(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s1.jsonl"
    jsonl.write_text("")  # existing but empty
    os.utime(jsonl, (1, 1))

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def producer() -> None:
        await asyncio.sleep(0.1)
        _write_event(jsonl, {"type": "assistant", "n": 1})
        _write_event(jsonl, {"type": "assistant", "n": 2})

    prod = asyncio.create_task(producer())
    try:
        events = await _drain(agen, 2)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)

    assert [e["n"] for e in events] == [1, 2]


async def test_tail_replays_existing_lines_when_from_start(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s1.jsonl"
    _write_event(jsonl, {"type": "assistant", "n": "a"})
    _write_event(jsonl, {"type": "assistant", "n": "b"})

    agen = tail_events(project, poll_interval=0.05, from_start=True).__aiter__()
    events = await _drain(agen, 2)
    assert [e["n"] for e in events] == ["a", "b"]


async def test_tail_switches_to_newer_jsonl_on_session_change(tmp_path: Path) -> None:
    # Models the /clear case: a new <session-id>.jsonl appears in the project dir
    # with a newer mtime → tail should switch to it without restarting.
    project = tmp_path / "proj"
    project.mkdir()
    jsonl1 = project / "s1.jsonl"
    jsonl1.write_text("")
    os.utime(jsonl1, (1, 1))

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def producer() -> None:
        await asyncio.sleep(0.1)
        _write_event(jsonl1, {"type": "assistant", "session": "s1"})
        os.utime(jsonl1, (100, 100))
        await asyncio.sleep(0.2)
        # Simulate /clear: a new jsonl with newer mtime appears.
        jsonl2 = project / "s2.jsonl"
        _write_event(jsonl2, {"type": "assistant", "session": "s2"})
        os.utime(jsonl2, (200, 200))

    prod = asyncio.create_task(producer())
    try:
        events = await _drain(agen, 2, timeout=3.0)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)

    sessions = [e["session"] for e in events]
    assert sessions == ["s1", "s2"]


async def test_tail_skips_malformed_lines(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s1.jsonl"
    jsonl.write_text("")
    os.utime(jsonl, (1, 1))

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def producer() -> None:
        await asyncio.sleep(0.1)
        with jsonl.open("a") as f:
            f.write("{not valid json\n")
            f.write(json.dumps({"type": "assistant", "ok": True}) + "\n")

    prod = asyncio.create_task(producer())
    try:
        events = await _drain(agen, 1, timeout=2.0)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)

    assert events == [{"type": "assistant", "ok": True}]


async def test_tail_waits_for_jsonl_to_appear(tmp_path: Path) -> None:
    # Project dir exists but no .jsonl yet — Claude Code hasn't started writing.
    project = tmp_path / "proj"
    project.mkdir()

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def producer() -> None:
        await asyncio.sleep(0.15)
        jsonl = project / "s.jsonl"
        _write_event(jsonl, {"type": "assistant", "n": 1})

    prod = asyncio.create_task(producer())
    try:
        events = await _drain(agen, 1, timeout=2.0)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)

    assert events == [{"type": "assistant", "n": 1}]


async def test_tail_handles_truncation_or_rewrite(tmp_path: Path) -> None:
    # Defensive: if the file shrinks (rare, but possible if Claude Code rotates),
    # tail should not crash and should resume from start.
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    _write_event(jsonl, {"type": "assistant", "padding": "x" * 200, "n": 1})
    os.utime(jsonl, (1, 1))

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def producer() -> None:
        await asyncio.sleep(0.15)
        # Rewrite (truncate then write a single line).
        jsonl.write_text(json.dumps({"type": "assistant", "n": 99}) + "\n")
        os.utime(jsonl, (50, 50))

    prod = asyncio.create_task(producer())
    try:
        events = await _drain(agen, 1, timeout=2.0)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)

    assert events[0]["n"] == 99


async def test_tail_does_not_replay_history_on_resume_rewrite(tmp_path: Path) -> None:
    # Issue #433: the real production trigger. A ``--resume`` (after a host crash
    # killed the tmux Claude) makes Claude Code rewrite the active jsonl IN PLACE,
    # PRESERVING the whole history and appending the new turn. The rewrite is
    # smaller than the padded original, so ``size < offset`` fires and tail resets
    # offset to 0 — re-reading the entire history. Tail must NOT re-yield events
    # (by uuid) that already existed when it started: only the genuinely new
    # ``u3`` may be emitted. Without the dedup this yields u1, u2, u3 (the burst).
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    # Existing history (delivered by a previous tail lifetime) with padding so the
    # later rewrite shrinks the file and trips the truncation-reset path.
    _write_event(jsonl, {"type": "assistant", "uuid": "u1", "pad": "x" * 400})
    _write_event(jsonl, {"type": "assistant", "uuid": "u2", "pad": "x" * 400})
    os.utime(jsonl, (1, 1))

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def producer() -> None:
        await asyncio.sleep(0.15)
        # Resume rewrite: history preserved verbatim + new u3, no padding (smaller).
        jsonl.write_text(
            json.dumps({"type": "assistant", "uuid": "u1"})
            + "\n"
            + json.dumps({"type": "assistant", "uuid": "u2"})
            + "\n"
            + json.dumps({"type": "assistant", "uuid": "u3"})
            + "\n"
        )
        os.utime(jsonl, (50, 50))

    prod = asyncio.create_task(producer())
    try:
        events = await _collect_for(agen, 0.5)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)

    assert [e["uuid"] for e in events] == ["u3"]


async def test_tail_does_not_re_yield_already_followed_event_on_rewrite(tmp_path: Path) -> None:
    # A uuid appended *after* tail started (already delivered live) must also not
    # be re-emitted when a subsequent rewrite resets the offset to 0.
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    _write_event(jsonl, {"type": "assistant", "uuid": "base", "pad": "x" * 400})
    os.utime(jsonl, (1, 1))

    agen = tail_events(project, poll_interval=0.05).__aiter__()

    async def producer() -> None:
        await asyncio.sleep(0.15)
        # Live append — delivered now.
        _write_event(jsonl, {"type": "assistant", "uuid": "live"})
        os.utime(jsonl, (40, 40))
        await asyncio.sleep(0.2)
        # Rewrite preserving everything (shrinks → reset to 0).
        jsonl.write_text(
            json.dumps({"type": "assistant", "uuid": "base"})
            + "\n"
            + json.dumps({"type": "assistant", "uuid": "live"})
            + "\n"
            + json.dumps({"type": "assistant", "uuid": "fresh"})
            + "\n"
        )
        os.utime(jsonl, (80, 80))

    prod = asyncio.create_task(producer())
    try:
        events = await _collect_for(agen, 0.7)
    finally:
        prod.cancel()
        await asyncio.gather(prod, return_exceptions=True)

    # "live" is yielded once (when appended), "fresh" once; "base" never; no dup.
    assert [e["uuid"] for e in events] == ["live", "fresh"]
