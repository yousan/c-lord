"""Tests for c_lord.transcript.mirror — per-thread tail→Discord pipe."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from c_lord.transcript.mirror import TranscriptMirror, bridge_mode_jsonl


def _write_event(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


async def test_mirror_posts_rendered_events_to_sink(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    mirror = TranscriptMirror(thread_id=42, project_dir=project, sink=sink, poll_interval=0.05)
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        _write_event(
            jsonl,
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello discord"}],
                },
            },
        )
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    assert any("hello discord" in p for p in posted)


async def test_mirror_skips_thinking_and_framing(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    mirror = TranscriptMirror(thread_id=1, project_dir=project, sink=sink, poll_interval=0.05)
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        _write_event(
            jsonl,
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "secret"}],
                },
            },
        )
        _write_event(jsonl, {"type": "ai-title", "aiTitle": "x"})
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert not posted, posted


async def test_mirror_stop_is_idempotent_and_quiet(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()

    async def sink(text: str) -> None:
        pass

    mirror = TranscriptMirror(thread_id=1, project_dir=project, sink=sink)
    # stop() before start() must not raise.
    await mirror.stop()
    mirror.start()
    await mirror.stop()
    await mirror.stop()


async def test_mirror_sink_errors_do_not_kill_task(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    calls = 0

    async def flaky_sink(text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient discord error")

    mirror = TranscriptMirror(thread_id=1, project_dir=project, sink=flaky_sink, poll_interval=0.05)
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        for i in range(2):
            _write_event(
                jsonl,
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"msg-{i}"}],
                    },
                },
            )
            await asyncio.sleep(0.1)
    finally:
        await mirror.stop()

    # Both events attempted; second one succeeded after first raised.
    assert calls >= 2


def test_bridge_mode_jsonl_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLORD_BRIDGE_MODE", raising=False)
    assert bridge_mode_jsonl() is False

    monkeypatch.setenv("CLORD_BRIDGE_MODE", "skill")
    assert bridge_mode_jsonl() is False

    monkeypatch.setenv("CLORD_BRIDGE_MODE", "jsonl")
    assert bridge_mode_jsonl() is True

    monkeypatch.setenv("CLORD_BRIDGE_MODE", "JSONL")
    assert bridge_mode_jsonl() is True
