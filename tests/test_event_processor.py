"""Unit tests for EventProcessor.

These tests exercise individual event handlers in isolation, unlike the
integration tests in test_run_helper.py which test the full pipeline.

The key benefit of extracting EventProcessor as a class is that each
event type can be tested independently without running the full
run_claude_with_config() pipeline.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.claude.types import (
    AskOption,
    AskQuestion,
    MessageType,
    StreamEvent,
)
from c_lord.cogs.event_processor import EventProcessor
from c_lord.cogs.run_config import RunConfig


def _make_config(thread: MagicMock, runner: MagicMock, **kwargs) -> RunConfig:
    """Build a minimal RunConfig for tests."""
    return RunConfig(thread=thread, runner=runner, prompt="test prompt", **kwargs)


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
    """Issue #53: assistant text is not posted from here — the transcript
    mirror delivers Claude's answer (#712). An ASSISTANT text event must NOT
    make the processor call ``thread.send``.
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
    async def test_user_stop_is_not_shown_as_an_error(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """#878: a turn the user stopped is not a failure — no ❌, no error embed, no ping.

        The stop reply (⏹️ Session stopped) already says what happened; the
        lamp goes to 🟡 so the thread reads as "your turn".
        """
        from c_lord.claude.tmux_runner import STOPPED_BY_USER_ERROR

        status = MagicMock()
        status.set_error = AsyncMock()
        status.set_done = AsyncMock()
        config = _make_config(thread, runner, status=status, failure_notify_id=4242)
        p = EventProcessor(config)

        await p.process(
            StreamEvent(
                message_type=MessageType.RESULT, is_complete=True, error=STOPPED_BY_USER_ERROR
            )
        )

        assert [c for c in thread.send.call_args_list if "embed" in c.kwargs] == []
        status.set_error.assert_not_called()
        status.set_done.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_error_embed_mentions_the_failure_notify_user(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """#681: an ❌ nobody is @-mentioned about is a silent death.

        Discord only pushes reliably when the *message content* carries
        ``<@id>``; an embed alone never pings. #677/#678 each got exactly this
        embed and sat unnoticed for two days.
        """
        config = _make_config(thread, runner, failure_notify_id=4242)
        p = EventProcessor(config)

        await p.process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error="Claude exited without producing a response",
            )
        )

        err = [c for c in thread.send.call_args_list if "embed" in c.kwargs][-1]
        assert err.kwargs.get("content") == "<@4242>"

    @pytest.mark.asyncio
    async def test_error_embed_stays_silent_without_a_notify_user(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """#681 AC3: no one to ping ⇒ the embed is posted with no content."""
        config = _make_config(thread, runner, failure_notify_id=None)
        p = EventProcessor(config)

        await p.process(
            StreamEvent(message_type=MessageType.RESULT, is_complete=True, error="boom")
        )

        err = [c for c in thread.send.call_args_list if "embed" in c.kwargs][-1]
        assert err.kwargs.get("content") is None

    @pytest.mark.asyncio
    async def test_a_clean_turn_never_pings(self, thread: MagicMock, runner: MagicMock) -> None:
        """#681 AC4: the failure ping must not leak into normal completions."""
        config = _make_config(thread, runner, failure_notify_id=4242)
        p = EventProcessor(config)

        await p.process(_make_result_event(session_id="s1"))

        assert not [c for c in thread.send.call_args_list if c.kwargs.get("content")]

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


class TestUsageLimitOutcome:
    """#631: a rate-limited RESULT reports the limit, not "send it again"."""

    @pytest.mark.asyncio
    async def test_limit_event_posts_limit_embed_and_sets_outcome(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        from c_lord.claude.tmux_runner import USAGE_LIMIT_ERROR_PREFIX
        from c_lord.claude.types import UsageLimit

        config = _make_config(thread, runner)
        p = EventProcessor(config)

        limit = UsageLimit("weekly limit", "Aug 29, 4pm (Asia/Tokyo)", "")
        await p.process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error=f"{USAGE_LIMIT_ERROR_PREFIX} Claude hit your weekly limit.",
                usage_limit=limit,
            )
        )

        embed_sends = [c for c in thread.send.call_args_list if "embed" in c.kwargs]
        assert len(embed_sends) == 1
        embed = embed_sends[0].kwargs["embed"]
        assert "Aug 29, 4pm (Asia/Tokyo)" in embed.description
        assert "もう一度送る" not in embed.description

        # The turn produced nothing, and the caller must know *why*.
        assert config.outcome.no_response is True
        assert config.outcome.usage_limit == limit

    @pytest.mark.asyncio
    async def test_limit_embed_tells_the_mirror_not_to_repeat_it(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """#631 AC8: the same fact must not arrive again in English a second later.

        The mirror meets Claude's own banner in the transcript and folds it into
        a line of its own; this registration is how it learns that this thread
        has already been told, in the richer wording, by c-lord.
        """
        from c_lord.claude.tmux_runner import USAGE_LIMIT_ERROR_PREFIX
        from c_lord.claude.types import UsageLimit
        from c_lord.usage_limit import usage_limit_notices

        usage_limit_notices.clear_all()
        config = _make_config(thread, runner)
        assert usage_limit_notices.announced(thread.id) is False

        await EventProcessor(config).process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error=f"{USAGE_LIMIT_ERROR_PREFIX} Claude hit your weekly limit.",
                usage_limit=UsageLimit("weekly limit", "Aug 29, 4pm (Asia/Tokyo)", ""),
            )
        )

        assert usage_limit_notices.announced(thread.id) is True
        usage_limit_notices.clear_all()

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_leaves_the_mirror_free_to_speak(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """Only a limit notice silences the mirror — not every error embed."""
        from c_lord.usage_limit import usage_limit_notices

        usage_limit_notices.clear_all()
        config = _make_config(thread, runner)

        await EventProcessor(config).process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error="Claude exited without producing a response",
            )
        )

        assert usage_limit_notices.announced(thread.id) is False


def _deletable_message() -> MagicMock:
    m = MagicMock()
    m.delete = AsyncMock()
    m.edit = AsyncMock()
    return m


class TestSessionStartEmbedIsTakenAway:
    """The session-start embed is bookkeeping: gone once the turn ends, and
    never turned into a ``progress.txt`` of its own (#542).
    """

    @pytest.mark.asyncio
    async def test_session_start_only_sends_no_file(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        sent: list[MagicMock] = []

        async def fake_send(*args, **kwargs):
            sent.append(_deletable_message())
            return sent[-1]

        thread.send = AsyncMock(side_effect=fake_send)

        p = EventProcessor(_make_config(thread, runner))
        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))
        await p.process(_make_result_event(session_id="s1"))

        file_sends = [c for c in thread.send.await_args_list if "file" in c.kwargs]
        assert file_sends == [], f"an empty progress.txt was posted: {file_sends!r}"
        sent[0].delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embed_is_deleted_only_once(self, thread: MagicMock, runner: MagicMock) -> None:
        start = _deletable_message()
        thread.send = AsyncMock(return_value=start)

        p = EventProcessor(_make_config(thread, runner))
        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))
        await p.process(_make_result_event(session_id="s1"))
        await p.process(_make_result_event(session_id="s1"))

        start.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failed_delete_does_not_break_the_turn(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        start = _deletable_message()
        start.delete = AsyncMock(side_effect=RuntimeError("Session is closed"))
        thread.send = AsyncMock(return_value=start)

        p = EventProcessor(_make_config(thread, runner))
        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))
        await p.process(_make_result_event(session_id="s1"))  # must not raise

    @pytest.mark.asyncio
    async def test_resumed_turn_deletes_and_attaches_nothing(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        sent: list[MagicMock] = []

        async def fake_send(*args, **kwargs):
            sent.append(_deletable_message())
            return sent[-1]

        thread.send = AsyncMock(side_effect=fake_send)

        p = EventProcessor(_make_config(thread, runner, session_id="resumed"))
        await p.process(_make_result_event(session_id="resumed"))

        for m in sent:
            m.delete.assert_not_called()
            assert [c for c in m.edit.await_args_list if "attachments" in c.kwargs] == []

    @pytest.mark.asyncio
    async def test_error_embed_is_left_in_place(self, thread: MagicMock, runner: MagicMock) -> None:
        sent: list[MagicMock] = []

        async def fake_send(*args, **kwargs):
            sent.append(_deletable_message())
            return sent[-1]

        thread.send = AsyncMock(side_effect=fake_send)

        p = EventProcessor(_make_config(thread, runner))
        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))
        await p.process(_make_result_event(session_id="s1", error="boom"))

        start, error = sent[0], sent[1]
        start.delete.assert_awaited_once()
        error.delete.assert_not_called()
        assert [c for c in error.edit.await_args_list if "attachments" in c.kwargs] == []
