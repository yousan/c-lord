"""Tests for c_lord.transcript.formatter — JSONL event → Discord-bound text."""

from __future__ import annotations

from c_lord.transcript.formatter import ZWSP_MARKER, render_event


def _assistant(blocks: list[dict]) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _user(content) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}}


def test_assistant_text_block_is_mirrored() -> None:
    ev = _assistant([{"type": "text", "text": "hello world"}])
    out = render_event(ev)
    assert out is not None
    assert out.body == "hello world"
    assert out.kind == "assistant_text"


def test_assistant_tool_use_bash_shows_command() -> None:
    ev = _assistant([{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}])
    out = render_event(ev)
    assert out is not None
    assert "Bash" in out.body and "ls -la" in out.body
    assert out.kind == "tool_use"


def test_assistant_tool_use_read_shows_path() -> None:
    ev = _assistant([{"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/hosts"}}])
    out = render_event(ev)
    assert out is not None
    assert "Read" in out.body and "/etc/hosts" in out.body


def test_assistant_tool_use_grep_shows_pattern() -> None:
    ev = _assistant([{"type": "tool_use", "name": "Grep", "input": {"pattern": "foo.*bar"}}])
    out = render_event(ev)
    assert out is not None
    assert "Grep" in out.body and "foo.*bar" in out.body


def test_assistant_tool_use_unknown_shows_name_only() -> None:
    ev = _assistant([{"type": "tool_use", "name": "MysteryTool", "input": {"x": 1}}])
    out = render_event(ev)
    assert out is not None
    assert "MysteryTool" in out.body


def test_assistant_thinking_block_is_hidden_by_default() -> None:
    ev = _assistant([{"type": "thinking", "thinking": "secret pondering"}])
    assert render_event(ev) is None


def test_assistant_mixed_blocks_concatenated_in_order() -> None:
    ev = _assistant(
        [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "let me run a command"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}},
        ]
    )
    out = render_event(ev)
    assert out is not None
    assert out.body.index("let me run a command") < out.body.index("echo hi")
    assert "secret" not in out.body  # thinking suppressed


def test_user_tool_result_is_mirrored_as_collapsed_output() -> None:
    ev = _user(
        [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_x",
                "content": "line1\nline2\nline3",
            }
        ]
    )
    out = render_event(ev)
    assert out is not None
    assert out.kind == "tool_result"
    assert "line1" in out.body


def test_user_tool_result_with_list_content_is_concatenated() -> None:
    ev = _user(
        [
            {
                "type": "tool_result",
                "content": [
                    {"type": "text", "text": "part-a "},
                    {"type": "text", "text": "part-b"},
                ],
            }
        ]
    )
    out = render_event(ev)
    assert out is not None
    assert "part-a" in out.body and "part-b" in out.body


def test_user_tool_result_empty_is_skipped() -> None:
    ev = _user([{"type": "tool_result", "content": "   "}])
    assert render_event(ev) is None


def test_user_meta_event_is_skipped() -> None:
    ev = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "auto stuff"}}
    assert render_event(ev) is None


def test_user_input_with_zwsp_marker_is_skipped_as_clord_origin() -> None:
    # c-lord-driven send-keys prefixes input with ZWSP so we don't double-post to Discord.
    ev = _user(f"{ZWSP_MARKER}hello from discord")
    assert render_event(ev) is None


def test_user_input_without_marker_is_mirrored() -> None:
    # External (human typed in pane) input must be mirrored to Discord.
    ev = _user("typed directly in tmux")
    out = render_event(ev)
    assert out is not None
    assert out.kind == "user_input"
    assert "typed directly in tmux" in out.body


def test_framing_event_types_are_skipped() -> None:
    for t in (
        "file-history-snapshot",
        "ai-title",
        "last-prompt",
        "pr-link",
        "permission-mode",
        "queue-operation",
        "attachment",
        "system",
    ):
        assert render_event({"type": t}) is None, t


def test_render_carries_session_id_when_present() -> None:
    ev = _assistant([{"type": "text", "text": "hi"}])
    ev["sessionId"] = "abc-123"
    out = render_event(ev)
    assert out is not None
    assert out.session_id == "abc-123"


def test_render_returns_none_for_unknown_top_level_type() -> None:
    assert render_event({"type": "mystery"}) is None
