"""Tests for TmuxClaudeRunner."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner
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
        """When screen redraws, find overlap between old tail and new head."""
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
        """If no trust prompt appears, returns after a few seconds."""
        tmux_manager.capture_pane.return_value = "Loading..."
        tmux_manager._find_window_for_thread.return_value = "work1"

        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.2),
        ):
            await runner._handle_startup_prompts()

        # Should return without error

    @pytest.mark.asyncio
    async def test_handles_trust_prompt(self, runner, tmux_manager) -> None:
        """Detects trust prompt and sends Enter."""
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

        # Should have sent Enter via _run
        mock_run.assert_called_once()


class TestTmuxClaudeRunnerRun:
    """Tests for the run() async generator."""

    @pytest.mark.asyncio
    async def test_start_claude_fresh(self, runner, tmux_manager) -> None:
        """When Claude is not running, start_claude is called with the prompt."""
        # _handle_startup_prompts consumes captures until elapsed >= _STARTUP_TIMEOUT
        # With _STARTUP_TIMEOUT=0.06, _POLL_INTERVAL=0.05: 1 iteration
        # Then run() takes 1 snapshot + polling captures
        poll_captures = [
            "Loading...",  # startup prompt check (1 iteration)
            "",  # snapshot after startup
            "Hello!",  # first poll
            "Hello!\n❯",  # input prompt detected
        ]
        tmux_manager.capture_pane.side_effect = poll_captures
        tmux_manager.is_claude_running.return_value = False
        tmux_manager._find_window_for_thread.return_value = "work1"

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.06),
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
        """When Claude is already running, send_input is used instead of start_claude."""
        tmux_manager.is_claude_running.return_value = True
        capture_sequence = [
            "existing content",  # after-send snapshot
            "existing content\nNew response",  # poll
            "existing content\nNew response\n❯",  # prompt
        ]
        tmux_manager.capture_pane.side_effect = capture_sequence

        events = []
        with patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05):
            async for event in runner.run("follow up"):
                events.append(event)

        tmux_manager.send_input.assert_called_once_with(12345, "follow up")
        tmux_manager.start_claude.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_claude_failure_yields_error(self, runner, tmux_manager) -> None:
        """When start_claude fails, an error event is yielded."""
        tmux_manager.capture_pane.return_value = ""
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
        """When send_input fails (already running), an error event is yielded."""
        tmux_manager.capture_pane.return_value = ""
        tmux_manager.is_claude_running.return_value = True
        tmux_manager.send_input.return_value = False

        events = []
        async for event in runner.run("test"):
            events.append(event)

        assert len(events) == 1
        assert events[0].is_complete
        assert events[0].error == "Failed to send input to Claude in tmux"

    @pytest.mark.asyncio
    async def test_text_delta_yielded_as_partial(self, runner, tmux_manager) -> None:
        """Text changes are yielded as partial ASSISTANT events."""
        # _handle_startup_prompts (1 iteration) + snapshot + polling
        capture_sequence = [
            "Loading...",  # startup prompt check (1 iteration)
            "",  # snapshot after startup
            "Line 1",  # poll 1
            "Line 1\nLine 2",  # poll 2
            "Line 1\nLine 2\n❯",  # prompt → done
        ]
        tmux_manager.capture_pane.side_effect = capture_sequence
        tmux_manager.is_claude_running.return_value = False

        events = []
        with (
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
            patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.06),
        ):
            async for event in runner.run("test"):
                events.append(event)

        # Should have partial events + final non-partial + result
        partials = [e for e in events if e.message_type == MessageType.ASSISTANT and e.is_partial]
        assert len(partials) >= 1

        finals = [e for e in events if e.message_type == MessageType.ASSISTANT and not e.is_partial]
        assert len(finals) == 1

        result = [e for e in events if e.message_type == MessageType.RESULT]
        assert len(result) == 1
        assert result[0].is_complete

    @pytest.mark.asyncio
    async def test_idle_timeout_completes(self, runner, tmux_manager) -> None:
        """When no text change happens for _IDLE_TIMEOUT, the run completes."""
        # Return ready prompt then static text forever
        tmux_manager.is_claude_running.return_value = True
        tmux_manager.capture_pane.return_value = "static text"

        runner.timeout_seconds = 60
        events = []
        with (
            patch("c_lord.claude.tmux_runner._IDLE_TIMEOUT", 0.3),
            patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05),
        ):
            async for event in runner.run("test"):
                events.append(event)

        result_events = [e for e in events if e.is_complete]
        assert len(result_events) == 1

    @pytest.mark.asyncio
    async def test_start_claude_failure_from_start(self, runner, tmux_manager) -> None:
        """If start_claude returns False, yields an error immediately."""
        tmux_manager.is_claude_running.return_value = False
        tmux_manager.start_claude.return_value = False

        events = []
        async for event in runner.run("test"):
            events.append(event)

        assert len(events) == 1
        assert events[0].is_complete
        assert events[0].error == "Failed to start Claude in tmux"


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

        with patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.05):
            task = asyncio.create_task(interrupt_after_delay())
            async for event in runner.run("test"):
                events.append(event)
            await task

        result_events = [e for e in events if e.is_complete]
        assert len(result_events) == 1
        assert result_events[0].error == "Stopped by user"
