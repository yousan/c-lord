"""Tests for TmuxClaudeRunner."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import (
    TmuxClaudeRunner,
    _clean_tui_lines,
    _normalize_capture,
    _parse_ask_from_pane,
)
from c_lord.claude.types import MessageType


@pytest.fixture
def tmux_manager():
    """Create a mock TmuxSessionManager."""
    mgr = MagicMock()
    mgr.capture_pane.return_value = ""
    mgr.is_claude_running.return_value = False
    mgr.start_claude.return_value = True
    mgr.send_input.return_value = True
    mgr.send_interrupt.return_value = True
    mgr.kill_session.return_value = True
    return mgr


@pytest.fixture
def runner(tmux_manager):
    """Create a TmuxClaudeRunner with mock tmux_manager."""
    return TmuxClaudeRunner(
        tmux_manager=tmux_manager,
        thread_id=12345,
        model="sonnet",
        timeout_seconds=10,
    )


# -- Helpers for building TUI pane text in tests ----------------------------


def _make_pane(
    response_lines: list[str],
    user_prompt: str = "test",
    *,
    with_chrome: bool = True,
    with_input_prompt: bool = False,
) -> str:
    """Build a realistic Claude TUI pane text for testing.

    Args:
        response_lines: Lines of Claude's response (raw, with ● markers etc.).
        user_prompt: The user's prompt text.
        with_chrome: Include bottom TUI chrome (separators, status bar).
        with_input_prompt: Include bare ❯ (indicates Claude is done).
    """
    lines = [f"❯ {user_prompt}", ""]
    lines.extend(response_lines)
    if with_chrome:
        lines.append("")
        if with_input_prompt:
            lines.append("─" * 40)
            lines.append("❯")
            lines.append("─" * 40)
            lines.append("-- INSERT -- ⏵⏵ bypass permissions on")
    return "\n".join(lines)


# -- Tests for _extract_response --------------------------------------------


class TestExtractResponse:
    """Tests for TmuxClaudeRunner._extract_response."""

    def test_basic_response(self) -> None:
        pane = _make_pane(["● Hello, world!"])
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == "Hello, world!"

    def test_multiline_response(self) -> None:
        pane = _make_pane(
            [
                "● Here is my answer:",
                "",
                "  1. First item",
                "  2. Second item",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "Here is my answer:" in result
        assert "1. First item" in result
        assert "2. Second item" in result

    def test_response_with_tool_use(self) -> None:
        pane = _make_pane(
            [
                "● Bash(git status)",
                "  ⎿  On branch main",
                "     nothing to commit",
                "",
                "● The repo is clean.",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "Bash(git status)" in result
        assert "On branch main" in result
        assert "The repo is clean." in result

    def test_strips_bottom_chrome(self) -> None:
        pane = _make_pane(
            ["● Done!"],
            with_chrome=True,
            with_input_prompt=True,
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == "Done!"
        assert "INSERT" not in result
        assert "─" not in result
        assert "❯" not in result

    def test_strips_memory_recall(self) -> None:
        pane = _make_pane(
            [
                "● Recalled 1 memory (ctrl+o to expand)",
                "",
                "● Here is the answer.",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "Recalled" not in result
        assert "ctrl+o" not in result
        assert "Here is the answer." in result

    def test_strips_ctrl_o_hint(self) -> None:
        pane = _make_pane(
            [
                "● Read 3 files (ctrl+o to expand)",
                "",
                "● Summary of files.",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "ctrl+o" not in result
        assert "Read 3 files" in result

    def test_empty_pane(self) -> None:
        assert TmuxClaudeRunner._extract_response("") == ""

    def test_no_user_prompt(self) -> None:
        pane = "Some random text\nwithout prompt markers"
        assert TmuxClaudeRunner._extract_response(pane) == ""

    def test_scrolled_off_prompt_fallback(self) -> None:
        """When the user prompt scrolled off-screen, extract response from visible content."""
        pane = "\n".join(
            [
                "",
                "● 2",
                "",
                "  詳しく知りたい部分はありますか？",
                "",
                "──────────────────────────────────────────",
                "❯\xa0",
                "──────────────────────────────────────────",
                "  -- INSERT -- ⏵⏵ bypass permissions on",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "2" in result
        assert "詳しく知りたい部分はありますか？" in result

    def test_scrolled_off_prompt_strips_banner(self) -> None:
        """Fallback mode strips shell noise and banner at the top."""
        pane = "\n".join(
            [
                "$ unalias claude; env -u CLAUDECODE claude --model sonnet 'hi'",
                "",
                " ▐▛███▜▌   Claude Code v2.1.59",
                "▝▜█████▛▘  Sonnet 4.6",
                "  ▘▘ ▝▝    ~/work",
                "",
                "● Hello! How can I help?",
                "",
                "──────────────────────────────────────────",
                "❯\xa0",
                "──────────────────────────────────────────",
                "  -- INSERT -- ⏵⏵ bypass permissions on",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "Hello! How can I help?" in result
        # Banner and shell noise should not appear
        assert "Claude Code" not in result
        assert "unalias" not in result

    def test_pane_with_shell_noise_and_banner(self) -> None:
        """Shell noise and banner before the user prompt are ignored."""
        pane = "\n".join(
            [
                "[oh-my-zsh] plugin 'foo' not found",
                "No GitHub token found.",
                "$ unalias claude; env -u CLAUDECODE claude --model sonnet 'test'",
                "",
                " ▐▛███▜▌   Claude Code v2.1.56",
                "▝▜█████▛▘  Sonnet 4.6",
                "  ▘▘ ▝▝    ~/work",
                "",
                "❯ test",
                "",
                "● Hello from Claude!",
                "",
                "─" * 40,
                "❯",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == "Hello from Claude!"
        assert "oh-my-zsh" not in result
        assert "GitHub" not in result
        assert "Claude Code v2" not in result

    def test_finds_last_user_prompt(self) -> None:
        """When multiple ❯ prompts exist, uses the last one with text."""
        pane = "\n".join(
            [
                "❯ first question",
                "",
                "● First answer",
                "",
                "❯ second question",
                "",
                "● Second answer",
                "",
                "─" * 40,
                "❯",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == "Second answer"
        assert "First answer" not in result

    def test_strips_searching_with_intermediate_word(self) -> None:
        """Tool indicators with words between verb and digit must be stripped.

        The TUI shows "Searching for 1 pattern…" (note "for" between verb and
        digit) — the original \\d+-only regex missed this case and let it
        leak into the Discord output.
        """
        pane = _make_pane(
            [
                "● Searching for 1 pattern…",
                "● Found it.",
            ],
            with_chrome=True,
            with_input_prompt=True,
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "Searching for 1 pattern" not in result
        assert "Found it." in result

    def test_strips_tool_indicator_with_animation_artifacts(self) -> None:
        """Tool indicator captured mid-animation has trailing chars after `…`.

        e.g. ``Reading 1 file…e…`` (TUI redraws ellipsis dots one-by-one and
        ``capture-pane`` snapshots a partial frame). Must still be stripped.
        """
        pane = _make_pane(
            [
                "● Reading 1 file…e…",
                "● Done reading.",
            ],
            with_chrome=True,
            with_input_prompt=True,
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "Reading 1 file" not in result
        assert "Done reading." in result

    def test_strips_ccstatusline_block(self) -> None:
        """ccstatusline output (multi-line, indented, between bottom separator and
        vim status bar) must be stripped from the Discord output.

        The Claude TUI bottom layout when ccstatusline is configured:
            ───────── (top separator)
            ❯ <input>
            ───────── (bottom separator)
            <ccstatusline lines, variable count, leading whitespace>
            -- INSERT -- ⏵⏵ ...
        """
        pane = "\n".join(
            [
                "❯ hello",
                "",
                "● Hello! How can I help you today?",
                "",
                "✻ Crunched for 2s",
                "",
                "─" * 100,
                "❯ ",
                "─" * 100,
                "   Model: Sonnet 4.6  Style: default  Ctx: 21.9k  Context: [...] 22k/1000k",
                "   Cost: $0.05  Session: 7.0%  Weekly: 14.0%  Reset: 1hr 51m",
                "   ⎇ main  (+0,-0)  +0  -0  1499210603107975270  cwd: /home/yousan/c-lord",
                "  -- INSERT -- ⏵⏵ bypass permissions on (shift+tab to cycle)",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == "Hello! How can I help you today?"
        assert "Model:" not in result
        assert "Cost:" not in result
        assert "cwd:" not in result
        assert "INSERT" not in result

    def test_strips_generation_status_during_streaming(self) -> None:
        """During generation, thinking indicators and tips at bottom are stripped."""
        pane = "\n".join(
            [
                "❯ hello",
                "",
                "● Working on it...",
                "",
                "─" * 40,
                "· Pontificating…",
                "  Tip: You have free guest passes to share · /passes",
                "─" * 40,
                "-- INSERT -- ⏵⏵ bypass permissions on",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == "Working on it..."
        assert "Pontificating" not in result
        assert "Tip:" not in result
        assert "─" not in result

    def test_strips_thinking_indicator_only(self) -> None:
        """When only a thinking indicator is visible (no response yet)."""
        pane = "\n".join(
            [
                "❯ hello",
                "",
                "─" * 40,
                "✻ Envisioning…",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == ""

    def test_strips_various_dingbat_thinking_indicators(self) -> None:
        """Various Unicode dingbats used as thinking indicators are stripped."""
        for indicator in ["✽ Gallivanting…", "✦ Cogitating…", "✻ Envisioning…", "✹ Thinking…"]:
            pane = "\n".join(
                [
                    "❯ test",
                    "",
                    "● Good response.",
                    "",
                    "─" * 40,
                    indicator,
                    "─" * 40,
                    "-- INSERT --",
                ]
            )
            result = TmuxClaudeRunner._extract_response(pane)
            assert result == "Good response.", f"Failed for indicator: {indicator}"

    def test_strips_press_up_to_edit_hint(self) -> None:
        """TUI hint '❯ Press up to edit' with non-breaking space is chrome."""
        pane = "\n".join(
            [
                "❯ hello",
                "",
                "● Response text here.",
                "",
                "─" * 40,
                "❯\xa0Press up to edit",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == "Response text here."
        assert "Press up" not in result
        assert "─" not in result

    def test_strips_regular_bare_prompt_with_text(self) -> None:
        """Any ❯ line at the bottom chrome is stripped."""
        pane = "\n".join(
            [
                "❯ question",
                "",
                "● Answer.",
                "",
                "─" * 40,
                "❯ ",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == "Answer."

    def test_strips_cooked_for_completion_indicator(self) -> None:
        """Completion status like '✻ Cooked for 56s' is stripped."""
        pane = "\n".join(
            [
                "❯ question",
                "",
                "● Full answer here.",
                "",
                "✻ Cooked for 56s",
                "",
                "─" * 40,
                "❯",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == "Full answer here."
        assert "Cooked" not in result

    def test_returns_empty_when_only_user_prompt_no_response(self) -> None:
        """User prompt visible but Claude has not started responding — return empty.

        Regression for issue #30: previously the Bot echoed the user's input
        because the post-prompt region was returned even without any Claude
        response markers (●/⎿/✻).
        """
        pane = "\n".join(
            [
                "❯ ユーザーの質問",
                "",
                "─" * 40,
                "❯ ",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        assert TmuxClaudeRunner._extract_response(pane) == ""

    def test_returns_empty_for_multiline_user_prompt_no_response(self) -> None:
        """Multi-line user input continuation lines must NOT be returned as response.

        Root cause of issue #30: the Discord message was multi-line, and the
        TUI showed continuation lines after the ❯ prompt line. Those lines
        had no ●/⎿ marker but the previous extractor treated them as Claude's
        response and echoed them back.
        """
        pane = "\n".join(
            [
                "❯ first line of question",
                "  second line of question",
                "  third line of question",
                "",
                "─" * 40,
                "❯ ",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        assert TmuxClaudeRunner._extract_response(pane) == ""

    def test_returns_empty_when_only_thinking_indicator(self) -> None:
        """Thinking indicator alone (no ●/⎿) → empty response."""
        pane = "\n".join(
            [
                "❯ ユーザーの質問",
                "",
                "─" * 40,
                "✻ Envisioning…",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        assert TmuxClaudeRunner._extract_response(pane) == ""

    def test_extracts_response_when_bullet_marker_present(self) -> None:
        """When the response zone contains a ● marker, extract normally."""
        pane = "\n".join(
            [
                "❯ ユーザーの質問",
                "",
                "● Claude の応答",
                "",
                "─" * 40,
                "❯ ",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        assert "Claude の応答" in TmuxClaudeRunner._extract_response(pane)

    def test_extracts_response_with_tool_marker_only(self) -> None:
        """When only a ⎿ tool marker is present (no ●), still treat as response."""
        pane = "\n".join(
            [
                "❯ ファイル読んで",
                "",
                "⎿ Reading file ...",
                "",
                "─" * 40,
                "❯ ",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        assert TmuxClaudeRunner._extract_response(pane) != ""

    def test_extract_response_strips_calling_mcp_tool_indicator(self) -> None:
        """`✶ Calling plugin:discord:discord…` is a thinking indicator,
        not a response anchor — must not leak as content. Regression for #39.
        """
        pane = (
            "❯ user question\n"
            "\n"
            "✶ Calling plugin:discord:discord…\n"
            "\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "-- INSERT -- ⏵⏵ bypass permissions on\n"
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "Calling plugin:discord:discord" not in result
        assert result == ""

    def test_strips_ascii_asterisk_thinking_in_chrome(self) -> None:
        """Plain * thinking indicators in bottom chrome are stripped."""
        pane = "\n".join(
            [
                "❯ question",
                "",
                "● Answer.",
                "",
                "─" * 40,
                "* Forming…",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert result == "Answer."
        assert "Forming" not in result

    def test_does_not_emit_chrome_during_redraw_race(self) -> None:
        """Regression for issue #32.

        When tmux capture-pane catches a frame mid-redraw, the pane may show
        a "ghost" copy of the bottom chrome (bare ❯ + ccstatusline + tool
        fragment) above the real bottom chrome. The extractor must NOT emit
        these chrome lines as response text.

        Real-world reproduction: Discord message 1501818362169397428 in
        thread 1499664870948339834 on 2026-05-07 contained:
            ❯
               Model: Opus 4.7 ... Style: default ...
               Cost: $0.06 ...
               ⎇ main ... cwd: ... Skill: none
               ... 3 skill descriptions dropped · /doctor for details ...
              Error: Exit code 127
        """
        pane = "\n".join(
            [
                "$ cd /home/yousan/c-lord",
                "yousan@host:~$ claude",
                "",
                # "ghost" mid-pane chrome from a stale redraw frame
                "❯",
                "   Model: Opus 4.7  v2.1.132  Style: default  Ctx: 22.8k  Context: [...]",
                "   Cost: $0.06  Session: 5.0%  Weekly: 21.0%  Reset: 4hr 10m",
                "   ⎇ main  (+0,-0)  +0  -0  cwd: /home/yousan/c-lord  Skill: none",
                "                3 skill descriptions dropped · /doctor for details",
                "  Error: Exit code 127",
                "     === /home/yousan/c-lord/data/sessions.db ===",
                # real bottom chrome
                "─" * 100,
                "❯",
                "─" * 100,
                "   Model: Opus 4.7  v2.1.132  Style: default  Ctx: 22.8k",
                "  -- INSERT -- ⏵⏵ bypass permissions on",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "Model:" not in result, f"ccstatusline leaked: {result!r}"
        assert "Cost:" not in result, f"ccstatusline leaked: {result!r}"
        assert "/doctor" not in result, f"tool indicator leaked: {result!r}"
        for line in result.splitlines():
            assert line.strip() != "❯", f"bare prompt leaked: {result!r}"


# -- Tests for _clean_tui_lines ---------------------------------------------


class TestCleanTuiLines:
    """Tests for the _clean_tui_lines helper function."""

    def test_strips_bare_input_prompt_line(self) -> None:
        """Bare ❯ line in response area is chrome leakage; must be dropped."""
        assert _clean_tui_lines(["❯", "● Hi"]) == "Hi"

    def test_strips_ccstatusline_model_row(self) -> None:
        """ccstatusline 'Model: ... Style:' rows must not leak (issue #32)."""
        result = _clean_tui_lines(
            [
                "● Real response.",
                "   Model: Opus 4.7  Style: default  Ctx: 22.8k",
            ]
        )
        assert result == "Real response."

    def test_strips_ccstatusline_cost_row(self) -> None:
        """ccstatusline 'Cost: $... Session:' rows must not leak (issue #32)."""
        result = _clean_tui_lines(
            [
                "● Real response.",
                "   Cost: $0.06  Session: 5.0%  Weekly: 21.0%",
            ]
        )
        assert result == "Real response."

    def test_strips_ccstatusline_branch_row(self) -> None:
        """ccstatusline '⎇ branch ... cwd:' rows must not leak (issue #32)."""
        result = _clean_tui_lines(
            [
                "● Real response.",
                "   ⎇ main  (+0,-0)  cwd: /home/yousan/c-lord  Skill: none",
            ]
        )
        assert result == "Real response."

    def test_strips_tip_line(self) -> None:
        """'Tip: Use Plan Mode ...' lines are TUI hints, not Claude response."""
        result = _clean_tui_lines(
            [
                "Tip: Use Plan Mode to prepare for a complex request before making changes.",
                "● Real response.",
            ]
        )
        assert result == "Real response."

    def test_strips_effort_indicator(self) -> None:
        """'◐ medium · /effort' TUI footer must not leak (Issue #50)."""
        result = _clean_tui_lines(
            [
                "● Real response.",
                "",
                "◐ medium · /effort",
            ]
        )
        assert "◐" not in result
        assert "/effort" not in result
        assert "Real response." in result

    def test_strips_skill_descriptions_dropped_indicator(self) -> None:
        """'N skill descriptions dropped · /doctor for details' is TUI noise."""
        result = _clean_tui_lines(
            [
                "● Real response.",
                "                3 skill descriptions dropped · /doctor for details",
            ]
        )
        assert result == "Real response."

    def test_strips_bullet_marker(self) -> None:
        assert _clean_tui_lines(["● Hello"]) == "Hello"

    def test_strips_bare_bullet(self) -> None:
        assert _clean_tui_lines(["●"]) == ""

    def test_cleans_tool_result_marker(self) -> None:
        result = _clean_tui_lines(["  ⎿  some output"])
        assert result == "  some output"

    def test_preserves_indented_lines(self) -> None:
        result = _clean_tui_lines(["    indented content"])
        assert result == "    indented content"

    def test_strips_leading_empty_lines(self) -> None:
        result = _clean_tui_lines(["", "", "● Text"])
        assert result == "Text"

    def test_strips_trailing_empty_lines(self) -> None:
        result = _clean_tui_lines(["● Text", "", ""])
        assert result == "Text"

    def test_removes_memory_recall_line(self) -> None:
        lines = [
            "● Recalled 2 memories (ctrl+o to expand)",
            "",
            "● Actual response.",
        ]
        result = _clean_tui_lines(lines)
        assert "Recalled" not in result
        assert "Actual response." in result

    def test_removes_ctrl_o_from_inline(self) -> None:
        lines = ["● Read 5 files (ctrl+o to expand)"]
        result = _clean_tui_lines(lines)
        assert result == "Read 5 files"
        assert "ctrl+o" not in result

    def test_removes_thinking_indicator_in_response_area(self) -> None:
        """Thinking indicators that appear inside the response area are stripped."""
        lines = ["✻ Moseying…"]
        result = _clean_tui_lines(lines)
        assert result == ""

    def test_removes_ascii_thinking_indicator(self) -> None:
        """Plain * thinking indicators (tmux fallback) are stripped."""
        lines = ["* Forming…"]
        result = _clean_tui_lines(lines)
        assert result == ""

    def test_removes_cooked_for_in_response_area(self) -> None:
        """Completion status in response area is stripped."""
        lines = [
            "● Answer here.",
            "",
            "✻ Cooked for 56s",
        ]
        result = _clean_tui_lines(lines)
        assert result == "Answer here."
        assert "Cooked" not in result

    def test_removes_ascii_cooked_for(self) -> None:
        """Plain * completion status is stripped."""
        lines = [
            "● Done.",
            "",
            "* Cooked for 3s",
        ]
        result = _clean_tui_lines(lines)
        assert result == "Done."

    def test_removes_press_ctrl_c_exit(self) -> None:
        """'Press Ctrl-C again to exit' TUI noise is stripped."""
        lines = [
            "● Answer here.",
            "Press Ctrl-C again to exit",
        ]
        result = _clean_tui_lines(lines)
        assert result == "Answer here."
        assert "Ctrl-C" not in result

    def test_greeting_preserved(self) -> None:
        """Claude greeting is kept — it may be a legitimate response to user's 'hi'."""
        lines = [
            "Hi! How can I help you today?",
            "",
            "● Actual response.",
        ]
        result = _clean_tui_lines(lines)
        assert "Hi! How can I help you today?" in result
        assert "Actual response." in result

    def test_greeting_with_bullet_marker_preserved(self) -> None:
        """Greeting with ● marker is kept as a valid response."""
        lines = [
            "● Hi! How can I help you with c-lord today?",
        ]
        result = _clean_tui_lines(lines)
        assert "Hi! How can I help you with c-lord today?" in result

    def test_removes_mode_switched(self) -> None:
        """'Claude Code has switched...' notification is stripped."""
        lines = [
            "Claude Code has switched to compact mode",
            "",
            "● Response text.",
        ]
        result = _clean_tui_lines(lines)
        assert result == "Response text."
        assert "switched" not in result

    def test_removes_insert_status_bar(self) -> None:
        """Vim-style '-- INSERT ...' status bar line is stripped."""
        lines = [
            "● Done.",
            "-- INSERT ⏵⏵ bypass permissions on",
        ]
        result = _clean_tui_lines(lines)
        assert result == "Done."
        assert "INSERT" not in result

    def test_removes_normal_status_bar(self) -> None:
        """Vim-style '-- NORMAL ...' status bar line is stripped."""
        lines = [
            "● Done.",
            "-- NORMAL ⏸⏸",
        ]
        result = _clean_tui_lines(lines)
        assert result == "Done."
        assert "NORMAL" not in result

    def test_removes_ascii_hyphen_separator(self) -> None:
        """ASCII hyphen separator lines (5+ hyphens) are stripped."""
        lines = [
            "● Text above.",
            "----------",
            "● Text below.",
        ]
        result = _clean_tui_lines(lines)
        assert "----------" not in result
        assert "Text above." in result
        assert "Text below." in result

    def test_keeps_short_hyphen_sequences(self) -> None:
        """Short hyphen sequences (< 5) in content are preserved."""
        lines = ["● Use -- for flags"]
        result = _clean_tui_lines(lines)
        assert "Use -- for flags" in result


# -- Tests for _compute_delta (kept for backward compat) --------------------


class TestTmuxClaudeRunnerDelta:
    """Tests for the _compute_delta static method."""

    def test_simple_append(self) -> None:
        old = "line1\nline2"
        new = "line1\nline2\nline3"
        assert TmuxClaudeRunner._compute_delta(old, new) == "\nline3"

    def test_no_change(self) -> None:
        text = "line1\nline2"
        assert TmuxClaudeRunner._compute_delta(text, text) == ""

    def test_full_new_text(self) -> None:
        delta = TmuxClaudeRunner._compute_delta("", "hello")
        assert delta == "hello"

    def test_trailing_whitespace_normalized(self) -> None:
        old = "line1\n"
        new = "line1\nline2\n"
        assert TmuxClaudeRunner._compute_delta(old, new) == "\nline2"

    def test_screen_redraw_overlap(self) -> None:
        old = "line1\nline2\nline3"
        new = "line2\nline3\nline4\nline5"
        delta = TmuxClaudeRunner._compute_delta(old, new)
        assert "line4" in delta
        assert "line5" in delta

    def test_no_overlap_returns_all_new(self) -> None:
        old = "completely different"
        new = "totally new content"
        delta = TmuxClaudeRunner._compute_delta(old, new)
        assert delta == "totally new content"


# -- Tests for prompt detection ----------------------------------------------


class TestTmuxClaudeRunnerPromptDetection:
    """Tests for _has_input_prompt."""

    def test_detects_arrow_prompt(self) -> None:
        text = "Some output\n❯ "
        assert TmuxClaudeRunner._has_input_prompt(text) is True

    def test_detects_gt_prompt(self) -> None:
        text = "Some output\n> "
        assert TmuxClaudeRunner._has_input_prompt(text) is True

    def test_detects_prompt_with_trailing_whitespace(self) -> None:
        text = "Some output\n❯   \n  "
        assert TmuxClaudeRunner._has_input_prompt(text) is True

    def test_no_prompt(self) -> None:
        text = "Claude is thinking...\nSome text"
        assert TmuxClaudeRunner._has_input_prompt(text) is False

    def test_empty_text(self) -> None:
        assert TmuxClaudeRunner._has_input_prompt("") is False


# -- Tests for trust/permission prompt detection -----------------------------


class TestTmuxClaudeRunnerTrustPrompt:
    """Tests for _has_trust_prompt."""

    def test_detects_trust_prompt(self) -> None:
        text = (
            "Quick safety check: Is this a project you created?\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit\n"
            "Enter to confirm"
        )
        assert TmuxClaudeRunner._has_trust_prompt(text) is True

    def test_no_trust_prompt(self) -> None:
        text = "Hello! How can I help?\n❯"
        assert TmuxClaudeRunner._has_trust_prompt(text) is False


class TestTmuxClaudeRunnerHandleStartupPrompts:
    """Tests for _handle_startup_prompts."""

    @pytest.mark.asyncio
    async def test_no_trust_prompt_returns_quickly(self, runner, tmux_manager) -> None:
        tmux_manager.capture_pane.return_value = "Loading..."
        tmux_manager._find_window_for_thread.return_value = "work1"

        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.2),
        ):
            await runner._handle_startup_prompts()

    @pytest.mark.asyncio
    async def test_handles_trust_prompt(self, runner, tmux_manager) -> None:
        capture_sequence = [
            "Loading...",
            "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm",
            "Processing...",
            "Processing...",
            "Processing...",
            "Processing...",
        ]
        tmux_manager.capture_pane.side_effect = capture_sequence
        tmux_manager._find_window_for_thread.return_value = "work1"

        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 5.0),
            patch("c_lord.tmux._run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            await runner._handle_startup_prompts()

        mock_run.assert_called_once()


# -- Tests for run() --------------------------------------------------------


class TestTmuxClaudeRunnerRun:
    """Tests for the run() async generator.

    Completion is detected by response-stability: when the extracted
    response text hasn't changed for _RESPONSE_STABLE_TIMEOUT seconds,
    Claude is considered done.  Tests patch this to a small value and
    use function-based side_effects to avoid list exhaustion.
    """

    @pytest.mark.asyncio
    async def test_start_claude_fresh(self, runner, tmux_manager) -> None:
        """Default runner: when Claude is not running, start_claude with try_continue=False."""
        pane = _make_pane(["● Hello!"])
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            if call_idx <= 2:
                return "Loading..."
            return pane

        tmux_manager.capture_pane.side_effect = capture_fn
        tmux_manager.is_claude_running.return_value = False
        tmux_manager._find_window_for_thread.return_value = "work1"

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.09),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
        ):
            async for event in runner.run("test prompt"):
                events.append(event)

        # Exactly one start_claude call with try_continue=False (direct fresh)
        tmux_manager.start_claude.assert_called_once_with(
            12345,
            "test prompt",
            "sonnet",
            permission_mode="acceptEdits",
            dangerously_skip_permissions=False,
            try_continue=False,
        )
        assert any(e.message_type == MessageType.RESULT and e.is_complete for e in events)

    @pytest.mark.asyncio
    async def test_send_input_when_claude_already_running(self, runner, tmux_manager) -> None:
        """When Claude is already running, send_input is used."""
        tmux_manager.is_claude_running.return_value = True
        pane = _make_pane(["● New response"], user_prompt="follow up")
        tmux_manager.capture_pane.return_value = pane

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.09),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
        ):
            async for event in runner.run("follow up"):
                events.append(event)

        tmux_manager.send_input.assert_called_once_with(12345, "follow up")
        tmux_manager.start_claude.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_claude_failure_yields_error(self, runner, tmux_manager) -> None:
        tmux_manager.is_claude_running.return_value = False
        tmux_manager.start_claude.return_value = False

        events = []
        async for event in runner.run("test"):
            events.append(event)

        assert len(events) == 2
        assert events[0].message_type == MessageType.SYSTEM
        assert events[0].session_id == "tmux-12345"
        assert events[-1].is_complete
        assert events[-1].error == "Failed to start Claude in tmux"

    @pytest.mark.asyncio
    async def test_send_input_failure_yields_error(self, runner, tmux_manager) -> None:
        tmux_manager.is_claude_running.return_value = True
        tmux_manager.send_input.return_value = False

        events = []
        async for event in runner.run("test"):
            events.append(event)

        assert len(events) == 2
        assert events[0].message_type == MessageType.SYSTEM
        assert events[0].session_id == "tmux-12345"
        assert events[-1].is_complete
        assert events[-1].error == "Failed to send input to Claude in tmux"

    @pytest.mark.asyncio
    async def test_idle_timeout_completes(self, runner, tmux_manager) -> None:
        """When no response appears for _IDLE_TIMEOUT, the run completes."""
        tmux_manager.is_claude_running.return_value = True
        tmux_manager.capture_pane.return_value = "static text"

        runner.timeout_seconds = 60
        events = []
        with (
            patch("c_lord.claude.tmux_runner._IDLE_TIMEOUT", 0.3),
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
        ):
            async for event in runner.run("test"):
                events.append(event)

        result_events = [e for e in events if e.is_complete]
        assert len(result_events) == 1


class TestInactivityTimeout:
    """#94: the hard ``timeout_seconds`` backstop must be inactivity-based.

    The old loop killed any turn whose total wall-clock exceeded
    ``timeout_seconds`` — even while Claude was actively thinking / running
    tools (the pane spinner + elapsed counter ticking every poll).  That
    posted a bogus "Session timed out" notice on live sessions.  The timeout
    now fires only after the pane has been *frozen* for ``timeout_seconds``.
    """

    @pytest.mark.asyncio
    async def test_does_not_timeout_while_pane_active(self, runner, tmux_manager) -> None:
        """A long but ACTIVE turn must complete, not emit a 'Timed out' RESULT."""
        tmux_manager.is_claude_running.return_value = True
        done_pane = _make_pane(["● All done!"], with_input_prompt=True)
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            # Pane changes every poll (elapsed-seconds tick) for far longer
            # than timeout_seconds — Claude is alive — then finishes.
            if call_idx <= 20:
                return f"\n✻ Cogitating for {call_idx}s\n────────\n❯\n────────\n-- INSERT --"
            return done_pane

        tmux_manager.capture_pane.side_effect = capture_fn

        runner.timeout_seconds = 0.2  # ~10 polls @ 0.02; the active phase outlasts it
        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        ):
            async for event in runner.run("test"):
                events.append(event)

        result_events = [e for e in events if e.is_complete]
        assert len(result_events) == 1
        assert result_events[0].error is None, (
            f"active turn was killed by the timeout: {result_events[0].error!r}"
        )

    @pytest.mark.asyncio
    async def test_timeout_fires_when_pane_frozen(self, runner, tmux_manager) -> None:
        """A genuinely hung (frozen-pane) session is still killed by the backstop."""
        tmux_manager.is_claude_running.return_value = True
        # A response is on screen (so the empty-response idle break does not
        # apply) but the pane never changes again — a real hang.
        frozen = _make_pane(["● Partial answer, then hung"], user_prompt="q")
        tmux_manager.capture_pane.return_value = frozen

        runner.timeout_seconds = 0.2
        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
            # Push the other exits out so only the inactivity backstop can fire.
            patch("c_lord.claude.tmux_runner._IDLE_TIMEOUT", 100.0),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 100.0),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_FALLBACK", 100.0),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        ):
            async for event in runner.run("q"):
                events.append(event)

        result_events = [e for e in events if e.is_complete]
        assert len(result_events) == 1
        assert result_events[0].error is not None
        assert "Timed out" in result_events[0].error


class TestTmuxClaudeRunnerInterrupt:
    """Tests for interrupt() and kill()."""

    @pytest.mark.asyncio
    async def test_interrupt_sends_ctrl_c(self, runner, tmux_manager) -> None:
        await runner.interrupt()
        tmux_manager.send_interrupt.assert_called_once_with(12345)
        assert runner._stopped is True

    @pytest.mark.asyncio
    async def test_kill_kills_session(self, runner, tmux_manager) -> None:
        await runner.kill()
        tmux_manager.kill_session.assert_called_once_with(12345)
        assert runner._stopped is True

    @pytest.mark.asyncio
    async def test_interrupt_stops_polling(self, runner, tmux_manager) -> None:
        """After interrupt(), the run loop should exit."""
        tmux_manager.is_claude_running.return_value = True
        call_count = 0

        def capture_side_effect(tid):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return ""
            return f"text {call_count}"

        tmux_manager.capture_pane.side_effect = capture_side_effect

        events = []

        async def interrupt_after_delay():
            await asyncio.sleep(0.15)
            await runner.interrupt()

        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
        ):
            task = asyncio.create_task(interrupt_after_delay())
            async for event in runner.run("test"):
                events.append(event)
            await task

        result_events = [e for e in events if e.is_complete]
        assert len(result_events) == 1
        assert result_events[0].error == "Stopped by user"

    @pytest.mark.asyncio
    async def test_silent_interrupt_no_error(self, runner, tmux_manager) -> None:
        """interrupt(silent=True) yields a RESULT with error=None (no error embed)."""
        tmux_manager.is_claude_running.return_value = True
        pane = _make_pane(["● Partial response"], user_prompt="q")
        call_count = 0

        def capture_side_effect(tid):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return ""
            return pane

        tmux_manager.capture_pane.side_effect = capture_side_effect

        events = []

        async def interrupt_after_delay():
            await asyncio.sleep(0.15)
            await runner.interrupt(silent=True)

        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
        ):
            task = asyncio.create_task(interrupt_after_delay())
            async for event in runner.run("q"):
                events.append(event)
            await task

        result_events = [e for e in events if e.is_complete]
        assert len(result_events) == 1
        # Silent interrupt should NOT produce an error.
        # Issue #53: RESULT.text is intentionally dropped — Claude posts its
        # final answer via the discord-reply skill, not via the runner stream.
        assert result_events[0].error is None
        assert result_events[0].text is None


# -- Tests for _is_generating ------------------------------------------------


class TestIsGenerating:
    """Tests for TmuxClaudeRunner._is_generating."""

    def test_is_generating_during_tool_execution(self) -> None:
        """Active generation indicator (ending with …) → True."""
        text = "\n".join(
            [
                "❯ list issues",
                "",
                "● Bash(gh issue list ...)",
                "",
                "─" * 40,
                "✻ Running…",
                "─" * 40,
                "-- INSERT -- ⏵⏵ bypass permissions on",
            ]
        )
        assert TmuxClaudeRunner._is_generating(text) is True

    def test_is_generating_when_completed(self) -> None:
        """No generation indicator, just bare ❯ → False."""
        text = "\n".join(
            [
                "❯ list issues",
                "",
                "● Here are the issues.",
                "",
                "─" * 40,
                "❯",
                "─" * 40,
                "-- INSERT -- ⏵⏵ bypass permissions on",
            ]
        )
        assert TmuxClaudeRunner._is_generating(text) is False

    def test_is_generating_completion_summary(self) -> None:
        """Completion summary without … (e.g. 'Cooked for 56s') → False."""
        text = "\n".join(
            [
                "❯ question",
                "",
                "● Full answer.",
                "",
                "─" * 40,
                "✻ Cooked for 56s",
                "─" * 40,
                "-- INSERT -- ⏵⏵ bypass permissions on",
            ]
        )
        assert TmuxClaudeRunner._is_generating(text) is False

    def test_is_generating_thinking_indicator(self) -> None:
        """Thinking indicator (ending with …) → True."""
        text = "\n".join(
            [
                "❯ hello",
                "",
                "─" * 40,
                "✻ Envisioning…",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        assert TmuxClaudeRunner._is_generating(text) is True

    def test_is_generating_ascii_asterisk(self) -> None:
        """tmux-captured ASCII asterisk thinking (ending with …) → True."""
        text = "\n".join(
            [
                "❯ test",
                "",
                "─" * 40,
                "* Forming…",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        assert TmuxClaudeRunner._is_generating(text) is True

    def test_is_generating_empty_text(self) -> None:
        """Empty pane text → False."""
        assert TmuxClaudeRunner._is_generating("") is False


class TestToolExecutionCompletion:
    """Tests that tool execution does not trigger false early completion."""

    @pytest.mark.asyncio
    async def test_tool_execution_does_not_trigger_early_completion(
        self, runner, tmux_manager
    ) -> None:
        """When ✻ Running… is visible and text is stable, quick-exit should NOT fire.

        The pane shows a tool call with ✻ Running… indicator and bare ❯ prompt.
        Without the _is_generating guard, the 3s quick-exit would fire.
        With the guard, only the 30s fallback can trigger completion.
        """
        tmux_manager.is_claude_running.return_value = True

        # Pane shows a tool call with ✻ Running… — text is stable but not done
        tool_pane = "\n".join(
            [
                "❯ list issues",
                "",
                "● Bash(gh issue list --limit 10)",
                "",
                "─" * 40,
                "✻ Running…",
                "❯",
                "─" * 40,
                "-- INSERT -- ⏵⏵ bypass permissions on",
            ]
        )
        # After several polls at tool_pane (stable but generating),
        # Claude finishes and shows the final response
        final_pane = _make_pane(
            [
                "● Here are the issues:",
                "",
                "  | # | Title     |",
                "  |---|-----------|",
                "  | 1 | Fix bug   |",
            ],
            user_prompt="list issues",
            with_input_prompt=True,
        )

        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            # Polls 1-8: tool running (stable text with ✻ Running…)
            if call_idx <= 8:
                return tool_pane
            # Polls 9+: final response
            return final_pane

        tmux_manager.capture_pane.side_effect = capture_fn

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.15),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_FALLBACK", 30.0),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
        ):
            async for event in runner.run("list issues"):
                events.append(event)

        # Runner should complete normally without early exit during tool execution.
        # ASSISTANT events are no longer yielded (#53); check RESULT instead.
        results = [e for e in events if e.message_type == MessageType.RESULT]
        assert len(results) == 1
        assert results[0].is_complete is True
        assert results[0].error is None


class TestGeneratingDoesNotCompleteEarly:
    """Regression for #179: a stable intermediate response while Claude is still
    generating (the indicator line carries trailing token stats, e.g.
    ``✽ Generating… (7m 45s · ↑ 23.2k tokens)``) must NOT finalize the turn.

    Incident: the turn finalized while Claude kept working, so the poll loop
    stopped before a later AskUserQuestion menu rendered — leaving it unbridged
    and the session stuck with no way to answer from Discord.
    """

    @pytest.mark.asyncio
    async def test_generating_with_token_stats_does_not_complete_early(
        self, runner, tmux_manager
    ) -> None:
        tmux_manager.is_claude_running.return_value = True

        # Stable intermediate response + active generation indicator (trailing
        # stats) + the always-present bare ❯ input box.
        generating_pane = "\n".join(
            [
                "❯ fix the bug",
                "",
                "● Analyzing the failure and drafting a fix…",
                "",
                "─" * 40,
                "❯",
                "─" * 40,
                "✽ Generating… (7m 45s · ↑ 23.2k tokens)",
                "-- INSERT -- ⏵⏵ bypass permissions on",
            ]
        )
        # Claude actually finishes: same response text, generation line gone.
        done_pane = "\n".join(
            [
                "❯ fix the bug",
                "",
                "● Analyzing the failure and drafting a fix…",
                "",
                "─" * 40,
                "❯",
                "─" * 40,
                "-- INSERT -- ⏵⏵ bypass permissions on",
            ]
        )

        generating_polls = 12
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            if call_idx <= generating_polls:
                return generating_pane
            return done_pane

        tmux_manager.capture_pane.side_effect = capture_fn

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.05),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_FALLBACK", 0.1),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
        ):
            async for event in runner.run("fix the bug"):
                events.append(event)

        results = [e for e in events if e.message_type == MessageType.RESULT]
        assert len(results) == 1
        assert results[0].is_complete is True
        # The turn must keep polling through the generating phase and finalize
        # only once the done pane appears.  With the bug, is_gen is False on the
        # generating pane (indicator does not end with '…'), so the quick-exit
        # (or fallback) fires early and call_count never exceeds generating_polls.
        assert tmux_manager.capture_pane.call_count > generating_polls


# -- Tests for OSC 8 hyperlink normalization (issue #47) --------------------


class TestNormalizeCapture:
    """Tests for _normalize_capture — OSC 8 → bare URL + ANSI strip."""

    def test_http_osc8_becomes_text_with_bare_url(self) -> None:
        """`#45` link to GitHub PR becomes `#45 (https://...)`."""
        text = "PR \x1b]8;id=abc;https://github.com/yousan/c-lord/pull/45\x1b\\#45\x1b]8;;\x1b\\"
        result = _normalize_capture(text)
        assert "#45" in result
        assert "https://github.com/yousan/c-lord/pull/45" in result

    def test_file_url_dropped_keep_text_only(self) -> None:
        """file:// URLs are local to bot; keep only the visible text."""
        text = "\x1b]8;id=x;file:///home/y/foo.py\x1b\\foo.py\x1b]8;;\x1b\\"
        result = _normalize_capture(text)
        assert result.strip() == "foo.py"
        assert "file://" not in result

    def test_ansi_color_codes_stripped(self) -> None:
        """CSI color escapes are removed."""
        text = "\x1b[38;5;114m●\x1b[39m \x1b[1mHello\x1b[0m"
        result = _normalize_capture(text)
        assert result == "● Hello"

    def test_osc8_and_ansi_combined(self) -> None:
        """Realistic capture-pane output with both."""
        text = (
            "\x1b[34m\x1b]8;id=1;https://docs.pytest.org/x.html\x1b\\"
            "https://docs.pytest.org/x.html\x1b[39m\x1b]8;;\x1b\\"
        )
        result = _normalize_capture(text)
        # When visible text == URL, emit URL once (no duplication).
        assert result.count("https://docs.pytest.org/x.html") == 1
        assert "\x1b" not in result

    def test_plain_text_unchanged(self) -> None:
        assert _normalize_capture("hello world") == "hello world"

    def test_empty_string(self) -> None:
        assert _normalize_capture("") == ""


class TestExtractResponsePreservesHyperlinks:
    """Issue #47: URLs in TUI markdown links must survive into Discord output."""

    def test_issue_reference_link_preserved(self) -> None:
        """Claude TUI output `[#271](https://...)` (rendered as OSC 8) keeps URL."""
        link = "\x1b]8;id=a;https://github.com/foo/bar/issues/271\x1b\\#271\x1b]8;;\x1b\\"
        pane = "\n".join(
            [
                "❯ list issues",
                "",
                f"● - {link} a bug",
                "",
                "─" * 40,
                "❯",
                "─" * 40,
                "-- INSERT --",
            ]
        )
        result = TmuxClaudeRunner._extract_response(pane)
        assert "#271" in result
        assert "https://github.com/foo/bar/issues/271" in result


class TestContinueFallback:
    """Tests for the --continue → fresh-start fallback (issue #123 Part 2, fix #128).

    --continue is ONLY used when the runner is explicitly constructed with
    try_continue=True (restart-resume path, triggered from on_ready).

    Normal cold starts (new threads, post-/clear) always use fresh start.
    """

    # ── Restart-resume path (try_continue=True) ────────────────────────

    @pytest.mark.asyncio
    async def test_restart_resume_uses_continue(self, tmux_manager) -> None:
        """Restart-resume runner (try_continue=True) sends --continue first."""
        resume_runner = TmuxClaudeRunner(
            tmux_manager=tmux_manager,
            thread_id=12345,
            model="sonnet",
            timeout_seconds=10,
            try_continue=True,
        )
        tmux_manager.is_claude_running.side_effect = [
            False,  # initial check
            True,  # after --continue delay — Claude started successfully
        ]
        pane = _make_pane(["● Resumed."])
        tmux_manager.capture_pane.return_value = pane

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.09),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
            patch("c_lord.claude.tmux_runner._CONTINUE_CHECK_DELAY", 0.01),
        ):
            async for event in resume_runner.run("hello"):
                events.append(event)

        # Only one start_claude call: try_continue=True (--continue succeeded)
        tmux_manager.start_claude.assert_called_once_with(
            12345,
            "hello",
            "sonnet",
            permission_mode="acceptEdits",
            dangerously_skip_permissions=False,
            try_continue=True,
        )

    @pytest.mark.asyncio
    async def test_restart_resume_fallback_when_continue_fails(self, tmux_manager) -> None:
        """If --continue fails (no session), restart-resume runner falls back to fresh."""
        resume_runner = TmuxClaudeRunner(
            tmux_manager=tmux_manager,
            thread_id=12345,
            model="sonnet",
            timeout_seconds=10,
            try_continue=True,
        )
        pane = _make_pane(["● Fresh start response."])
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            return "Loading..." if call_idx <= 2 else pane

        tmux_manager.capture_pane.side_effect = capture_fn
        tmux_manager.is_claude_running.side_effect = [
            False,  # initial check
            False,  # after --continue delay — failed (no history)
        ]
        tmux_manager._find_window_for_thread.return_value = "work1"

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.09),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
            patch("c_lord.claude.tmux_runner._CONTINUE_CHECK_DELAY", 0.01),
        ):
            async for event in resume_runner.run("hello"):
                events.append(event)

        assert tmux_manager.start_claude.call_count == 2
        assert tmux_manager.start_claude.call_args_list[0].kwargs["try_continue"] is True
        assert tmux_manager.start_claude.call_args_list[1].kwargs["try_continue"] is False
        assert any(e.message_type == MessageType.RESULT and e.is_complete for e in events)

    # ── Default (fresh) path — /clear, new threads ─────────────────────

    @pytest.mark.asyncio
    async def test_default_runner_never_uses_continue(self, runner, tmux_manager) -> None:
        """Issue #123 fix: default runner (try_continue=False) must NOT send --continue.

        This is the regression test for the /clear path. After /clear, the window is
        killed and recreated. In the new window, --continue would find ~/.claude history
        and successfully resume the supposedly-cleared context. The fix prevents this
        by never attaching --continue unless the runner was explicitly constructed with
        try_continue=True (restart-resume path only).
        """
        pane = _make_pane(["● Hello, I'm fresh!"])
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            return "Loading..." if call_idx <= 2 else pane

        tmux_manager.capture_pane.side_effect = capture_fn
        # Only ONE is_claude_running call expected (no --continue check delay)
        tmux_manager.is_claude_running.return_value = False
        tmux_manager._find_window_for_thread.return_value = "work1"

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.09),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
        ):
            async for event in runner.run("hello"):
                events.append(event)

        # Exactly ONE start_claude call — direct fresh, no --continue attempt
        tmux_manager.start_claude.assert_called_once_with(
            12345,
            "hello",
            "sonnet",
            permission_mode="acceptEdits",
            dangerously_skip_permissions=False,
            try_continue=False,
        )
        # Only one is_claude_running call (no post-continue check)
        assert tmux_manager.is_claude_running.call_count == 1
        assert any(e.message_type == MessageType.RESULT and e.is_complete for e in events)


# -- Tests for new permission markers (v2.1+) ---------------------------------


class TestPermissionPromptNewMarkers:
    """_has_permission_prompt must detect all known Claude Code v2.1+ variants."""

    def test_do_you_want_to_continue(self) -> None:
        text = "Some context\nDo you want to continue?\n❯ 1. Yes\n  2. No"
        assert TmuxClaudeRunner._has_permission_prompt(text) is True

    def test_allow_fetch_content(self) -> None:
        text = "Do you want to allow Claude to fetch this content?\n❯ 1. Yes\n  2. No"
        assert TmuxClaudeRunner._has_permission_prompt(text) is True

    def test_allow_connection(self) -> None:
        text = "Do you want to allow this connection?\n❯ 1. Yes\n  2. No"
        assert TmuxClaudeRunner._has_permission_prompt(text) is True

    def test_continue_anyway_yn(self) -> None:
        text = "Security warnings found.\nContinue anyway? [y/N]"
        assert TmuxClaudeRunner._has_permission_prompt(text) is True

    def test_existing_proceed_still_detected(self) -> None:
        text = "Do you want to proceed?\n❯ 1. Yes"
        assert TmuxClaudeRunner._has_permission_prompt(text) is True


# -- Tests for y/N prompt detection -------------------------------------------


class TestYesNoPromptDetection:
    """_is_yn_prompt distinguishes [y/N] style from numbered-menu style."""

    def test_detects_yn_format(self) -> None:
        text = "Continue anyway? [y/N]"
        assert TmuxClaudeRunner._is_yn_prompt(text) is True

    def test_detects_yn_uppercase_y(self) -> None:
        text = "Security warning. Continue anyway? [Y/n]"
        assert TmuxClaudeRunner._is_yn_prompt(text) is True

    def test_numbered_menu_is_not_yn(self) -> None:
        text = "Do you want to proceed?\n❯ 1. Yes\n  2. No"
        assert TmuxClaudeRunner._is_yn_prompt(text) is False


# -- Tests for unknown TUI interactive detection ------------------------------


class TestUnknownTuiInteractive:
    """_has_unknown_interactive detects menu cursors not matching known prompts."""

    def test_detects_unknown_numbered_menu(self) -> None:
        # A numbered menu that does NOT match any known marker
        text = (
            "Would you like to stash these changes and continue with teleport?\n"
            "❯ 1. Yes, stash and continue\n"
            "  2. No, abort"
        )
        assert TmuxClaudeRunner._has_unknown_interactive(text) is True

    def test_known_permission_prompt_is_not_unknown(self) -> None:
        # Known prompt — should NOT trigger unknown detection
        text = "Do you want to proceed?\n❯ 1. Yes\n  2. No"
        assert TmuxClaudeRunner._has_unknown_interactive(text) is False

    def test_known_trust_prompt_is_not_unknown(self) -> None:
        text = "Yes, I trust this folder\nEnter to confirm"
        assert TmuxClaudeRunner._has_unknown_interactive(text) is False

    def test_normal_idle_prompt_is_not_unknown(self) -> None:
        # The bare ❯ input prompt (idle state) — NOT an interactive menu
        text = "● Some response\n\n────────\n❯\n────────\n-- INSERT --"
        assert TmuxClaudeRunner._has_unknown_interactive(text) is False

    def test_empty_text_is_not_unknown(self) -> None:
        assert TmuxClaudeRunner._has_unknown_interactive("") is False

    def test_install_prompt_detected(self) -> None:
        text = "Would you like to install this LSP plugin?\n❯ 1. Yes\n  2. No"
        assert TmuxClaudeRunner._has_unknown_interactive(text) is True

    def test_update_prompt_detected(self) -> None:
        text = "A new version of Claude Code is available.\nUpdate now? [y/N]"
        assert TmuxClaudeRunner._has_unknown_interactive(text) is True

    def test_yn_unknown_prompt_detected(self) -> None:
        # A y/N prompt that doesn't match any known marker
        text = "Would you like to enable auto-connect to IDE? [y/N]"
        assert TmuxClaudeRunner._has_unknown_interactive(text) is True


# -- Tests for y/N auto-accept ------------------------------------------------


class TestAcceptPermissionPromptYN:
    """_accept_permission_prompt sends 'y' for [y/N] prompts, Enter for numbered menus."""

    @pytest.mark.asyncio
    async def test_yn_prompt_sends_y(self, runner, tmux_manager) -> None:
        tmux_manager._find_window_for_thread.return_value = "work1"
        pane_with_yn = "Continue anyway? [y/N]"

        with patch("c_lord.tmux._run") as mock_run:
            await runner._accept_permission_prompt(pane_with_yn)
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            # Should send "y" not bare Enter
            assert "y" in args

    @pytest.mark.asyncio
    async def test_numbered_menu_sends_enter(self, runner, tmux_manager) -> None:
        tmux_manager._find_window_for_thread.return_value = "work1"
        pane_numbered = "Do you want to proceed?\n❯ 1. Yes\n  2. No"

        with patch("c_lord.tmux._run") as mock_run:
            await runner._accept_permission_prompt(pane_numbered)
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "Enter" in args


# -- Regression tests for #156 (y-spam bug: conversation text triggers auto-accept) ----

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "panes"


def _load_fixture(name: str) -> str:
    return (_FIXTURES_DIR / name).read_text()


class TestPermissionPromptTailAnchor:
    """Regression suite for #156: detection must not fire on conversation-body markers.

    Each test loads a real captured pane fixture so that future breakage is
    immediately reproducible ('this pane snapshot broke things').
    """

    def test_bug156_yn_in_conversation_no_permission_trigger(self) -> None:
        """Conversation text containing [y/N] MUST NOT trigger _has_permission_prompt."""
        pane = _load_fixture("bug_156_yn_in_conversation.txt")
        assert TmuxClaudeRunner._has_permission_prompt(pane) is False

    def test_bug156_yn_in_conversation_no_yn_trigger(self) -> None:
        """Conversation text containing [y/N] MUST NOT trigger _is_yn_prompt."""
        pane = _load_fixture("bug_156_yn_in_conversation.txt")
        assert TmuxClaudeRunner._is_yn_prompt(pane) is False

    def test_real_yn_prompt_at_bottom_still_detected(self) -> None:
        """A real [y/N] prompt at the bottom of the pane MUST be detected."""
        pane = _load_fixture("real_yn_prompt_at_bottom.txt")
        assert TmuxClaudeRunner._has_permission_prompt(pane) is True

    def test_real_yn_prompt_is_yn(self) -> None:
        """A real [y/N] prompt at the bottom MUST be identified as y/N style."""
        pane = _load_fixture("real_yn_prompt_at_bottom.txt")
        assert TmuxClaudeRunner._is_yn_prompt(pane) is True

    def test_bug156_conversation_unknown_interactive_not_triggered(self) -> None:
        """Conversation-body markers must not trigger unknown-interactive detection."""
        pane = _load_fixture("bug_156_yn_in_conversation.txt")
        assert TmuxClaudeRunner._has_unknown_interactive(pane) is False


# -- Regression test for #179 (generating indicator carries trailing stats) ------


class TestIsGeneratingWithStats:
    """Regression for #179: the live generation indicator is rendered as
    ``✽ Generating… (7m 45s · ↑ 23.2k tokens)`` — the ``…`` is followed by an
    elapsed/token suffix, so it is NOT at the end of the line.  Detection that
    only matched ``endswith('…')`` missed it, so c-lord treated an actively
    generating session as idle and finalized the turn early.
    """

    def test_generating_with_trailing_token_stats_detected(self) -> None:
        pane = _load_fixture("bug_179_generating_with_stats.txt")
        assert TmuxClaudeRunner._is_generating(pane) is True


# -- Regression tests for #153 (plan/ask menus flagged as unknown) ---------------


class TestKnownInteractiveMenusNotFlaggedAsUnknown:
    """Regression for #153: ExitPlanMode and AskUserQuestion menus must not
    trigger unknown_tui_prompt_embed.  These are KNOWN prompts handled
    elsewhere (Discord buttons via EventProcessor / TranscriptMirrorCog).
    """

    def test_plan_approval_not_unknown(self) -> None:
        """ExitPlanMode 'Would you like to proceed?' menu MUST NOT be flagged."""
        pane = _load_fixture("plan_approval_menu.txt")
        assert TmuxClaudeRunner._has_unknown_interactive(pane) is False

    def test_ask_user_question_not_unknown(self) -> None:
        """AskUserQuestion numbered menu MUST NOT be flagged as unknown."""
        pane = _load_fixture("ask_user_question_menu.txt")
        assert TmuxClaudeRunner._has_unknown_interactive(pane) is False

    def test_real_unknown_menu_still_detected(self) -> None:
        """A truly unknown menu (e.g. LSP install) MUST still be detected."""
        pane = (
            "Would you like to install the LSP plugin?\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────\n"
            "-- INSERT --"
        )
        assert TmuxClaudeRunner._has_unknown_interactive(pane) is True


class TestGhostTextInputNotFlagged:
    """Regression for #62: a real ``capture-pane -e`` snapshot of the Claude
    Code TUI showing ghost/placeholder text in the input box.

    The fixture was captured live from ``claude`` v2.1.152 (not hand-written):
    the input box renders ``❯`` + a non-breaking space (``\\xa0``) + dim
    placeholder text (``Try "create a util ..."``).  A hand-made fixture used a
    regular space and so hid the real structure — the live box uses ``\\xa0``
    while a *sent* user message uses a regular space.  Detection runs on the
    NORMALISED capture, exactly as the run loop does.
    """

    FIXTURE = "bug_62_ghost_text_real.txt"

    def _norm(self) -> str:
        return _normalize_capture(_load_fixture(self.FIXTURE))

    def test_ghost_text_no_permission_prompt(self) -> None:
        """Ghost text in input area MUST NOT trigger _has_permission_prompt."""
        assert TmuxClaudeRunner._has_permission_prompt(self._norm()) is False

    def test_ghost_text_no_yn_prompt(self) -> None:
        """Ghost text in input area MUST NOT trigger _is_yn_prompt."""
        assert TmuxClaudeRunner._is_yn_prompt(self._norm()) is False

    def test_ghost_text_no_unknown_interactive(self) -> None:
        """Ghost text in input area MUST NOT trigger _has_unknown_interactive."""
        assert TmuxClaudeRunner._has_unknown_interactive(self._norm()) is False

    def test_ghost_text_recognized_as_ready_prompt(self) -> None:
        """#62: ghost/placeholder text in the input box still means Claude is
        idle and waiting at the prompt.  ``_has_input_prompt`` MUST return True
        so the turn completes promptly instead of misreading the input box as
        "still busy" until the 30s fallback fires.

        This is the RED case: the box line is ``❯\\xa0Try "..."`` sitting above a
        tall bottom chrome (separator + 3 ccstatusline rows + ``-- INSERT --`` +
        effort footer), so the old bare-``❯``/6-line-window logic returns False.
        """
        assert TmuxClaudeRunner._has_input_prompt(self._norm()) is True

    def test_ghost_text_not_leaked_into_response(self) -> None:
        """#62: the input-box ghost text must never be read as confirmed input,
        i.e. it must not leak into the extracted response posted to Discord.
        """
        response = TmuxClaudeRunner._extract_response(_load_fixture(self.FIXTURE))
        assert "Try" not in response
        assert "logging.py" not in response

    def test_sent_message_is_not_a_live_prompt(self) -> None:
        """A *sent* user message (``❯ <text>`` with a regular space) near the
        bottom must NOT be treated as the live input box — only the ``❯\\xa0``
        (NBSP) form or a bare ``❯`` is the live prompt.
        """
        pane = (
            "● answer\n"
            "────────────────────────\n"
            "❯ 2+2 は？ 数字だけ答えて\n"  # sent message: regular space after ❯
            "────────────────────────\n"
            "Model: Sonnet\nCost: $0\n⎇ main\n"
            "-- INSERT --\n"
            "● high · /effort\n"
        )
        assert TmuxClaudeRunner._has_input_prompt(pane) is False


# -- Tests for #166 (AskUserQuestion → Discord buttons in tmux/jsonl mode) -----


class TestParseAskFromPane:
    """#166: parse the AskUserQuestion TUI menu from a real captured pane into
    a structured AskQuestion (question, header, real options) so it can be
    rendered as Discord buttons.  Meta-options ('Type something.', 'Chat about
    this') are excluded — they are TUI affordances, not real choices.
    """

    def test_parses_question_header_and_options(self) -> None:
        pane = _load_fixture("ask_user_question_3options.txt")
        q = _parse_ask_from_pane(pane)
        assert q is not None
        assert q.header == "Deploy"
        assert q.question == "Which environment?"
        labels = [o.label for o in q.options]
        assert labels == ["Production", "Staging", "Local"]

    def test_meta_options_excluded(self) -> None:
        """'Type something.' / 'Chat about this' must not become buttons."""
        pane = _load_fixture("ask_user_question_3options.txt")
        q = _parse_ask_from_pane(pane)
        assert q is not None
        labels = [o.label for o in q.options]
        assert "Type something." not in labels
        assert "Chat about this" not in labels

    def test_captures_option_descriptions(self) -> None:
        """#169: each option's indented description line must be captured so it
        can be shown in the embed (Discord buttons can't display descriptions).
        """
        pane = _load_fixture("ask_user_question_3options.txt")
        q = _parse_ask_from_pane(pane)
        assert q is not None
        descriptions = [o.description for o in q.options]
        assert descriptions == ["本番", "検証", "ローカル"]

    def test_plan_approval_is_not_ask(self) -> None:
        """A Plan-approval menu is NOT an AskUserQuestion → returns None."""
        pane = _load_fixture("plan_approval_menu.txt")
        assert _parse_ask_from_pane(pane) is None

    def test_only_active_bottom_menu_parsed_not_scrollback(self) -> None:
        """Regression (#166 staging): old menus in scrollback must be ignored.

        capture-pane returns 500 lines of history; a previous AskUserQuestion
        menu still sits above the current one.  Parsing the whole buffer pulled
        in stale options and produced duplicate Discord Select values (400 Bad
        Request).  Only the active (bottom-most) menu must be parsed.
        """
        old_menu = (
            " ☐ OldHeader\n"
            "Old question?\n"
            "❯ 1. AlphaOld\n"
            "  2. BetaOld\n"
            "  3. Type something.\n"
            "  4. Chat about this\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        new_menu = (
            " ☐ Region\n"
            "Which region?\n"
            "❯ 1. Tokyo\n"
            "  2. Osaka\n"
            "  3. Type something.\n"
            "  4. Chat about this\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        pane = "old conversation\n" + old_menu + "\nmore output\n" + new_menu
        q = _parse_ask_from_pane(pane)
        assert q is not None
        assert q.header == "Region"
        assert q.question == "Which region?"
        labels = [o.label for o in q.options]
        assert labels == ["Tokyo", "Osaka"], f"leaked scrollback options: {labels}"

    def test_idle_pane_is_not_ask(self) -> None:
        pane = "● Some response\n\n────────\n❯\n────────\n-- INSERT --"
        assert _parse_ask_from_pane(pane) is None

    def test_ask_menu_not_flagged_as_unknown(self) -> None:
        """Regression: the current AskUserQuestion variant (Type something. /
        Chat about this, no 'Other') must NOT trip the unknown-prompt warning —
        #153's markers were stale for Claude Code v2.1.150.
        """
        pane = _load_fixture("ask_user_question_3options.txt")
        assert TmuxClaudeRunner._has_unknown_interactive(pane) is False


class TestRunYieldsPaneAsk:
    """#166: the run() loop must surface an AskUserQuestion menu as a single
    pane_ask StreamEvent so the EventProcessor can show Discord buttons.
    """

    @pytest.mark.asyncio
    async def test_run_yields_single_pane_ask(self, runner, tmux_manager) -> None:
        ask_pane = _load_fixture("ask_user_question_3options.txt")
        tmux_manager.is_claude_running.return_value = True
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            # Menu lingers for several polls, then resolves to a done pane.
            if call_idx <= 8:
                return ask_pane
            return _DONE_PANE

        tmux_manager.capture_pane.side_effect = capture_fn

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
            patch("c_lord.claude.tmux_runner._ASK_ALERT_DELAY", 0.04),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        ):
            async for event in runner.run("test"):
                events.append(event)

        ask_events = [e for e in events if e.pane_ask is not None]
        assert len(ask_events) == 1, f"expected 1 pane_ask event, got {len(ask_events)}"
        labels = [o.label for o in ask_events[0].pane_ask.options]
        assert labels == ["Production", "Staging", "Local"]

    @pytest.mark.asyncio
    async def test_no_idle_timeout_while_generating_then_ask(self, runner, tmux_manager) -> None:
        """The run must not idle-timeout while Claude is still thinking (#166).

        AskUserQuestion can appear only after a long 'Cogitating…' phase.  If the
        idle timeout fires during generation the runner stops polling and never
        sees the menu — exactly what happened on staging.
        """
        ask_pane = _load_fixture("ask_user_question_3options.txt")
        tmux_manager.is_claude_running.return_value = True
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            if call_idx <= 10:  # "thinking" far longer than the (patched) idle timeout
                # The pane keeps changing each poll (elapsed-seconds tick) — no
                # response text yet.  This is what must keep the run alive.
                return f"\n✻ Cogitating for {call_idx}s\n────────\n❯\n────────\n-- INSERT --"
            return ask_pane

        tmux_manager.capture_pane.side_effect = capture_fn

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
            patch("c_lord.claude.tmux_runner._IDLE_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._ASK_ALERT_DELAY", 0.04),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        ):
            async for event in runner.run("test"):
                events.append(event)
                if event.pane_ask is not None:
                    break

        ask_events = [e for e in events if e.pane_ask is not None]
        assert len(ask_events) == 1, "idle timeout fired during generation; menu never bridged"

    @pytest.mark.asyncio
    async def test_run_normalises_ansi_capture_before_detect(self, runner, tmux_manager) -> None:
        """Regression (#166 staging): the Claude TUI emits ANSI colour codes
        that split "❯" from "1.".  The run loop must normalise the capture
        before parsing, or the menu is never detected.  This fixture is the
        real escape-coded pane that failed on staging.
        """
        ansi_pane = _load_fixture("ask_user_question_ansi_raw.txt")
        # Sanity: the raw fixture really does defeat the parser.
        assert _parse_ask_from_pane(ansi_pane) is None

        tmux_manager.is_claude_running.return_value = True
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            if call_idx <= 8:
                return ansi_pane
            return _DONE_PANE

        tmux_manager.capture_pane.side_effect = capture_fn

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
            patch("c_lord.claude.tmux_runner._ASK_ALERT_DELAY", 0.04),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        ):
            async for event in runner.run("test"):
                events.append(event)
                if event.pane_ask is not None:
                    break

        ask_events = [e for e in events if e.pane_ask is not None]
        assert len(ask_events) == 1, "ANSI capture not normalised → menu missed"
        assert [o.label for o in ask_events[0].pane_ask.options] == ["Coffee", "Tea", "Water"]

    @pytest.mark.asyncio
    async def test_answer_menu_sends_each_key_separately(self, runner, tmux_manager) -> None:
        """#171: keys must be sent ONE PER send_keys call (with delays between),
        not batched — a single `send-keys Down Down Enter` is too fast and the
        TUI drops the Down navigations, selecting the wrong (first) option.
        """
        from unittest.mock import call

        with patch("c_lord.claude.tmux_runner._MENU_NAV_DELAY", 0.0):
            await runner.answer_menu(2)
        assert tmux_manager.send_keys.call_args_list == [
            call(12345, "Down"),
            call(12345, "Down"),
            call(12345, "Enter"),
        ]

    @pytest.mark.asyncio
    async def test_answer_menu_zero_just_enter(self, runner, tmux_manager) -> None:
        """answer_menu(0) selects the first (already-highlighted) option."""
        with patch("c_lord.claude.tmux_runner._MENU_NAV_DELAY", 0.0):
            await runner.answer_menu(0)
        tmux_manager.send_keys.assert_called_once_with(12345, "Enter")

    @pytest.mark.asyncio
    async def test_answer_menu_text_types_onto_row_then_confirms(self, runner, tmux_manager) -> None:
        """#172: free text is typed ONTO the highlighted 'Type something.' row,
        then confirmed with Enter.

        Verified on a live Claude Code v2.1.150 TUI:
        - Navigating to 'Type something.' and pressing Enter registers a
          *decline* (no input field opens).
        - Submitting the text via send_input would post it as a SEPARATE
          message, not the AskUserQuestion answer.
        - Typing literal text while the row is highlighted replaces its label
          with the text; a final Enter records it as the answer.

        So the order must be: Down×N (NO Enter) → send_literal(text) → Enter.
        """
        from unittest.mock import call

        with patch("c_lord.claude.tmux_runner._MENU_NAV_DELAY", 0.0):
            await runner.answer_menu_text(2, "melon")

        # Ordered across send_keys + send_literal.
        relevant = [c for c in tmux_manager.mock_calls if c[0] in ("send_keys", "send_literal")]
        assert relevant == [
            call.send_keys(12345, "Down"),
            call.send_keys(12345, "Down"),
            call.send_literal(12345, "melon"),
            call.send_keys(12345, "Enter"),
        ]
        # Must NOT submit via send_input (that adds Enter + posts a separate msg).
        tmux_manager.send_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_answer_menu_text_index_zero_no_navigation(self, runner, tmux_manager) -> None:
        """When the text row is already highlighted (index 0): type then Enter."""
        from unittest.mock import call

        with patch("c_lord.claude.tmux_runner._MENU_NAV_DELAY", 0.0):
            await runner.answer_menu_text(0, "kiwi")

        relevant = [c for c in tmux_manager.mock_calls if c[0] in ("send_keys", "send_literal")]
        assert relevant == [
            call.send_literal(12345, "kiwi"),
            call.send_keys(12345, "Enter"),
        ]
        tmux_manager.send_input.assert_not_called()


# -- Regression tests for #165 (unknown_tui_prompt re-fire spam) ---------------

_UNKNOWN_MENU_A = (
    "● Bash(python3 menu.py)\n"
    "  ⎿  Select deployment target:\n"
    "     ❯ 1. Production\n"
    "       2. Staging\n"
    "       3. Cancel\n"
    "✻ Cogitated for {n}s\n"
    "────────\n❯\n────────\n-- INSERT --"
)

_UNKNOWN_MENU_B = (
    "● Bash(python3 other.py)\n"
    "  ⎿  Choose a branch:\n"
    "     ❯ 1. main\n"
    "       2. develop\n"
    "✻ Cogitated for {n}s\n"
    "────────\n❯\n────────\n-- INSERT --"
)

_DONE_PANE = "● All done!\n\n────────\n❯\n────────\n-- INSERT --"


class TestUnknownPromptDedup:
    """Regression for #165: an unknown menu that lingers in the pane must
    trigger the unknown_tui_prompt embed only ONCE, not every ~5s.  The
    volatile chrome (spinner / elapsed seconds) must not defeat the dedup.
    A *different* menu, or the same menu reappearing after it cleared, must
    alert again.
    """

    @pytest.mark.asyncio
    async def test_same_menu_fires_once(self, runner, tmux_manager) -> None:
        """A lingering unknown menu yields exactly one unknown_tui_prompt event."""
        tmux_manager.is_claude_running.return_value = True
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            # Same menu for many polls (volatile spinner seconds change each poll),
            # then a completed pane so the run loop can finish.
            if call_idx <= 12:
                return _UNKNOWN_MENU_A.format(n=call_idx)
            return _DONE_PANE

        tmux_manager.capture_pane.side_effect = capture_fn

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
            patch("c_lord.claude.tmux_runner._UNKNOWN_ALERT_DELAY", 0.04),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        ):
            async for event in runner.run("test"):
                events.append(event)

        unknown_events = [e for e in events if e.unknown_tui_prompt is not None]
        assert len(unknown_events) == 1, (
            f"expected exactly 1 unknown_tui_prompt event, got {len(unknown_events)}"
        )

    @pytest.mark.asyncio
    async def test_different_menu_fires_again(self, runner, tmux_manager) -> None:
        """A second, distinct unknown menu must alert again (not be suppressed)."""
        tmux_manager.is_claude_running.return_value = True
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            if call_idx <= 6:
                return _UNKNOWN_MENU_A.format(n=call_idx)
            if call_idx <= 12:
                return _UNKNOWN_MENU_B.format(n=call_idx)
            return _DONE_PANE

        tmux_manager.capture_pane.side_effect = capture_fn

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
            patch("c_lord.claude.tmux_runner._UNKNOWN_ALERT_DELAY", 0.04),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        ):
            async for event in runner.run("test"):
                events.append(event)

        unknown_events = [e for e in events if e.unknown_tui_prompt is not None]
        assert len(unknown_events) == 2, (
            f"expected 2 unknown_tui_prompt events (menu A then B), got {len(unknown_events)}"
        )


# -- Regression tests for the folder-trust dialog (Quick safety check) ---------


class TestTrustPromptTopAnchored:
    """The folder-trust dialog ("Quick safety check…") is a TOP-anchored
    full-screen prompt: its markers sit near the top of the pane while the
    bottom rows are blank.  So the bottom 'permission zone' is empty and none
    of the zone-based detectors see it — which is exactly why a fresh-clone
    session used to stall at the dialog.  It must be matched against the FULL
    pane and accepted in the main poll loop.
    """

    def test_real_trust_prompt_detected(self) -> None:
        pane = _load_fixture("trust_prompt_at_top.txt")
        assert TmuxClaudeRunner._has_trust_prompt(pane) is True

    def test_zone_detectors_are_blind_to_top_anchored_dialog(self) -> None:
        pane = _load_fixture("trust_prompt_at_top.txt")
        # All bottom-zone detectors miss the top-anchored dialog: this is the
        # bug — handling it requires a full-pane check in the main loop.
        assert TmuxClaudeRunner._has_permission_prompt(pane) is False
        assert TmuxClaudeRunner._is_yn_prompt(pane) is False
        assert TmuxClaudeRunner._has_unknown_interactive(pane) is False

    def test_prose_mentioning_the_dialog_does_not_trigger(self) -> None:
        # Detection keys on the menu OPTION LINE, not a loose substring.  The
        # runner captures ~500 lines of scrollback, so a session that merely
        # discusses the dialog — even quoting BOTH marker phrases — must NOT
        # trip a spurious Enter into the input (regression for the #180 fix).
        prose = (
            '● The trust dialog shows "Yes, I trust this folder" as option 1;\n'
            "  the user presses Enter to confirm to accept it.\n"
            "────────\n❯\n────────\n-- INSERT -- bypass permissions on"
        )
        assert "Yes, I trust this folder" in prose  # both phrases present...
        assert "Enter to confirm" in prose
        assert TmuxClaudeRunner._has_trust_prompt(prose) is False  # ...yet not a dialog

    def test_menu_line_without_cursor_still_detected(self) -> None:
        # The cursor (❯) may render on a different option; the "1." menu line
        # itself is the stable signature.
        text = "Quick safety check\n  1. Yes, I trust this folder\n  2. No, exit"
        assert TmuxClaudeRunner._has_trust_prompt(text) is True


class TestRunAutoAcceptsTrustPrompt:
    """run()'s main poll loop must auto-accept the folder-trust dialog.

    Reproduces the stall: the cold-start handler (_handle_startup_prompts)
    races the dialog and often bails before it renders, so the main loop is
    the backstop.  Here is_claude_running=True skips the cold-start path, so
    the main loop is the *only* thing that can accept the dialog.
    """

    @pytest.mark.asyncio
    async def test_main_loop_sends_enter_on_trust_prompt(self, runner, tmux_manager) -> None:
        trust_pane = _load_fixture("trust_prompt_at_top.txt")
        tmux_manager.is_claude_running.return_value = True
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            # Dialog lingers until accepted, then the session proceeds to done.
            if call_idx <= 6:
                return trust_pane
            return _DONE_PANE

        tmux_manager.capture_pane.side_effect = capture_fn

        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        ):
            async for _ in runner.run("test"):
                pass

        # The dialog defaults to option 1 ("Yes, I trust this folder"); Enter
        # confirms it.
        enter_calls = [c for c in tmux_manager.send_keys.call_args_list if "Enter" in c.args]
        assert enter_calls, "main loop did not send Enter to accept the trust dialog"
