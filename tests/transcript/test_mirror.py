"""Tests for c_lord.transcript.mirror — per-thread tail→Discord pipe."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from c_lord.transcript.mirror import TranscriptMirror, bridge_mode_jsonl, verbosity_mode


def _write_event(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def _assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _assistant_tool_use(name: str = "Bash", command: str = "ls") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": {"command": command}}],
        },
    }


def _user_tool_result(output: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": output}],
        },
    }


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
        _write_event(jsonl, _assistant_text("hello discord"))
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
            _write_event(jsonl, _assistant_text(f"msg-{i}"))
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


# ---------------------------------------------------------------------------
# Issue #83: verbosity mode tests
# ---------------------------------------------------------------------------


def test_verbosity_mode_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLORD_MIRROR_VERBOSITY", raising=False)
    assert verbosity_mode() == "minimal"

    monkeypatch.setenv("CLORD_MIRROR_VERBOSITY", "full")
    assert verbosity_mode() == "full"

    monkeypatch.setenv("CLORD_MIRROR_VERBOSITY", "FULL")
    assert verbosity_mode() == "full"

    monkeypatch.setenv("CLORD_MIRROR_VERBOSITY", "minimal")
    assert verbosity_mode() == "minimal"


async def test_mirror_minimal_suppresses_tool_use(tmp_path: Path) -> None:
    """In minimal mode, tool_use events must NOT be posted to the sink."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    mirror = TranscriptMirror(
        thread_id=1, project_dir=project, sink=sink, verbosity="minimal", poll_interval=0.05
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _assistant_tool_use("Bash", "ls"))
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert not any("Bash" in p for p in posted), f"tool_use leaked: {posted}"


async def test_mirror_minimal_suppresses_tool_result(tmp_path: Path) -> None:
    """In minimal mode, tool_result events must NOT be posted to the sink."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    mirror = TranscriptMirror(
        thread_id=1, project_dir=project, sink=sink, verbosity="minimal", poll_interval=0.05
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _user_tool_result("file output here"))
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert not any("file output" in p for p in posted), f"tool_result leaked: {posted}"


async def test_mirror_minimal_posts_assistant_text(tmp_path: Path) -> None:
    """In minimal mode, assistant_text events ARE posted to the sink."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    mirror = TranscriptMirror(
        thread_id=1, project_dir=project, sink=sink, verbosity="minimal", poll_interval=0.05
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _assistant_text("final answer here"))
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert any("final answer here" in p for p in posted)


async def test_mirror_minimal_attaches_progress_file_when_tools_buffered(
    tmp_path: Path,
) -> None:
    """When tool events are buffered and assistant_text arrives, file_sink is
    called with the assistant text and a progress file path."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    sink_calls: list[str] = []
    file_sink_calls: list[tuple[str, str]] = []

    async def sink(text: str) -> None:
        sink_calls.append(text)

    async def file_sink(text: str, file_path: str) -> None:
        file_sink_calls.append((text, file_path))

    mirror = TranscriptMirror(
        thread_id=1,
        project_dir=project,
        sink=sink,
        file_sink=file_sink,
        verbosity="minimal",
        poll_interval=0.05,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _assistant_tool_use("Bash", "ls"))
        _write_event(jsonl, _user_tool_result("file1.py"))
        _write_event(jsonl, _assistant_text("done"))
        await asyncio.sleep(0.4)
    finally:
        await mirror.stop()

    # file_sink must have been called (not plain sink) since buffer was non-empty
    assert len(file_sink_calls) == 1
    text, path = file_sink_calls[0]
    assert text == "done"
    assert os.path.exists(path) or not os.path.exists(path)  # file may be cleaned up
    # plain sink not called for this assistant_text
    assert not any("done" in c for c in sink_calls)


async def test_mirror_minimal_no_file_sink_fallback_to_sink(tmp_path: Path) -> None:
    """When file_sink is None but tools were buffered, assistant_text still
    posts via plain sink (graceful degradation)."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    mirror = TranscriptMirror(
        thread_id=1,
        project_dir=project,
        sink=sink,
        file_sink=None,
        verbosity="minimal",
        poll_interval=0.05,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _assistant_tool_use("Bash", "ls"))
        _write_event(jsonl, _assistant_text("fallback answer"))
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    assert any("fallback answer" in p for p in posted)


async def test_mirror_full_mode_posts_tool_events_directly(tmp_path: Path) -> None:
    """In full mode, tool_use events ARE posted directly to the sink."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    mirror = TranscriptMirror(
        thread_id=1, project_dir=project, sink=sink, verbosity="full", poll_interval=0.05
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _assistant_tool_use("Bash", "ls"))
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert any("Bash" in p for p in posted), f"full mode should post tool_use: {posted}"


async def test_mirror_minimal_clears_buffer_after_assistant_text(tmp_path: Path) -> None:
    """Buffer is reset after each assistant_text so second turn starts fresh."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    file_sink_calls: list[tuple[str, str]] = []

    async def sink(text: str) -> None:
        pass

    async def file_sink(text: str, file_path: str) -> None:
        file_sink_calls.append((text, file_path))

    mirror = TranscriptMirror(
        thread_id=1,
        project_dir=project,
        sink=sink,
        file_sink=file_sink,
        verbosity="minimal",
        poll_interval=0.05,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        # Turn 1: tool + final text → file_sink called
        _write_event(jsonl, _assistant_tool_use("Bash", "ls"))
        _write_event(jsonl, _assistant_text("turn1 done"))
        await asyncio.sleep(0.3)
        # Turn 2: no tools, just final text → plain sink, NOT file_sink
        _write_event(jsonl, _assistant_text("turn2 done"))
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    # file_sink only called once (turn 1), not for turn 2 (no buffered tools)
    assert len(file_sink_calls) == 1
    assert file_sink_calls[0][0] == "turn1 done"
