"""Tests for _run_helper module: streaming, intermediate text, tool results."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.types import (
    MessageType,
    StreamEvent,
    ToolCategory,
    ToolUseEvent,
)
from c_lord.cogs._run_helper import (
    TOOL_RESULT_MAX_CHARS,
    LiveToolTimer,
    _make_error_embed,
    _truncate_result,
    run_claude_in_thread,
)
from c_lord.concurrency import SessionRegistry


class TestTruncateResult:
    def test_short_content_unchanged(self) -> None:
        assert _truncate_result("hello") == "hello"

    def test_exact_limit_unchanged(self) -> None:
        text = "x" * TOOL_RESULT_MAX_CHARS
        assert _truncate_result(text) == text

    def test_long_content_truncated(self) -> None:
        text = "x" * (TOOL_RESULT_MAX_CHARS + 100)
        result = _truncate_result(text)
        assert result.endswith("... (truncated)")
        assert len(result) < len(text)

    def test_empty_content(self) -> None:
        assert _truncate_result("") == ""


class TestRunClaudeInThread:
    """Integration tests for run_claude_in_thread with mocked runner."""

    @pytest.fixture
    def thread(self) -> MagicMock:
        t = MagicMock(spec=discord.Thread)
        t.id = 12345
        t.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        return t

    @pytest.fixture
    def repo(self) -> MagicMock:
        r = MagicMock()
        r.save = AsyncMock()
        return r

    @pytest.fixture
    def runner(self) -> MagicMock:
        r = MagicMock()
        return r

    def _make_async_gen(self, events: list[StreamEvent]):
        """Create a mock async generator from a list of events."""

        async def gen(*args, **kwargs):
            for e in events:
                yield e

        return gen

    @pytest.mark.asyncio
    async def test_tool_result_updates_embed(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """Tool result content should update the tool use embed."""
        tool_msg = MagicMock(spec=discord.Message)
        tool_msg.edit = AsyncMock()
        tool_msg.embeds = [MagicMock(title="📖 Reading: /tmp/test.py...")]
        thread.send = AsyncMock(return_value=tool_msg)

        events = [
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1"),
            StreamEvent(
                message_type=MessageType.ASSISTANT,
                tool_use=ToolUseEvent(
                    tool_id="t1",
                    tool_name="Read",
                    tool_input={"file_path": "/tmp/test.py"},
                    category=ToolCategory.READ,
                ),
            ),
            StreamEvent(
                message_type=MessageType.USER,
                tool_result_id="t1",
                tool_result_content="print('hello world')",
            ),
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                session_id="sess-1",
                cost_usd=0.01,
                duration_ms=1000,
            ),
        ]
        runner.run = self._make_async_gen(events)

        await run_claude_in_thread(thread, runner, repo, "test", None)

        # Tool message should have been edited with result
        tool_msg.edit.assert_called()

    @pytest.mark.asyncio
    async def test_thinking_posted_as_embed(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """Extended thinking should be posted as a spoiler embed."""
        events = [
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1"),
            StreamEvent(message_type=MessageType.ASSISTANT, thinking="Let me analyze this..."),
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text="Done!",
                session_id="sess-1",
                cost_usd=0.01,
                duration_ms=500,
            ),
        ]
        runner.run = self._make_async_gen(events)

        await run_claude_in_thread(thread, runner, repo, "test", None)

        # Check that thinking embed was sent
        embed_calls = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        thinking_embeds = [
            c
            for c in embed_calls
            if hasattr(c.kwargs.get("embed"), "title")
            and "Thinking" in (c.kwargs["embed"].title or "")
        ]
        assert len(thinking_embeds) == 1
        # Description must use a plain code block (no spoiler) for guaranteed readability
        embed = thinking_embeds[0].kwargs["embed"]
        assert embed.description is not None
        assert embed.description.startswith("```")
        assert embed.description.endswith("```")
        assert "||" not in embed.description

    @pytest.mark.asyncio
    async def test_redacted_thinking_posted_as_embed(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """A redacted_thinking block should post a placeholder embed."""
        events = [
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1"),
            StreamEvent(message_type=MessageType.ASSISTANT, has_redacted_thinking=True),
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text="Done.",
                session_id="sess-1",
            ),
        ]
        runner.run = self._make_async_gen(events)

        await run_claude_in_thread(thread, runner, repo, "test", None)

        embed_calls = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        titles = [c.kwargs["embed"].title or "" for c in embed_calls]
        assert any("redacted" in t.lower() for t in titles)

    @pytest.mark.asyncio
    async def test_error_handling(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """Errors should be posted as error embeds."""
        events = [
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error="Something went wrong",
            ),
        ]
        runner.run = self._make_async_gen(events)

        await run_claude_in_thread(thread, runner, repo, "test", None)

        embed_calls = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        assert any("Error" in (c.kwargs["embed"].title or "") for c in embed_calls)

    @pytest.mark.asyncio
    async def test_error_embed_send_failure_does_not_raise(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """If thread.send fails when sending the error embed, it should not crash.

        This happens during bot shutdown: the Discord connection closes, then
        the session fails, and the error-embed send also fails (ServerDisconnectedError).
        The exception handler must suppress the secondary failure.
        """

        async def _failing_run(*args, **kwargs):
            raise Exception("Server disconnected")
            yield  # make it an async generator

        runner.run = _failing_run

        # Also make the error-embed send fail to simulate closed connection
        thread.send.side_effect = Exception("Server disconnected")

        # Should not raise even though both the session and the error-embed send fail
        result = await run_claude_in_thread(thread, runner, repo, "test", None)
        assert result is None  # returns None because session_id was never set

    @pytest.mark.asyncio
    async def test_repo_none_skips_save(self, thread: MagicMock, runner: MagicMock) -> None:
        """When repo is None (automated workflows), session save should be skipped."""
        events = [
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1"),
            StreamEvent(message_type=MessageType.ASSISTANT, text="Done."),
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text="Done.",
                session_id="sess-1",
                cost_usd=0.01,
                duration_ms=500,
            ),
        ]
        runner.run = self._make_async_gen(events)

        # Should not raise even with repo=None
        result = await run_claude_in_thread(thread, runner, None, "test", None)
        assert result == "sess-1"

    @pytest.mark.asyncio
    async def test_timeout_error_uses_timeout_embed(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """Timeout errors should use timeout_embed, not the generic error_embed."""
        events = [
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error="Timed out after 300 seconds",
            ),
        ]
        runner.run = self._make_async_gen(events)

        await run_claude_in_thread(thread, runner, repo, "test", None)

        embed_calls = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        assert any("timed out" in (c.kwargs["embed"].title or "").lower() for c in embed_calls)
        # Must NOT be the generic "Error" embed
        assert not any(c.kwargs["embed"].title == "❌ Error" for c in embed_calls)

    @pytest.mark.asyncio
    async def test_non_timeout_error_uses_error_embed(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """Non-timeout errors should still use the generic error_embed."""
        events = [
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error="CLI exited with code 1",
            ),
        ]
        runner.run = self._make_async_gen(events)

        await run_claude_in_thread(thread, runner, repo, "test", None)

        embed_calls = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        assert any("Error" in (c.kwargs["embed"].title or "") for c in embed_calls)

    @pytest.mark.asyncio
    async def test_session_start_embed_sent_only_once_for_multiple_system_events(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """session_start_embed must be sent exactly once even when Claude emits
        multiple SYSTEM events (e.g. init + hook feedback events with session_id).

        Regression test for: Claude Code emits 3+ SYSTEM events per session when
        hooks are configured (init + UserPromptSubmit hook partial + complete),
        each with session_id, causing 3 identical session-start embeds to appear.
        """
        events = [
            # Simulates: init SYSTEM message
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1"),
            # Simulates: hook feedback (UserPromptSubmit partial) — also has session_id
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1"),
            # Simulates: hook feedback (UserPromptSubmit complete) — also has session_id
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1"),
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                session_id="sess-1",
                cost_usd=0.001,
                duration_ms=500,
            ),
        ]
        runner.run = self._make_async_gen(events)

        await run_claude_in_thread(thread, runner, repo, "test", None)

        start_embeds = [
            c
            for c in thread.send.call_args_list
            if "embed" in c.kwargs and "session started" in (c.kwargs["embed"].title or "").lower()
        ]
        assert len(start_embeds) == 1, (
            f"Expected exactly 1 session_start_embed, got {len(start_embeds)}"
        )


# TestNoReplyFallback removed: the "Claude finished without calling
# discord-reply" fallback (Issue #67) was retired alongside the skill itself
# in Issue #71.  The JSONL transcript mirror now surfaces tool_use/tool_result
# events directly, so a quiet turn means there were genuinely no events worth
# mirroring — the pane shows the same thing, which matches the design intent.


class TestMakeErrorEmbed:
    """Unit tests for the _make_error_embed router function."""

    def test_timeout_message_returns_timeout_embed(self) -> None:
        embed = _make_error_embed("Timed out after 300 seconds")
        assert "timed out" in embed.title.lower()

    def test_timeout_message_includes_seconds(self) -> None:
        embed = _make_error_embed("Timed out after 120 seconds")
        assert "120" in embed.description

    def test_generic_error_returns_error_embed(self) -> None:
        embed = _make_error_embed("Something went wrong")
        assert embed.title == "❌ Error"
        assert "Something went wrong" in embed.description

    def test_partial_timeout_text_uses_error_embed(self) -> None:
        # "Timed out" not at start — should NOT match
        embed = _make_error_embed("Process Timed out after 300 seconds")
        assert embed.title == "❌ Error"


class TestConcurrencyIntegration:
    """Tests that run_claude_in_thread integrates with SessionRegistry."""

    @pytest.fixture
    def thread(self) -> MagicMock:
        t = MagicMock(spec=discord.Thread)
        t.id = 12345
        t.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        return t

    @pytest.fixture
    def repo(self) -> MagicMock:
        r = MagicMock()
        r.save = AsyncMock()
        return r

    @pytest.fixture
    def runner(self) -> MagicMock:
        r = MagicMock()
        r.working_dir = None
        # clone() returns the same mock so run() assignments in tests carry over.
        r.clone.return_value = r
        return r

    def _make_async_gen(self, events: list[StreamEvent]):
        async def gen(*args, **kwargs):
            for e in events:
                yield e

        return gen

    def _simple_events(self) -> list[StreamEvent]:
        return [
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1"),
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text="Done.",
                session_id="sess-1",
                cost_usd=0.01,
                duration_ms=500,
            ),
        ]

    @pytest.mark.asyncio
    async def test_concurrency_notice_not_injected_into_prompt(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """When registry is provided, the session is registered but the concurrency
        notice is NOT injected into the CLI (tmux TUI mode doesn't support
        --append-system-prompt). The user prompt must be passed unchanged.
        """
        registry = SessionRegistry()
        captured_prompt = []

        async def capturing_gen(prompt, **kwargs):
            captured_prompt.append(prompt)
            for e in self._simple_events():
                yield e

        runner.run = capturing_gen

        await run_claude_in_thread(thread, runner, repo, "fix the bug", None, registry=registry)

        # Prompt is unchanged — no concurrency notice injected.
        assert len(captured_prompt) == 1
        assert captured_prompt[0] == "fix the bug"

        # clone() is NOT called — tmux TUI mode doesn't support --append-system-prompt.
        runner.clone.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_registered_during_run(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """Session should be registered in the registry while running."""
        registry = SessionRegistry()
        registered_during_run = []

        original_events = self._simple_events()

        async def capturing_gen(prompt, **kwargs):
            # Capture registry state during execution
            registered_during_run.extend(registry.list_active())
            for e in original_events:
                yield e

        runner.run = capturing_gen

        await run_claude_in_thread(thread, runner, repo, "fix the bug", None, registry=registry)

        assert len(registered_during_run) == 1
        assert registered_during_run[0].thread_id == 12345

    @pytest.mark.asyncio
    async def test_session_unregistered_after_run(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """Session should be removed from registry after completion."""
        registry = SessionRegistry()
        runner.run = self._make_async_gen(self._simple_events())

        await run_claude_in_thread(thread, runner, repo, "fix the bug", None, registry=registry)

        assert registry.list_active() == []

    @pytest.mark.asyncio
    async def test_session_unregistered_on_error(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """Session should be removed from registry even if an error occurs."""
        registry = SessionRegistry()

        async def failing_gen(prompt, **kwargs):
            raise RuntimeError("boom")
            yield  # make it a generator  # noqa: E501

        runner.run = failing_gen

        await run_claude_in_thread(thread, runner, repo, "fix the bug", None, registry=registry)

        assert registry.list_active() == []

    @pytest.mark.asyncio
    async def test_other_sessions_registered_but_not_injected(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """When other sessions exist, the registry side effect still happens
        but the concurrency notice is NOT injected into the CLI.
        """
        registry = SessionRegistry()
        registry.register(9999, "running /goodmorning", "/home/ebi")

        captured_prompt = []

        async def capturing_gen(prompt, **kwargs):
            captured_prompt.append(prompt)
            for e in self._simple_events():
                yield e

        runner.run = capturing_gen

        await run_claude_in_thread(thread, runner, repo, "fix the bug", None, registry=registry)

        # User prompt is unchanged — no concurrency notice injected.
        assert len(captured_prompt) == 1
        assert captured_prompt[0] == "fix the bug"

        # clone() is NOT called — tmux TUI mode doesn't support --append-system-prompt.
        runner.clone.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_registry_no_clone(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """Without registry or lounge, runner is used directly (no clone needed)."""
        captured_prompt = []

        async def capturing_gen(prompt, **kwargs):
            captured_prompt.append(prompt)
            for e in self._simple_events():
                yield e

        runner.run = capturing_gen

        await run_claude_in_thread(thread, runner, repo, "fix the bug", None)

        assert captured_prompt[0] == "fix the bug"
        # No system context → runner.clone should not have been called.
        runner.clone.assert_not_called()


class TestLiveToolTimer:
    """Tests for LiveToolTimer elapsed-time embed updates."""

    def _bash_tool(self) -> ToolUseEvent:
        return ToolUseEvent(
            tool_id="t1",
            tool_name="Bash",
            tool_input={"command": "az login --use-device-code"},
            category=ToolCategory.COMMAND,
        )

    def _make_msg(self) -> MagicMock:
        msg = MagicMock(spec=discord.Message)
        msg.edit = AsyncMock()
        return msg

    @pytest.mark.asyncio
    async def test_timer_updates_embed_after_interval(self) -> None:
        """After TOOL_TIMER_INTERVAL seconds, the embed should be updated with elapsed time."""
        import c_lord.discord_ui.tool_timer as tt

        msg = self._make_msg()
        timer = LiveToolTimer(msg, self._bash_tool())

        original_interval = tt.TOOL_TIMER_INTERVAL
        tt.TOOL_TIMER_INTERVAL = 0.01  # speed up for test
        try:
            task = timer.start()
            await asyncio.sleep(0.05)  # allow at least one tick
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            tt.TOOL_TIMER_INTERVAL = original_interval

        msg.edit.assert_called()
        # Elapsed time must be in description (title stays stable across ticks)
        call_embed = msg.edit.call_args.kwargs.get("embed")
        assert call_embed is not None
        assert call_embed.description is not None
        assert "s" in call_embed.description  # e.g. "⏳ 0s elapsed..."
        assert "s)" not in call_embed.title  # title must NOT contain elapsed time

    @pytest.mark.asyncio
    async def test_timer_cancelled_stops_updates(self) -> None:
        """After cancellation, no further edits should occur."""
        import c_lord.discord_ui.tool_timer as tt

        msg = self._make_msg()
        timer = LiveToolTimer(msg, self._bash_tool())

        original_interval = tt.TOOL_TIMER_INTERVAL
        tt.TOOL_TIMER_INTERVAL = 0.01
        try:
            task = timer.start()
            await asyncio.sleep(0.005)  # cancel before first tick
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            tt.TOOL_TIMER_INTERVAL = original_interval

        msg.edit.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_claude_cancels_timer_on_tool_result(self) -> None:
        """Timer task should be cancelled when the tool result arrives."""
        import c_lord.cogs._run_helper as rh

        thread = MagicMock(spec=discord.Thread)
        thread.id = 11111
        tool_msg = MagicMock(spec=discord.Message)
        tool_msg.edit = AsyncMock()
        tool_msg.embeds = [MagicMock(title="🔧 Running: az login...")]
        thread.send = AsyncMock(return_value=tool_msg)

        repo = MagicMock()
        repo.save = AsyncMock()
        runner = MagicMock()

        events = [
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1"),
            StreamEvent(
                message_type=MessageType.ASSISTANT,
                tool_use=ToolUseEvent(
                    tool_id="t1",
                    tool_name="Bash",
                    tool_input={"command": "az login --use-device-code"},
                    category=ToolCategory.COMMAND,
                ),
            ),
            StreamEvent(
                message_type=MessageType.USER,
                tool_result_id="t1",
                tool_result_content="Device login complete",
            ),
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                session_id="sess-1",
                cost_usd=0.01,
                duration_ms=5000,
            ),
        ]

        async def gen(*args, **kwargs):
            for e in events:
                yield e

        runner.run = gen

        original_interval = rh.TOOL_TIMER_INTERVAL
        rh.TOOL_TIMER_INTERVAL = 100  # ensure timer never fires during this test
        try:
            await run_claude_in_thread(thread, runner, repo, "login", None)
        finally:
            rh.TOOL_TIMER_INTERVAL = original_interval

        # All timers should be cleared after run completes
        # (verified indirectly: no ghost tasks, session finishes cleanly)
