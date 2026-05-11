"""Tests for TmuxClaudeRunner."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner, _clean_tui_lines, _normalize_capture
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
            "Yes, I trust this folder\nEnter to confirm",
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
        """When Claude is not running, start_claude is called."""
        pane = _make_pane(["● Hello!"])
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            # First few calls are during _handle_startup_prompts.
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

        tmux_manager.start_claude.assert_called_once_with(
            12345,
            "test prompt",
            "sonnet",
            permission_mode="acceptEdits",
            dangerously_skip_permissions=False,
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
