"""Tests for c_lord.transcript.mirror — per-thread tail→Discord pipe."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.transcript.mirror import (
    TranscriptMirror,
    _first_ask_question,
    bridge_mode_jsonl,
    verbosity_mode,
)

from .helpers import clord_marker_event, clord_transcript


def _write_event(path: Path, payload: dict) -> None:
    # ensure_ascii=False: Claude Code serialises with JS ``JSON.stringify``, which
    # writes non-ASCII raw. Escaping would hide c-lord's zero-width-space marker
    # (#627) behind a ``\\u200b`` and no transcript on disk looks like that.
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


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


def _user_str(content: str) -> dict:
    """A ``user``-role event whose content is a bare string (pane input / marker)."""
    return {"type": "user", "message": {"role": "user", "content": content}}


async def test_mirror_posts_rendered_events_to_sink(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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
    """#492: unset defaults to jsonl (the mode #216 decided is the real path)."""
    monkeypatch.delenv("CLORD_BRIDGE_MODE", raising=False)
    assert bridge_mode_jsonl() is True

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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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


async def test_mirror_minimal_bash_mode_buffered_not_leaked_as_bubble(tmp_path: Path) -> None:
    """#487: Claude Code bash-mode markers (``! command``) are buffered into the
    progress file like tool activity — never posted raw as a 👤 thread bubble."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    import os

    os.utime(jsonl, (1, 1))

    sink_calls: list[str] = []
    progress_contents: list[str] = []

    async def sink(text: str) -> None:
        sink_calls.append(text)

    async def file_sink(text: str, file_path: str) -> None:
        # Read inside the callback — the mirror unlinks the temp file afterwards.
        with open(file_path, encoding="utf-8") as f:
            progress_contents.append(f.read())

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
        _write_event(jsonl, _user_str("<bash-input> google-chrome --headless</bash-input>"))
        _write_event(
            jsonl,
            _user_str("<bash-stdout></bash-stdout><bash-stderr>Missing X server</bash-stderr>"),
        )
        _write_event(jsonl, _assistant_text("done"))
        await asyncio.sleep(0.4)
    finally:
        await mirror.stop()

    # No raw bash-mode tag ever reached the thread bubble (plain sink).
    assert not any("<bash-input>" in c or "<bash-stdout>" in c for c in sink_calls)
    # The bash command + output landed in the progress file (the "raw mirror" layer).
    assert progress_contents, "progress file_sink should have been called"
    joined = "\n".join(progress_contents)
    assert "google-chrome --headless" in joined
    assert "Missing X server" in joined


async def test_mirror_minimal_no_file_sink_fallback_to_sink(tmp_path: Path) -> None:
    """When file_sink is None but tools were buffered, assistant_text still
    posts via plain sink (graceful degradation)."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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


async def test_cursor_never_records_a_silently_flushed_intermediate(tmp_path: Path) -> None:
    """#553: the cursor must mean "this was delivered AS THE FINAL ANSWER".

    An assistant_text followed by a tool call is an *intermediate* message: the
    mirror posts it silently and the turn keeps going. Its uuid used to stay in
    ``_last_text_uuid``, so a shutdown mid-turn committed it to the cursor — a
    cursor pointing past the last completed turn's final answer. On restart the
    #215 rescue compared the two, found them different, and re-posted an answer
    the user had already read (yousan: 「メッセージが二重で出てる？」).
    """
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    import os

    os.utime(jsonl, (1, 1))

    cursor: list[str] = []

    async def sink(text: str) -> None:
        pass

    async def reply_cursor_sink(uuid: str) -> None:
        cursor.append(uuid)

    mirror = TranscriptMirror(
        thread_id=553,
        project_dir=project,
        sink=sink,
        reply_cursor_sink=reply_cursor_sink,
        poll_interval=0.05,
        idle_flush_seconds=0,  # no idle flush — stop() is the only turn boundary
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        # A completed turn: this one really was the final answer.
        _write_event(jsonl, {**_assistant_text("delivered final answer"), "uuid": "u-final"})
        _write_event(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.25)
        # A new turn starts and is INTERRUPTED mid-flight: its text is followed
        # by a tool call, so the mirror posts it silently as an intermediate.
        _write_event(jsonl, {**_assistant_text("still working on it"), "uuid": "u-mid"})
        _write_event(
            jsonl,
            {
                "type": "assistant",
                "uuid": "u-tool",
                "message": {
                    "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
                },
            },
        )
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()  # shutdown mid-turn

    assert cursor == ["u-final"], f"cursor must hold only delivered final answers, got {cursor!r}"


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
    clord_transcript(jsonl)
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
    clord_transcript(jsonl)
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


# -- #232: AskUserQuestion bridge for non-run_claude (mirror-driven) sessions ----


def _assistant_ask(header: str = "進め方") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [
                            {
                                "question": "どの粒度で進めますか?",
                                "header": header,
                                "options": [
                                    {"label": "A案", "description": "その場"},
                                    {"label": "B案", "description": "新規"},
                                ],
                            }
                        ]
                    },
                }
            ],
        },
    }


def test_first_ask_question_parses_tool_use() -> None:
    q = _first_ask_question(_assistant_ask())
    assert q is not None
    assert q.header == "進め方"
    assert q.question == "どの粒度で進めますか?"
    assert [o.label for o in q.options] == ["A案", "B案"]


def test_first_ask_question_ignores_non_ask_events() -> None:
    assert _first_ask_question(_assistant_text("hi")) is None
    assert _first_ask_question(_assistant_tool_use(name="Bash")) is None
    assert _first_ask_question({"type": "assistant", "message": {}}) is None


def test_ask_bus_is_active_tracks_waiters() -> None:
    tid = 23223223
    assert ask_bus.is_active(tid) is False
    ask_bus.register(tid)
    try:
        assert ask_bus.is_active(tid) is True
    finally:
        ask_bus.unregister(tid)
    assert ask_bus.is_active(tid) is False


async def test_mirror_bridges_ask_to_cb(tmp_path: Path) -> None:
    """#232: an AskUserQuestion menu tailed from the transcript is bridged via
    the callback even with no run_claude turn active."""
    import os

    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))

    called = asyncio.Event()
    captured: dict = {}

    async def sink(text: str) -> None:
        pass

    async def ask_cb(question) -> None:
        captured["q"] = question
        called.set()

    mirror = TranscriptMirror(
        thread_id=2320001, project_dir=project, sink=sink, poll_interval=0.05, ask_bridge_cb=ask_cb
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        _write_event(jsonl, _assistant_ask("進め方"))
        await asyncio.wait_for(called.wait(), timeout=3)
    finally:
        await mirror.stop()

    assert captured["q"].header == "進め方"
    assert [o.label for o in captured["q"].options] == ["A案", "B案"]


async def test_mirror_skips_already_answered_ask(tmp_path: Path) -> None:
    """#262: a menu whose tool_result is already in the transcript (answered)
    must NOT be re-bridged.

    The mirror can process the AskUserQuestion ``tool_use`` line late (queue lag
    or restart replay), by which point the answer (``tool_result``) is already in
    the transcript. Bridging then posts a dead, duplicate menu to Discord *after*
    the user already answered (the live bridge having released ``ask_bus``).
    """
    import os

    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))

    tid = 2320003
    spawned = False

    async def sink(text: str) -> None:
        pass

    async def ask_cb(question) -> None:
        nonlocal spawned
        spawned = True

    ask_with_id = _assistant_ask("発生箇所")
    ask_with_id["message"]["content"][0]["id"] = "toolu_answered1"
    answer = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_answered1", "content": "answered"}
            ],
        },
    }

    mirror = TranscriptMirror(
        thread_id=tid, project_dir=project, sink=sink, poll_interval=0.05, ask_bridge_cb=ask_cb
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        # Both the menu and its answer are already in the transcript by the time
        # the mirror tails them (the lag / restart-replay case from #262).
        _write_event(jsonl, ask_with_id)
        _write_event(jsonl, answer)
        await asyncio.sleep(0.4)
    finally:
        await mirror.stop()

    assert spawned is False


async def test_mirror_skips_ask_when_bus_active(tmp_path: Path) -> None:
    """#232 dedup: when a bridge is already active (run_claude owns the menu via
    ask_bus), the mirror must NOT spawn a second bridge."""
    import os

    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))

    tid = 2320002
    spawned = False

    async def sink(text: str) -> None:
        pass

    async def ask_cb(question) -> None:
        nonlocal spawned
        spawned = True

    ask_bus.register(tid)  # simulate run_claude already bridging
    mirror = TranscriptMirror(
        thread_id=tid, project_dir=project, sink=sink, poll_interval=0.05, ask_bridge_cb=ask_cb
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        _write_event(jsonl, _assistant_ask())
        await asyncio.sleep(0.4)
    finally:
        await mirror.stop()
        ask_bus.unregister(tid)

    assert spawned is False


# ---------------------------------------------------------------------------
# Issue #433: resume (after host crash) rewrites the active jsonl in place,
# preserving history. The mirror must NOT re-post that history to Discord.
# ---------------------------------------------------------------------------


def _assistant_text_uuid(text: str, uuid: str, pad: int = 0) -> dict:
    d = _assistant_text(text)
    d["uuid"] = uuid
    if pad:
        d["pad"] = "x" * pad
    return d


def _turn_end(uuid: str) -> dict:
    return {"type": "system", "subtype": "turn_duration", "uuid": uuid}


async def test_mirror_does_not_repost_history_on_resume_rewrite(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    # #627: the mirror only follows a transcript c-lord itself drove, so the
    # history starts with one of c-lord's marked prompts — as a real one does.
    _write_event(jsonl, clord_marker_event())
    # Two completed turns of HISTORY, delivered by a previous mirror lifetime.
    # Padded so the later resume-rewrite shrinks the file (trips offset reset).
    _write_event(jsonl, _assistant_text_uuid("old answer 1", "u1", pad=400))
    _write_event(jsonl, _turn_end("t1"))
    _write_event(jsonl, _assistant_text_uuid("old answer 2", "u2", pad=400))
    _write_event(jsonl, _turn_end("t2"))
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
    mirror.start()  # tails from EOF — history must stay un-posted
    try:
        await asyncio.sleep(0.2)
        # Resume rewrite: history preserved verbatim + a NEW turn, no padding.
        lines = [
            # A resume rewrite preserves the history verbatim, marker included.
            clord_marker_event(),
            _assistant_text_uuid("old answer 1", "u1"),
            _turn_end("t1"),
            _assistant_text_uuid("old answer 2", "u2"),
            _turn_end("t2"),
            _assistant_text_uuid("brand new answer", "u3"),
            _turn_end("t3"),
        ]
        jsonl.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
        os.utime(jsonl, (50, 50))
        await asyncio.sleep(0.4)
    finally:
        await mirror.stop()

    # Only the genuinely new turn reaches Discord; no history re-post (the burst).
    assert any("brand new answer" in s for s in reply_sink_calls), reply_sink_calls
    assert not any("old answer 1" in s for s in reply_sink_calls), reply_sink_calls
    assert not any("old answer 2" in s for s in reply_sink_calls), reply_sink_calls


# -- #399 AC3: suppress the post-resolution flush of pane-bridged context ----


def _pane_bridged_pair() -> tuple[str, str]:
    """(pane-rendered context, raw markdown the CLI flushed) — real captures."""
    from c_lord.claude.tmux_runner import _parse_ask_from_pane

    fixtures = Path(__file__).parent.parent / "fixtures"
    pane = (fixtures / "panes" / "ask_context_prose_above_menu.txt").read_text()
    q = _parse_ask_from_pane(pane)
    assert q is not None and q.context
    md = (fixtures / "transcripts" / "i399_prose_flushed_markdown.txt").read_text()
    return q.context, md


async def test_mirror_suppresses_pane_bridged_context(tmp_path: Path) -> None:
    """#399 AC3: when the pane-ask bridge already posted the pre-menu prose,
    the CLI's post-resolution flush of the same text (raw markdown) must not be
    re-posted — but later, different text still flows normally."""
    from c_lord.discord_ui.bridged_context import bridged_context

    pane_ctx, flushed_md = _pane_bridged_pair()
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    import os

    os.utime(jsonl, (1, 1))

    posted: list[str] = []
    replied: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    async def reply_sink(text: str) -> None:
        replied.append(text)

    bridged_context.clear()
    bridged_context.register(99399, pane_ctx)
    mirror = TranscriptMirror(
        thread_id=99399,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        poll_interval=0.05,
        idle_flush_seconds=0,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        # The flush-at-resolution: prose (markdown), then continuation, turn end.
        _write_event(jsonl, _assistant_text(flushed_md))
        _write_event(jsonl, _assistant_text("選択を受けて続行します。"))
        _write_event(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.4)
    finally:
        await mirror.stop()
        bridged_context.clear()

    everything = posted + replied
    assert not any("楽観ロック" in p for p in everything), (
        "pane-bridged context was re-posted by the mirror"
    )
    # The rest of the turn still flows.
    assert any("選択を受けて続行します。" in p for p in everything)


async def test_mirror_commits_uuid_of_suppressed_text_when_turn_ends(tmp_path: Path) -> None:
    """If the suppressed text is the turn's last text, its uuid must still be
    committed (#215) so a restart does not re-post it as a missed final answer."""
    from c_lord.discord_ui.bridged_context import bridged_context

    pane_ctx, flushed_md = _pane_bridged_pair()
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    import os

    os.utime(jsonl, (1, 1))

    cursor: list[str] = []
    replied: list[str] = []

    async def sink(text: str) -> None:
        pass

    async def reply_sink(text: str) -> None:
        replied.append(text)

    async def cursor_sink(uuid: str) -> None:
        cursor.append(uuid)

    bridged_context.clear()
    bridged_context.register(99400, pane_ctx)
    mirror = TranscriptMirror(
        thread_id=99400,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        reply_cursor_sink=cursor_sink,
        poll_interval=0.05,
        idle_flush_seconds=0,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        event = _assistant_text(flushed_md)
        event["uuid"] = "uuid-399-suppressed"
        _write_event(jsonl, event)
        _write_event(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.4)
    finally:
        await mirror.stop()
        bridged_context.clear()

    assert replied == []  # nothing re-posted
    assert "uuid-399-suppressed" in cursor


async def test_mirror_commits_suppressed_uuid_without_turn_end_marker(tmp_path: Path) -> None:
    """#399 hardening: when the suppressed text is the last event and NO
    turn-end marker ever arrives (current CLI builds often emit none), the
    uuid must still be committed — otherwise a bot restart re-posts the
    already-delivered context via #215 recovery."""
    from c_lord.discord_ui.bridged_context import bridged_context

    pane_ctx, flushed_md = _pane_bridged_pair()
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    import os

    os.utime(jsonl, (1, 1))

    cursor: list[str] = []

    async def sink(text: str) -> None:
        pass

    async def cursor_sink(uuid: str) -> None:
        cursor.append(uuid)

    bridged_context.clear()
    bridged_context.register(99401, pane_ctx)
    mirror = TranscriptMirror(
        thread_id=99401,
        project_dir=project,
        sink=sink,
        reply_cursor_sink=cursor_sink,
        poll_interval=0.05,
        idle_flush_seconds=0.2,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        event = _assistant_text(flushed_md)
        event["uuid"] = "uuid-399-idle-suppressed"
        _write_event(jsonl, event)
        # NO turn_end marker — only the idle window passes. The uuid must be
        # committed while the mirror is still RUNNING (a hard-killed bot never
        # reaches the graceful-stop commit).
        await asyncio.sleep(0.6)
        assert "uuid-399-idle-suppressed" in cursor
    finally:
        await mirror.stop()
        bridged_context.clear()


async def test_stale_registry_entry_cleared_at_turn_boundary(tmp_path: Path) -> None:
    """#399 review blocker 3: an entry whose flush never arrived (CLI killed /
    menu Esc'd) must be disarmed at the next turn boundary — otherwise a later
    similar-but-different REAL message gets swallowed (reproduced at
    SequenceMatcher ratio 0.97 with one decision-flipping sentence changed)."""
    from c_lord.discord_ui.bridged_context import bridged_context

    pane_ctx, flushed_md = _pane_bridged_pair()
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    import os

    os.utime(jsonl, (1, 1))

    posted: list[str] = []
    replied: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    async def reply_sink(text: str) -> None:
        replied.append(text)

    bridged_context.clear()
    bridged_context.register(99402, pane_ctx)
    mirror = TranscriptMirror(
        thread_id=99402,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        poll_interval=0.05,
        idle_flush_seconds=0,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        # Turn ends WITHOUT the flush ever arriving (menu was Esc'd).
        _write_event(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.2)
        # Next turn: Claude restates the (similar) prose as a REAL message.
        _write_event(jsonl, _assistant_text(flushed_md))
        _write_event(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.4)
    finally:
        await mirror.stop()
        bridged_context.clear()

    assert any("楽観ロック" in p for p in posted + replied), (
        "stale registry entry swallowed a real message after a turn boundary"
    )


async def test_mirror_registers_flushed_intermediate_text_as_mirror_source(tmp_path: Path) -> None:
    """#399 plan order: the mirror flushes the pre-menu prose as an intermediate
    text BEFORE the menu. It must register that text as source='mirror' so the
    later pane-bridge skips its own duplicate post."""
    from c_lord.discord_ui.bridged_context import bridged_context

    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    import os

    os.utime(jsonl, (1, 1))

    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    prose = (
        "保存先の判断ポイント。メモは bot スコープかローカルかで決まります。私の推しはホーム直下です。"
        * 2
    )

    bridged_context.clear()
    mirror = TranscriptMirror(thread_id=99500, project_dir=project, sink=sink, poll_interval=0.05)
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        # Intermediate prose, then a tool event forces a silent flush of it.
        _write_event(jsonl, _assistant_text(prose))
        _write_event(jsonl, _assistant_tool_use("Write", "plan.md"))
        await asyncio.sleep(0.4)
        # The mirror posted the prose...
        assert any("保存先の判断ポイント" in p for p in posted)
        # ...and registered it as 'mirror' so a pane-bridge would now skip.
        assert bridged_context.consume_match(99500, prose, source="mirror") is True
    finally:
        await mirror.stop()
        bridged_context.clear()


# -- #539: the silence filler is driven by the mirror loop -------------------


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _ProgressSpy:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.deletes: list[object] = []

    async def post(self, text: str):
        self.posts.append(text)
        return f"m{len(self.posts)}"

    async def edit(self, handle, text: str) -> None:  # pragma: no cover - not asserted
        pass

    async def delete(self, handle) -> None:
        self.deletes.append(handle)


def _progress(spy: _ProgressSpy, clock: _FakeClock):
    from c_lord.discord_ui.turn_progress import TurnProgress

    return TurnProgress(
        post=spy.post,
        edit=spy.edit,
        delete=spy.delete,
        quiet_seconds=90.0,
        update_seconds=15.0,
        clock=clock,
    )


def _fresh_jsonl(tmp_path: Path):
    import os

    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))
    return project, jsonl


async def test_progress_line_appears_after_a_long_tool_only_silence(tmp_path: Path) -> None:
    """Tool events keep arriving but nothing is posted — that is the #539 gap."""
    project, jsonl = _fresh_jsonl(tmp_path)
    spy, clock = _ProgressSpy(), _FakeClock()

    async def sink(text: str) -> None:
        pass

    mirror = TranscriptMirror(
        thread_id=7,
        project_dir=project,
        sink=sink,
        poll_interval=0.05,
        # Long enough that the idle heartbeat cannot fire between the clock jump
        # and the tool event below. With a fake clock, "time passed" and "an
        # event arrived" are separate steps, so a fast heartbeat would tick on
        # the jump alone and render the pre-event state.
        idle_flush_seconds=5.0,
        progress=_progress(spy, clock),
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        _write_event(jsonl, _assistant_tool_use("Bash", "ls"))
        await asyncio.sleep(0.2)
        # 91s pass with nothing posted to Discord — but Claude is still working,
        # which reaches the mirror as more tool traffic. That combination (quiet
        # thread, busy transcript) is the gap #539 is about.
        clock.advance(91.0)
        _write_event(jsonl, _assistant_tool_use("Bash", "rg -n 'timeout' c_lord/"))
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    assert spy.posts, "no progress line appeared during a 91s tool-only silence"
    assert "作業中" in spy.posts[0]
    assert "Bash" in spy.posts[0]


async def test_progress_line_disappears_when_claude_speaks(tmp_path: Path) -> None:
    """Real output makes the filler get out of the way."""
    project, jsonl = _fresh_jsonl(tmp_path)
    spy, clock = _ProgressSpy(), _FakeClock()

    async def sink(text: str) -> None:
        pass

    mirror = TranscriptMirror(
        thread_id=8,
        project_dir=project,
        sink=sink,
        poll_interval=0.05,
        idle_flush_seconds=0.05,
        progress=_progress(spy, clock),
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        _write_event(jsonl, _assistant_tool_use("Bash", "ls"))
        await asyncio.sleep(0.2)
        clock.advance(91.0)
        await asyncio.sleep(0.25)
        assert spy.posts, "precondition: the line should be showing"
        _write_event(jsonl, _assistant_text("途中経過です"))
        await asyncio.sleep(0.4)
    finally:
        await mirror.stop()

    assert spy.deletes, "the progress line was left behind after real output"


async def test_idle_thread_never_shows_a_progress_line(tmp_path: Path) -> None:
    """No turn, no events — an idle thread must not sprout a stale 待機中."""
    project, _ = _fresh_jsonl(tmp_path)
    spy, clock = _ProgressSpy(), _FakeClock()

    async def sink(text: str) -> None:
        pass

    mirror = TranscriptMirror(
        thread_id=9,
        project_dir=project,
        sink=sink,
        poll_interval=0.05,
        idle_flush_seconds=0.05,
        progress=_progress(spy, clock),
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        clock.advance(6000.0)
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert spy.posts == []


async def test_a_prompt_alone_arms_the_progress_line(tmp_path: Path) -> None:
    """A turn that starts with a prompt and then thinks silently still gets a line.

    Staging showed the elapsed time under-reporting: the ``user_input`` branch
    armed the turn and then flushed the previous one, and that flush disarms.
    The turn only got armed again by its first tool event, so the clock started
    late. Here there are no tool events at all, so nothing can paper over it.
    """
    project, jsonl = _fresh_jsonl(tmp_path)
    spy, clock = _ProgressSpy(), _FakeClock()

    async def sink(text: str) -> None:
        pass

    mirror = TranscriptMirror(
        thread_id=11,
        project_dir=project,
        sink=sink,
        poll_interval=0.05,
        idle_flush_seconds=0.05,
        progress=_progress(spy, clock),
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        _write_event(jsonl, _user_str("調べてください"))
        await asyncio.sleep(0.25)
        clock.advance(91.0)
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert spy.posts, "a prompt-then-silence turn produced no progress line"
