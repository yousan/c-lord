"""Tests for the TUI tool-noise stripper (Issue #38 Step 2)."""

from __future__ import annotations

from c_lord.discord_ui.tui_strip import strip_tool_noise


class TestStripToolNoise:
    def test_passthrough_when_no_tool_pattern(self) -> None:
        assert strip_tool_noise("Hello, the answer is 42.") == "Hello, the answer is 42."

    def test_strips_simple_bash_block(self) -> None:
        body = "Bash(echo green-2)\n  green-2\n\n出力: green-2"
        assert strip_tool_noise(body) == "出力: green-2"

    def test_strips_multiple_tool_blocks_keeping_final(self) -> None:
        body = (
            "Bash(pwd)\n  /home/foo\n\n"
            "Read(README.md)\n  # README\n  intro line\n\n"
            "最終回答: ここが本文です。"
        )
        assert strip_tool_noise(body) == "最終回答: ここが本文です。"

    def test_keeps_text_before_first_tool(self) -> None:
        # Conversational text before the first tool call is part of Claude's
        # reasoning we want to keep.
        body = "まず確認します。\n\nBash(ls)\n  a\n  b\n\nファイルは a, b です。"
        cleaned = strip_tool_noise(body)
        assert "まず確認します。" in cleaned
        assert "ファイルは a, b です。" in cleaned
        assert "Bash(ls)" not in cleaned
        assert "  a" not in cleaned

    def test_collapses_excessive_blank_lines(self) -> None:
        body = "first\n\n\n\nsecond"
        assert strip_tool_noise(body) == "first\n\nsecond"

    def test_strips_indented_output_only_following_tool(self) -> None:
        # Indented lines that are NOT after a tool call (e.g. code block continuation)
        # are preserved. We only strip indented lines that immediately follow a
        # tool call line.
        body = "Here is code:\n    indent line\n    another\n\nDone"
        # No tool pattern → unchanged.
        assert strip_tool_noise(body) == body

    def test_preserves_message_when_stripping_would_empty_it(self) -> None:
        # Defensive: if the result would be empty (very unusual), return the
        # original so the user does not see a blank reply.
        body = "Bash(only this)\n  no follow-up"
        result = strip_tool_noise(body)
        # Should fall back to the original since the cleaned text is empty.
        assert result == body

    def test_handles_all_known_tool_names(self) -> None:
        # Every tool we recognize gets stripped.
        for name in ("Read", "Write", "Edit", "Grep", "Glob", "WebFetch", "Task"):
            body = f"{name}(arg here)\n  output line\n\nresponse"
            assert strip_tool_noise(body) == "response", f"failed for {name}"

    def test_tool_call_with_no_following_output(self) -> None:
        body = "Bash(echo hi)\n\nDone"
        assert strip_tool_noise(body) == "Done"

    def test_real_capture_with_arrow_marker_stripped_upstream(self) -> None:
        # The actual capture-pane output the bot streams (markers like ●/⎿ are
        # already stripped by tmux_runner). This is the format we observed live.
        body = "Bash(echo green-2)\n  green-2\n\n出力: green-2"
        assert strip_tool_noise(body) == "出力: green-2"
