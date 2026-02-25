"""Tests for TmuxClaudeRunner."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner, _clean_tui_lines
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


# -- Tests for _clean_tui_lines ---------------------------------------------


class TestCleanTuiLines:
    """Tests for the _clean_tui_lines helper function."""

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

        assert len(events) == 1
        assert events[0].is_complete
        assert events[0].error == "Failed to start Claude in tmux"

    @pytest.mark.asyncio
    async def test_send_input_failure_yields_error(self, runner, tmux_manager) -> None:
        tmux_manager.is_claude_running.return_value = True
        tmux_manager.send_input.return_value = False

        events = []
        async for event in runner.run("test"):
            events.append(event)

        assert len(events) == 1
        assert events[0].is_complete
        assert events[0].error == "Failed to send input to Claude in tmux"

    @pytest.mark.asyncio
    async def test_extracted_text_yielded_as_partial(self, runner, tmux_manager) -> None:
        """Extracted response text is yielded as partial ASSISTANT events."""
        pane_v1 = _make_pane(["● Line 1"])
        pane_v2 = _make_pane(["● Line 1", "  Line 2"])
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            if call_idx <= 2:
                return "Loading..."  # _handle_startup_prompts
            if call_idx == 3:
                return pane_v1  # first version
            return pane_v2  # final version (grows then stabilises)

        tmux_manager.capture_pane.side_effect = capture_fn
        tmux_manager.is_claude_running.return_value = False

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.06),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.09),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
        ):
            async for event in runner.run("test"):
                events.append(event)

        partials = [e for e in events if e.message_type == MessageType.ASSISTANT and e.is_partial]
        assert len(partials) >= 1
        # The extracted text should be clean (no TUI markers).
        assert "●" not in partials[-1].text

        finals = [e for e in events if e.message_type == MessageType.ASSISTANT and not e.is_partial]
        assert len(finals) == 1

        result = [e for e in events if e.message_type == MessageType.RESULT]
        assert len(result) == 1
        assert result[0].is_complete

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

    @pytest.mark.asyncio
    async def test_response_stability_detection(self, runner, tmux_manager) -> None:
        """Response stabilising for _RESPONSE_STABLE_TIMEOUT triggers completion."""
        tmux_manager.is_claude_running.return_value = True
        pane_v1 = _make_pane(["● Growing..."], user_prompt="q")
        pane_v2 = _make_pane(["● Growing...", "  Done now."], user_prompt="q")
        call_idx = 0

        def capture_fn(tid):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return pane_v1
            return pane_v2

        tmux_manager.capture_pane.side_effect = capture_fn

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.09),
            patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.01),
        ):
            async for event in runner.run("q"):
                events.append(event)

        # Should have partial events as response grew.
        partials = [e for e in events if e.message_type == MessageType.ASSISTANT and e.is_partial]
        assert len(partials) == 2  # v1 and v2
        assert "Done now." in partials[-1].text

        # Should have final non-partial and result.
        finals = [e for e in events if e.message_type == MessageType.ASSISTANT and not e.is_partial]
        assert len(finals) == 1
        result = [e for e in events if e.message_type == MessageType.RESULT]
        assert len(result) == 1
        assert result[0].is_complete
        assert result[0].error is None


# -- Tests for interrupt/kill ------------------------------------------------


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
