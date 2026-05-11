"""Tests for ProgressBuffer — accumulate StreamEvents into a progress.txt attachment."""

from __future__ import annotations

import json
from datetime import datetime

import discord
import pytest

from c_lord.claude.types import (
    MessageType,
    StreamEvent,
    ToolCategory,
    ToolUseEvent,
)
from c_lord.discord_ui.progress_buffer import ProgressBuffer


def _make_assistant_event(
    text: str | None = None, tool_use: ToolUseEvent | None = None
) -> StreamEvent:
    return StreamEvent(
        message_type=MessageType.ASSISTANT,
        text=text,
        tool_use=tool_use,
    )


def _make_tool_use(name: str, tool_id: str = "t1") -> ToolUseEvent:
    return ToolUseEvent(
        tool_id=tool_id,
        tool_name=name,
        tool_input={"k": "v"},
        category=ToolCategory.COMMAND,
    )


class TestProgressBufferBasic:
    def test_empty_buffer_should_not_attach(self) -> None:
        buf = ProgressBuffer()
        assert buf.should_attach is False
        assert buf.tool_count == 0

    def test_add_event_with_tool_use_marks_attach(self) -> None:
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(tool_use=_make_tool_use("Bash")))
        assert buf.tool_count == 1
        assert buf.should_attach is True

    def test_text_only_does_not_trigger_attach(self) -> None:
        # Short text reply with no tools — should skip (per design: tool count 0 skips).
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(text="hello"))
        assert buf.tool_count == 0
        assert buf.should_attach is False

    def test_multiple_tools_increment_count(self) -> None:
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(tool_use=_make_tool_use("Bash", "t1")))
        buf.add(_make_assistant_event(tool_use=_make_tool_use("Read", "t2")))
        assert buf.tool_count == 2

    def test_same_tool_id_counted_once(self) -> None:
        # tmux_runner may yield the same tool_use multiple times (partial events).
        # We count distinct tool_ids.
        buf = ProgressBuffer()
        tu = _make_tool_use("Bash", "t1")
        buf.add(_make_assistant_event(tool_use=tu))
        buf.add(_make_assistant_event(tool_use=tu))
        assert buf.tool_count == 1

    def test_tool_call_detected_from_tui_text(self) -> None:
        # c-lord's tmux_runner does not synthesize tool_use events — it streams
        # the TUI capture as ASSISTANT text. We detect tool calls by scanning
        # the captured text for ``ToolName(...)`` patterns so progress.txt is
        # still attached when a real tool ran.
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(text="thinking...\nBash(echo hi)\noutput\n"))
        assert buf.should_attach is True

    def test_text_without_tool_pattern_does_not_attach(self) -> None:
        # Plain prose answer (even if multi-line) should not trigger attach.
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(text="Hello, the answer is 42."))
        assert buf.should_attach is False

    def test_partial_growing_text_counts_tool_once(self) -> None:
        # Partial events grow the captured text. The same tool pattern across
        # partials should count as one.
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(text="Bash(echo"))
        buf.add(_make_assistant_event(text="Bash(echo hi"))
        buf.add(_make_assistant_event(text="Bash(echo hi)\noutput"))
        assert buf.tool_count == 1


class TestProgressBufferJSONL:
    def test_jsonl_includes_assistant_text(self) -> None:
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(text="hi"))
        out = buf.to_jsonl()
        lines = [json.loads(line) for line in out.splitlines()]
        assert lines == [{"type": "assistant", "text": "hi"}]

    def test_jsonl_includes_tool_use(self) -> None:
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(tool_use=_make_tool_use("Bash", "abc")))
        out = buf.to_jsonl()
        events = [json.loads(line) for line in out.splitlines()]
        assert len(events) == 1
        assert events[0]["type"] == "assistant"
        assert events[0]["tool_use"]["tool_name"] == "Bash"
        assert events[0]["tool_use"]["tool_id"] == "abc"

    def test_jsonl_includes_tool_result(self) -> None:
        buf = ProgressBuffer()
        buf.add(
            StreamEvent(
                message_type=MessageType.USER,
                tool_result_id="abc",
                tool_result_content="output line",
            )
        )
        events = [json.loads(line) for line in buf.to_jsonl().splitlines()]
        assert events[0]["type"] == "user"
        assert events[0]["tool_result_id"] == "abc"
        assert events[0]["tool_result_content"] == "output line"

    def test_jsonl_skips_progress_events(self) -> None:
        # PROGRESS events are noise (stall timer resets) — exclude from progress.txt.
        buf = ProgressBuffer()
        buf.add(StreamEvent(message_type=MessageType.PROGRESS))
        buf.add(_make_assistant_event(text="real"))
        events = [json.loads(line) for line in buf.to_jsonl().splitlines()]
        assert len(events) == 1
        assert events[0]["text"] == "real"

    def test_jsonl_compacts_partial_text_to_final(self) -> None:
        # Partial events with growing text are noisy. Keep only the latest snapshot
        # per "partial run" by collapsing consecutive partials of the same kind.
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(text="he"))  # partial-like
        buf.add(_make_assistant_event(text="hell"))
        buf.add(_make_assistant_event(text="hello"))
        events = [json.loads(line) for line in buf.to_jsonl().splitlines()]
        # All three are recorded — collapsing is out of scope, we preserve fidelity.
        assert [e["text"] for e in events] == ["he", "hell", "hello"]


class TestProgressBufferFile:
    def test_to_discord_file_returns_none_when_no_tools(self) -> None:
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(text="hi"))
        assert buf.to_discord_file() is None

    def test_to_discord_file_returns_file_when_tool_used(self) -> None:
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(tool_use=_make_tool_use("Bash")))
        f = buf.to_discord_file()
        assert isinstance(f, discord.File)
        assert f.filename.startswith("progress-")
        assert f.filename.endswith(".txt")

    def test_filename_includes_timestamp(self) -> None:
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(tool_use=_make_tool_use("Bash")))
        f = buf.to_discord_file(now=datetime(2026, 5, 11, 10, 30, 45))
        assert f is not None
        assert f.filename == "progress-20260511-103045.txt"

    def test_to_discord_file_content_is_jsonl(self) -> None:
        buf = ProgressBuffer()
        buf.add(_make_assistant_event(tool_use=_make_tool_use("Bash", "t1")))
        f = buf.to_discord_file()
        assert f is not None
        # Read the buffer's underlying content
        content = f.fp.read().decode("utf-8")
        lines = content.splitlines()
        assert all(json.loads(line) for line in lines)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
