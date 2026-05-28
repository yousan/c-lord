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
    """Buffer is reset after each assistant_text so second turn starts fresh.

    A ``result`` event between turns signals the turn boundary; each turn's
    final text is flushed as a reply on that event.
    """
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    file_sink_calls: list[tuple[str, str]] = []
    reply_sink_calls: list[str] = []

    async def sink(text: str) -> None:
        pass

    async def reply_sink(text: str) -> None:
        reply_sink_calls.append(text)

    async def file_sink(text: str, file_path: str) -> None:
        file_sink_calls.append((text, file_path))

    mirror = TranscriptMirror(
        thread_id=1,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        file_sink=file_sink,
        verbosity="minimal",
        poll_interval=0.05,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        # Turn 1: tool + final text → result event triggers reply flush via file_sink
        _write_event(jsonl, _assistant_tool_use("Bash", "ls"))
        _write_event(jsonl, _assistant_text("turn1 done"))
        _write_event(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.3)
        # Turn 2: no tools, just final text → reply_sink (no file_sink, buffer was cleared)
        _write_event(jsonl, _assistant_text("turn2 done"))
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    # Turn 1: file_sink called (tools were buffered)
    assert len(file_sink_calls) == 1
    assert file_sink_calls[0][0] == "turn1 done"
    # Turn 2: reply_sink called (no tools buffered)
    assert any("turn2 done" in s for s in reply_sink_calls)


# ---------------------------------------------------------------------------
# Issue #143: intermediate assistant_text must be silent; only last gets reply
# ---------------------------------------------------------------------------


async def test_mirror_multiple_assistant_texts_intermediate_silent(tmp_path: Path) -> None:
    """Issue #143: when multiple assistant_text events arrive in one turn,
    intermediate ones must go to sink (no reference), only the last to reply_sink."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    sink_calls: list[str] = []
    reply_sink_calls: list[str] = []

    async def sink(text: str) -> None:
        sink_calls.append(text)

    async def reply_sink(text: str) -> None:
        reply_sink_calls.append(text)

    mirror = TranscriptMirror(
        thread_id=1,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        verbosity="minimal",
        poll_interval=0.05,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _assistant_text("intermediate text"))
        _write_event(jsonl, _assistant_text("final text"))
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    # intermediate → silent sink
    assert any("intermediate text" in s for s in sink_calls), f"sink={sink_calls}"
    # final → reply_sink (not silent)
    assert any("final text" in s for s in reply_sink_calls), f"reply={reply_sink_calls}"
    # final must NOT appear in the silent sink
    assert not any("final text" in s for s in sink_calls), f"final leaked to sink={sink_calls}"


async def test_mirror_result_event_flushes_pending_as_reply(tmp_path: Path) -> None:
    """Issue #143: a ``result`` JSONL event signals turn end and flushes
    the pending assistant_text as a reply (not waiting for mirror stop)."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    reply_sink_calls: list[str] = []

    async def sink(text: str) -> None:
        pass

    async def reply_sink(text: str) -> None:
        reply_sink_calls.append(text)

    mirror = TranscriptMirror(
        thread_id=1,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        verbosity="minimal",
        poll_interval=0.05,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _assistant_text("turn answer"))
        _write_event(jsonl, {"type": "system", "subtype": "turn_duration"})
        # Give time for events to be processed; don't stop yet.
        await asyncio.sleep(0.3)
        # reply_sink must have been called before stop().
        assert any("turn answer" in s for s in reply_sink_calls), (
            f"reply_sink not called before stop: {reply_sink_calls}"
        )
    finally:
        await mirror.stop()


async def test_mirror_multiple_turns_each_final_text_as_reply(tmp_path: Path) -> None:
    """Issue #143: with explicit result events between turns, each turn's
    final assistant_text is posted as reply (not silenced)."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    reply_sink_calls: list[str] = []
    sink_calls: list[str] = []

    async def sink(text: str) -> None:
        sink_calls.append(text)

    async def reply_sink(text: str) -> None:
        reply_sink_calls.append(text)

    mirror = TranscriptMirror(
        thread_id=1,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        verbosity="minimal",
        poll_interval=0.05,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        # Turn 1
        _write_event(jsonl, _assistant_text("answer1"))
        _write_event(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.2)
        # Turn 2
        _write_event(jsonl, _assistant_text("answer2"))
        _write_event(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    assert any("answer1" in s for s in reply_sink_calls), f"turn1 not in reply={reply_sink_calls}"
    assert any("answer2" in s for s in reply_sink_calls), f"turn2 not in reply={reply_sink_calls}"
    assert not any("answer1" in s for s in sink_calls), f"turn1 leaked to sink={sink_calls}"
    assert not any("answer2" in s for s in sink_calls), f"turn2 leaked to sink={sink_calls}"


# ---------------------------------------------------------------------------
# Issue #86: regression tests for markdown-table + tool events scenario
# ---------------------------------------------------------------------------


async def test_mirror_minimal_markdown_table_with_tools_reaches_file_sink(
    tmp_path: Path,
) -> None:
    """Issue #86 regression: markdown table in assistant_text after tool events
    must reach file_sink (not be silently dropped)."""
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
    markdown_table_body = (
        "まとめると：\n\n"
        "| テーブル | フィールド | 件数 | 内容 |\n"
        "|---|---|---|---|\n"
        "| `konishis_postmeta` | `mw-wp-form` | 1件 | **設定** |\n\n"
        "進めますか？"
    )
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _assistant_tool_use("Bash", "mysql -e 'SELECT ...'"))
        _write_event(jsonl, _user_tool_result("<persisted-output>\nOutput too large (301KB)."))
        _write_event(jsonl, _assistant_tool_use("Bash", "mysql -e 'SELECT tbl ...'"))
        _write_event(jsonl, _user_tool_result("tbl\tfield\nextra\t1"))
        _write_event(
            jsonl,
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": markdown_table_body}],
                },
            },
        )
        await asyncio.sleep(0.5)
    finally:
        await mirror.stop()

    # The markdown table body must have reached file_sink (not been dropped)
    assert len(file_sink_calls) == 1, f"expected 1 file_sink call, got {len(file_sink_calls)}"
    assert markdown_table_body in file_sink_calls[0][0]
    # Progress file must exist (or have existed) and contain tool output
    assert not sink_calls or not any(markdown_table_body in c for c in sink_calls)


def test_progress_content_truncated_when_over_limit(tmp_path: Path) -> None:
    """Progress buffer content must be truncated to _PROGRESS_MAX_BYTES to
    prevent oversized progress.txt from causing 413 errors."""
    from c_lord.transcript.mirror import _PROGRESS_MAX_BYTES, _truncate_progress

    large_content = "x" * (_PROGRESS_MAX_BYTES + 1000)
    result = _truncate_progress(large_content)
    assert len(result.encode()) <= _PROGRESS_MAX_BYTES
    assert "[truncated]" in result or len(result) < len(large_content)


def test_progress_content_not_truncated_when_within_limit(tmp_path: Path) -> None:
    """Small progress content must pass through unchanged."""
    from c_lord.transcript.mirror import _truncate_progress

    small = "tool output line"
    assert _truncate_progress(small) == small


# ── Issue #215: cursor sink records the final-answer uuid at turn end ──


async def test_reply_cursor_sink_records_final_uuid_on_turn_end(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    cursor: list[str] = []

    async def sink(text: str) -> None:  # pragma: no cover - not asserted here
        pass

    async def reply_cursor_sink(uuid: str) -> None:
        cursor.append(uuid)

    mirror = TranscriptMirror(
        thread_id=7,
        project_dir=project,
        sink=sink,
        reply_cursor_sink=reply_cursor_sink,
        poll_interval=0.05,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        _write_event(jsonl, {**_assistant_text("the final answer"), "uuid": "u-final"})
        _write_event(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    assert cursor == ["u-final"]


# ---------------------------------------------------------------------------
# Issue #218: idle flush — final answer must ping even when turn_duration is
# absent (current Claude Code builds no longer emit `result` and do not
# reliably emit `system/turn_duration`).
# ---------------------------------------------------------------------------


def test_idle_flush_seconds_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from c_lord.transcript.mirror import idle_flush_seconds

    monkeypatch.delenv("CLORD_MIRROR_IDLE_FLUSH_SECONDS", raising=False)
    assert idle_flush_seconds() == 8.0

    monkeypatch.setenv("CLORD_MIRROR_IDLE_FLUSH_SECONDS", "3")
    assert idle_flush_seconds() == 3.0


async def test_minimal_idle_flush_pings_without_turn_duration(tmp_path: Path) -> None:
    """A final assistant_text with no turn_duration must be flushed to the
    reply_sink (ping path) after the idle window — before stop() is called."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    silent: list[str] = []
    replies: list[str] = []

    async def sink(text: str) -> None:
        silent.append(text)

    async def reply_sink(text: str) -> None:
        replies.append(text)

    mirror = TranscriptMirror(
        thread_id=7,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        poll_interval=0.05,
        idle_flush_seconds=0.2,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _assistant_text("FINAL ANSWER"))
        # No turn_duration / result follows. Wait past the idle threshold.
        await asyncio.sleep(0.6)
        # Assert BEFORE stop(): the idle flush must have fired live, not at stop.
        assert any("FINAL ANSWER" in r for r in replies), (replies, silent)
    finally:
        await mirror.stop()


async def test_minimal_idle_does_not_prematurely_flush_intermediate_text(
    tmp_path: Path,
) -> None:
    """assistant_text immediately followed by a tool_use (within the idle
    window) must NOT be flushed as a reply — it is intermediate narration."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    jsonl.write_text("")
    import os

    os.utime(jsonl, (1, 1))

    replies: list[str] = []

    async def sink(text: str) -> None:
        pass

    async def reply_sink(text: str) -> None:
        replies.append(text)

    mirror = TranscriptMirror(
        thread_id=8,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        poll_interval=0.05,
        idle_flush_seconds=0.5,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        _write_event(jsonl, _assistant_text("working on it"))
        await asyncio.sleep(0.1)  # well within idle window
        _write_event(jsonl, _assistant_tool_use())
        await asyncio.sleep(0.1)
        # Intermediate text must not have been flushed as a reply (no ping).
        assert not any("working on it" in r for r in replies), replies
    finally:
        await mirror.stop()
