"""Unit tests for EventProcessor.

These tests exercise individual event handlers in isolation, unlike the
integration tests in test_run_helper.py which test the full pipeline.

The key benefit of extracting EventProcessor as a class is that each
event type can be tested independently without running the full
run_claude_with_config() pipeline.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.claude.types import (
    AskOption,
    AskQuestion,
    ElicitationRequest,
    MessageType,
    PermissionRequest,
    StreamEvent,
    ToolCategory,
    ToolUseEvent,
)
from c_lord.cogs.event_processor import EventProcessor
from c_lord.cogs.run_config import RunConfig


def _make_config(thread: MagicMock, runner: MagicMock, **kwargs) -> RunConfig:
    """Build a minimal RunConfig for tests."""
    return RunConfig(thread=thread, runner=runner, prompt="test prompt", **kwargs)


def _make_tool_event(tool_id: str = "t1") -> StreamEvent:
    return StreamEvent(
        message_type=MessageType.ASSISTANT,
        tool_use=ToolUseEvent(
            tool_id=tool_id,
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            category=ToolCategory.COMMAND,
        ),
    )


def _make_result_event(**kwargs) -> StreamEvent:
    return StreamEvent(
        message_type=MessageType.RESULT,
        is_complete=True,
        cost_usd=0.01,
        duration_ms=500,
        **kwargs,
    )


class TestEventProcessorProperties:
    """Initial state and property behaviour."""

    def test_session_id_is_none_initially(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)
        assert p.session_id is None

    def test_session_id_inherits_from_config(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner, session_id="existing")
        p = EventProcessor(config)
        assert p.session_id == "existing"

    def test_pending_ask_none_initially(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)
        assert p.pending_ask is None

    def test_should_drain_false_initially(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)
        assert p.should_drain is False

    def test_assistant_text_sent_false_initially(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)
        assert p.assistant_text_sent is False


class TestOnSystem:
    """SYSTEM event handling."""

    @pytest.mark.asyncio
    async def test_captures_session_id(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-abc"))

        assert p.session_id == "sess-abc"

    @pytest.mark.asyncio
    async def test_saves_to_repo(self, thread: MagicMock, runner: MagicMock) -> None:
        repo = MagicMock()
        repo.save = AsyncMock()
        config = _make_config(thread, runner, repo=repo)
        p = EventProcessor(config)

        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))

        repo.save.assert_called_once_with(thread.id, "s1", working_dir=None)

    @pytest.mark.asyncio
    async def test_saves_working_dir_to_repo(self, thread: MagicMock, runner: MagicMock) -> None:
        repo = MagicMock()
        repo.save = AsyncMock()
        config = _make_config(thread, runner, repo=repo, working_dir="/tmp/session/123")
        p = EventProcessor(config)

        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))

        repo.save.assert_called_once_with(thread.id, "s1", working_dir="/tmp/session/123")

    @pytest.mark.asyncio
    async def test_sends_start_embed_for_new_session(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """Start embed is posted when no session_id was pre-set (new session)."""
        config = _make_config(thread, runner)  # no session_id
        p = EventProcessor(config)

        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="new-sess"))

        thread.send.assert_called_once()
        call_kwargs = thread.send.call_args.kwargs
        assert "embed" in call_kwargs

    @pytest.mark.asyncio
    async def test_no_start_embed_for_resumed_session(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """Start embed is NOT posted when a session_id is pre-set (resume)."""
        config = _make_config(thread, runner, session_id="pre-existing")
        p = EventProcessor(config)

        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="pre-existing"))

        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_embed_sent_only_once(self, thread: MagicMock, runner: MagicMock) -> None:
        """Multiple SYSTEM events (can happen with Claude Code) only produce one start embed."""
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))
        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))

        embed_sends = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        assert len(embed_sends) == 1


class TestOnAssistantText:
    """Issue #53: assistant text is no longer posted to Discord — Claude
    pushes its final answer via the discord-reply skill. The processor
    receives ASSISTANT text events but must NOT call ``thread.send`` for
    them, and ``assistant_text_sent`` is hard-wired to False.
    """

    @pytest.mark.asyncio
    async def test_complete_text_does_not_send(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        await p.process(
            StreamEvent(message_type=MessageType.ASSISTANT, text="Hello!", is_partial=False)
        )

        text_sends = [
            c for c in thread.send.call_args_list if c.args and isinstance(c.args[0], str)
        ]
        assert text_sends == []

    @pytest.mark.asyncio
    async def test_partial_text_does_not_send(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        await p.process(
            StreamEvent(message_type=MessageType.ASSISTANT, text="Hel", is_partial=True)
        )

        text_sends = [
            c for c in thread.send.call_args_list if c.args and isinstance(c.args[0], str)
        ]
        assert text_sends == []

    @pytest.mark.asyncio
    async def test_assistant_text_sent_stays_false(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)
        await p.process(
            StreamEvent(message_type=MessageType.ASSISTANT, text="Hello", is_partial=False)
        )
        assert p.assistant_text_sent is False


class TestOnAssistantThinking:
    """ASSISTANT thinking event handling."""

    @pytest.mark.asyncio
    async def test_complete_thinking_sends_embed(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        event = StreamEvent(
            message_type=MessageType.ASSISTANT,
            thinking="I am thinking...",
            is_partial=False,
        )
        await p.process(event)

        embed_sends = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        assert len(embed_sends) == 1

    @pytest.mark.asyncio
    async def test_partial_thinking_does_not_send_embed(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        event = StreamEvent(message_type=MessageType.ASSISTANT, thinking="I am", is_partial=True)
        await p.process(event)

        thread.send.assert_not_called()


class TestOnToolUse:
    """ASSISTANT tool_use event handling."""

    @pytest.mark.asyncio
    async def test_tool_use_sends_embed(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        await p.process(_make_tool_event("t1"))

        embed_sends = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        assert len(embed_sends) == 1

    @pytest.mark.asyncio
    async def test_tool_use_tracked_in_state(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        await p.process(_make_tool_event("tool-xyz"))

        assert "tool-xyz" in p._state.active_tools

    @pytest.mark.asyncio
    async def test_tool_use_starts_timer(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        await p.process(_make_tool_event("t-timer"))

        assert "t-timer" in p._state.active_timers
        # Clean up the timer task
        task = p._state.active_timers["t-timer"]
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task


class TestOnToolResult:
    """USER (tool result) event handling."""

    @pytest.mark.asyncio
    async def test_timer_cancelled_on_result(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        # Plant a fake timer task
        fake_task = MagicMock(spec=asyncio.Task)
        fake_task.done.return_value = False
        p._state.active_timers["t1"] = fake_task

        result_event = StreamEvent(message_type=MessageType.USER, tool_result_id="t1")
        await p.process(result_event)

        fake_task.cancel.assert_called_once()
        assert "t1" not in p._state.active_timers

    @pytest.mark.asyncio
    async def test_tool_embed_updated_with_result(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        # Plant a fake in-progress tool message
        fake_embed = MagicMock(spec=discord.Embed)
        fake_embed.title = "Running: echo hi"
        fake_msg = MagicMock(spec=discord.Message)
        fake_msg.embeds = [fake_embed]
        fake_msg.edit = AsyncMock()
        p._state.active_tools["t1"] = fake_msg

        result_event = StreamEvent(
            message_type=MessageType.USER,
            tool_result_id="t1",
            tool_result_content="output here",
        )
        await p.process(result_event)

        fake_msg.edit.assert_called_once()


class TestAskUserQuestion:
    """AskUserQuestion detection."""

    @pytest.mark.asyncio
    async def test_pending_ask_set_on_detect(self, thread: MagicMock, runner: MagicMock) -> None:
        runner.interrupt = AsyncMock()
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        ask = AskQuestion(
            question="Which option?",
            options=[AskOption(label="A"), AskOption(label="B")],
        )
        event = StreamEvent(
            message_type=MessageType.ASSISTANT,
            ask_questions=[ask],
        )
        await p.process(event)

        assert p.pending_ask == [ask]
        assert p.should_drain is True

    @pytest.mark.asyncio
    async def test_runner_interrupted_on_ask(self, thread: MagicMock, runner: MagicMock) -> None:
        runner.interrupt = AsyncMock()
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        event = StreamEvent(
            message_type=MessageType.ASSISTANT,
            ask_questions=[AskQuestion(question="?", options=[AskOption(label="A")])],
        )
        await p.process(event)

        runner.interrupt.assert_called_once()


class TestPaneAskOwnership:
    """#535 AC1: the poll-loop bridge must respect an existing menu owner."""

    @pytest.mark.asyncio
    async def test_pane_ask_skipped_when_another_bridge_owns_the_menu(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """A menu already bridged elsewhere must not be bridged a second time.

        ``_handle_pane_ask`` used to call ``bridge_pane_ask`` unconditionally
        while the transcript-mirror bridge had the same menu on screen — the two
        copies of the buttons yousan saw in #535.
        """
        from c_lord.discord_ui.ask_bus import ask_bus

        runner.answer_menu = AsyncMock()
        config = _make_config(thread, runner)
        p = EventProcessor(config)
        ask = AskQuestion(question="Which?", options=[AskOption(label="A")])

        owner_queue = ask_bus.register(thread.id)
        assert owner_queue is not None
        try:
            with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
                await p.process(StreamEvent(message_type=MessageType.SYSTEM, pane_ask=ask))
            bridge.assert_not_called()
            assert ask_bus.is_active(thread.id)
        finally:
            ask_bus.unregister(thread.id)

    @pytest.mark.asyncio
    async def test_pane_ask_bridged_when_nobody_owns_the_menu(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """The common case is unchanged: no owner → this turn bridges the menu."""
        runner.answer_menu = AsyncMock()
        config = _make_config(thread, runner)
        p = EventProcessor(config)
        ask = AskQuestion(question="Which?", options=[AskOption(label="A")])

        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await p.process(StreamEvent(message_type=MessageType.SYSTEM, pane_ask=ask))
        bridge.assert_called_once()


class TestOnComplete:
    """RESULT (is_complete) event handling."""

    @pytest.mark.asyncio
    async def test_complete_does_not_send_session_complete_embed(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """Successful completion no longer posts a session_complete embed."""
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        await p.process(_make_result_event(session_id="s1"))

        embed_sends = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        assert len(embed_sends) == 0

    @pytest.mark.asyncio
    async def test_error_sends_error_embed(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        event = StreamEvent(
            message_type=MessageType.RESULT,
            is_complete=True,
            error="Something went wrong",
        )
        await p.process(event)

        embed_sends = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        assert len(embed_sends) >= 1

    @pytest.mark.asyncio
    async def test_result_text_not_posted(self, thread: MagicMock, runner: MagicMock) -> None:
        """Issue #53: RESULT.text is dropped — never posted to Discord."""
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        result_event = _make_result_event(text="Answer.", session_id="s1")
        await p.process(result_event)

        text_sends = [
            c for c in thread.send.call_args_list if c.args and isinstance(c.args[0], str)
        ]
        answer_sends = [c for c in text_sends if "Answer." in c.args[0]]
        assert answer_sends == [], "RESULT text must not reach Discord post-#53"

    @pytest.mark.asyncio
    async def test_result_saves_working_dir_to_repo(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        repo = MagicMock()
        repo.save = AsyncMock()
        config = _make_config(thread, runner, repo=repo, working_dir="/sessions/456")
        p = EventProcessor(config)

        await p.process(_make_result_event(session_id="s2"))

        repo.save.assert_called_once_with(thread.id, "s2", working_dir="/sessions/456")


class TestConnectionErrorResilience:
    """Discord/aiohttp connection errors must not crash the session.

    ServerDisconnectedError (aiohttp.ClientError subclass) is raised when
    the Discord HTTP session closes — e.g. during bot shutdown. It is NOT a
    subclass of discord.HTTPException, so it was previously uncaught and
    would propagate up to kill the entire session.
    """

    @pytest.mark.asyncio
    async def test_tool_use_send_failure_does_not_raise(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """If thread.send fails (connection closed), _handle_tool_use returns silently."""
        thread.send.side_effect = Exception("Server disconnected")
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        # Should not raise — failure is logged and the handler returns early.
        await p.process(_make_tool_event("t1"))

        # Tool was not tracked because send failed
        assert "t1" not in p._state.active_tools
        assert "t1" not in p._state.active_timers

    @pytest.mark.asyncio
    async def test_tool_result_edit_failure_does_not_raise(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """If tool embed edit raises a connection error, _on_tool_result suppresses it."""
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        fake_embed = MagicMock(spec=discord.Embed)
        fake_embed.title = "Running: echo hi"
        fake_msg = MagicMock(spec=discord.Message)
        fake_msg.embeds = [fake_embed]
        fake_msg.edit = AsyncMock(side_effect=Exception("Server disconnected"))
        p._state.active_tools["t1"] = fake_msg

        result_event = StreamEvent(
            message_type=MessageType.USER,
            tool_result_id="t1",
            tool_result_content="output",
        )
        # Should not raise — failure is suppressed.
        await p.process(result_event)


class TestFinalize:
    """finalize() cleanup."""

    @pytest.mark.asyncio
    async def test_finalize_cancels_active_timers(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        fake_task = MagicMock(spec=asyncio.Task)
        fake_task.done.return_value = False
        p._state.active_timers["t1"] = fake_task

        await p.finalize()

        fake_task.cancel.assert_called_once()
        assert len(p._state.active_timers) == 0

    @pytest.mark.asyncio
    async def test_finalize_skips_done_tasks(self, thread: MagicMock, runner: MagicMock) -> None:
        config = _make_config(thread, runner)
        p = EventProcessor(config)

        fake_task = MagicMock(spec=asyncio.Task)
        fake_task.done.return_value = True
        p._state.active_timers["t1"] = fake_task

        await p.finalize()

        fake_task.cancel.assert_not_called()


class TestCompactHandling:
    """Tests for context compaction event handling."""

    @pytest.mark.asyncio
    async def test_compact_sends_notification(self) -> None:
        thread = MagicMock()
        thread.send = AsyncMock(return_value=MagicMock(embeds=[]))
        runner = MagicMock()
        runner.interrupt = AsyncMock()
        status = MagicMock()
        status.set_compact = AsyncMock()
        status.set_thinking = AsyncMock()
        status._reset_stall_timer = MagicMock()
        config = _make_config(thread, runner, status=status)
        processor = EventProcessor(config)

        event = StreamEvent(
            message_type=MessageType.SYSTEM,
            is_compact=True,
            compact_trigger="auto",
            compact_pre_tokens=167745,
        )
        await processor.process(event)

        status.set_compact.assert_awaited_once()
        # Check that a message was sent to the thread
        calls = [str(c) for c in thread.send.call_args_list]
        assert any("compact" in c.lower() or "\U0001f5dc" in c for c in calls)

    @pytest.mark.asyncio
    async def test_progress_resets_stall_timer(self) -> None:
        thread = MagicMock()
        thread.send = AsyncMock(return_value=MagicMock(embeds=[]))
        runner = MagicMock()
        status = MagicMock()
        status._reset_stall_timer = MagicMock()
        config = _make_config(thread, runner, status=status)
        processor = EventProcessor(config)

        event = StreamEvent(message_type=MessageType.PROGRESS)
        await processor.process(event)

        status._reset_stall_timer.assert_called_once()


def _mention_contents(thread: MagicMock) -> list[str]:
    """Return the ``content`` of every thread.send call that carries an @mention.

    A Discord push notification is only guaranteed when the *message content*
    (not an embed) contains ``<@id>`` — so #480's fix is observable exactly as
    "at least one send whose content mentions the notify user".
    """
    out: list[str] = []
    for call in thread.send.call_args_list:
        content = call.kwargs.get("content")
        if content is None and call.args and isinstance(call.args[0], str):
            content = call.args[0]
        if isinstance(content, str) and "<@" in content:
            out.append(content)
    return out


class TestInteractivePromptNotification:
    """#480: question-mode prompts must @-mention the turn's poster.

    Permission / plan / elicitation / AskUserQuestion pauses block the turn
    awaiting the user's input, but historically posted a button embed with no
    message-content mention — so users whose thread notifications are
    "mentions only" got no push. These tests assert the notify user is pinged
    via message content (embeds do not ping).
    """

    @pytest.mark.asyncio
    async def test_permission_request_pings_notify_user(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner, notify_user_id=999)
        p = EventProcessor(config)

        await p.process(
            StreamEvent(
                message_type=MessageType.SYSTEM,
                permission_request=PermissionRequest(
                    request_id="r1", tool_name="Bash", tool_input={"command": "echo hi"}
                ),
            )
        )

        mentions = _mention_contents(thread)
        assert any("<@999>" in m for m in mentions), (
            f"permission prompt must ping notify user 999; sends={thread.send.call_args_list}"
        )

    @pytest.mark.asyncio
    async def test_plan_approval_pings_notify_user(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner, notify_user_id=999)
        p = EventProcessor(config)

        await p.process(
            StreamEvent(
                message_type=MessageType.ASSISTANT,
                is_plan_approval=True,
                is_partial=False,
                text="# Plan\n1. do thing",
            )
        )

        mentions = _mention_contents(thread)
        assert any("<@999>" in m for m in mentions), (
            f"plan approval must ping notify user 999; sends={thread.send.call_args_list}"
        )

    @pytest.mark.asyncio
    async def test_elicitation_pings_notify_user(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner, notify_user_id=999)
        p = EventProcessor(config)

        await p.process(
            StreamEvent(
                message_type=MessageType.SYSTEM,
                elicitation=ElicitationRequest(
                    request_id="e1",
                    server_name="srv",
                    mode="url-mode",
                    message="Authorize",
                    url="https://example.com/auth",
                ),
            )
        )

        mentions = _mention_contents(thread)
        assert any("<@999>" in m for m in mentions), (
            f"elicitation must ping notify user 999; sends={thread.send.call_args_list}"
        )

    @pytest.mark.asyncio
    async def test_no_mention_when_notify_user_unset(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """With no notify user (e.g. automated turn, owner unset), never ping."""
        config = _make_config(thread, runner)  # notify_user_id defaults to None
        p = EventProcessor(config)

        await p.process(
            StreamEvent(
                message_type=MessageType.SYSTEM,
                permission_request=PermissionRequest(
                    request_id="r1", tool_name="Bash", tool_input={"command": "echo hi"}
                ),
            )
        )

        assert _mention_contents(thread) == []
