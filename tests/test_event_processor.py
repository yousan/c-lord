# <<REDACTED 12 lines>>
from unittest.mock import AsyncMock, MagicMock, patch
# <<REDACTED 3 lines>>
from c_lord.claude.types import (
    # <<REDACTED 5 lines>>
from c_lord.cogs.event_processor import EventProcessor
from c_lord.cogs.run_config import RunConfig
# <<REDACTED 52 lines>>
        repo.save.assert_called_once_with(thread.id, "s1", working_dir=None)

    @pytest.mark.asyncio
    async def test_saves_working_dir_to_repo(self, thread: MagicMock, runner: MagicMock) -> None:
        # <<REDACTED 2 lines>>
        config = _make_config(thread, runner, repo=repo, working_dir="/tmp/session/123")
        # <<REDACTED 3 lines>>

        repo.save.assert_called_once_with(thread.id, "s1", working_dir="/tmp/session/123")
# <<REDACTED 41 lines>>
    """Issue #53: assistant text is not posted from here — the transcript
    mirror delivers Claude's answer (#712). An ASSISTANT text event must NOT
    make the processor call ``thread.send``.
    """
# <<REDACTED 2 lines>>
    async def test_complete_text_does_not_send(self, thread: MagicMock, runner: MagicMock) -> None:
        # <<REDACTED 3 lines>>
        await p.process(
            StreamEvent(message_type=MessageType.ASSISTANT, text="Hello!", is_partial=False)
        )
# <<REDACTED 4 lines>>
        assert text_sends == []
# <<REDACTED 2 lines>>
    async def test_partial_text_does_not_send(self, thread: MagicMock, runner: MagicMock) -> None:
        # <<REDACTED 3 lines>>
        await p.process(
            StreamEvent(message_type=MessageType.ASSISTANT, text="Hel", is_partial=True)
        )
# <<REDACTED 4 lines>>
        assert text_sends == []
# <<REDACTED 2 lines>>
class TestPaneAskOwnership:
    """#535 AC1: the poll-loop bridge must respect an existing menu owner."""

    @pytest.mark.asyncio
    async def test_pane_ask_skipped_when_another_bridge_owns_the_menu(
        # <<REDACTED 2 lines>>
        """A menu already bridged elsewhere must not be bridged a second time.

        ``_handle_pane_ask`` used to call ``bridge_pane_ask`` unconditionally
        while the transcript-mirror bridge had the same menu on screen — the two
        copies of the buttons yousan saw in #535.
        """
        from c_lord.discord_ui.ask_bus import ask_bus

        runner.answer_menu = AsyncMock()
        # <<REDACTED 2 lines>>
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
        # <<REDACTED 2 lines>>
        """The common case is unchanged: no owner → this turn bridges the menu."""
        runner.answer_menu = AsyncMock()
        # <<REDACTED 2 lines>>
        ask = AskQuestion(question="Which?", options=[AskOption(label="A")])

        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await p.process(StreamEvent(message_type=MessageType.SYSTEM, pane_ask=ask))
        bridge.assert_called_once()


# <<REDACTED 4 lines>>
    async def test_complete_does_not_send_session_complete_embed(
        # <<REDACTED 2 lines>>
        """Successful completion no longer posts a session_complete embed."""
        # <<REDACTED 6 lines>>
        assert len(embed_sends) == 0
# <<REDACTED 16 lines>>
    @pytest.mark.asyncio
    async def test_user_stop_is_not_shown_as_an_error(
        # <<REDACTED 2 lines>>
        """#878: a turn the user stopped is not a failure — no ❌, no error embed, no ping.

        The stop reply (⏹️ Session stopped) already says what happened; the
        lamp goes to 🟡 so the thread reads as "your turn".
        """
        from c_lord.claude.tmux_runner import STOPPED_BY_USER_ERROR

        status = MagicMock()
        status.set_error = AsyncMock()
        status.set_done = AsyncMock()
        config = _make_config(thread, runner, status=status, failure_notify_id=4242)
        # <<REDACTED 2 lines>>
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
        # <<REDACTED 2 lines>>
        """#681: an ❌ nobody is @-mentioned about is a silent death.

        Discord only pushes reliably when the *message content* carries
        ``<@id>``; an embed alone never pings. #677/#678 each got exactly this
        embed and sat unnoticed for two days.
        """
        config = _make_config(thread, runner, failure_notify_id=4242)
        # <<REDACTED 2 lines>>
        await p.process(
            StreamEvent(
                # <<REDACTED 2 lines>>
                error="Claude exited without producing a response",
            )
        )

        err = [c for c in thread.send.call_args_list if "embed" in c.kwargs][-1]
        assert err.kwargs.get("content") == "<@4242>"

    @pytest.mark.asyncio
    async def test_error_embed_stays_silent_without_a_notify_user(
        # <<REDACTED 2 lines>>
        """#681 AC3: no one to ping ⇒ the embed is posted with no content."""
        config = _make_config(thread, runner, failure_notify_id=None)
        # <<REDACTED 2 lines>>
        await p.process(
            StreamEvent(message_type=MessageType.RESULT, is_complete=True, error="boom")
        )

        err = [c for c in thread.send.call_args_list if "embed" in c.kwargs][-1]
        assert err.kwargs.get("content") is None

    @pytest.mark.asyncio
    async def test_a_clean_turn_never_pings(self, thread: MagicMock, runner: MagicMock) -> None:
        """#681 AC4: the failure ping must not leak into normal completions."""
        config = _make_config(thread, runner, failure_notify_id=4242)
        # <<REDACTED 4 lines>>
        assert not [c for c in thread.send.call_args_list if c.kwargs.get("content")]

    # <<REDACTED 1 lines>>
    async def test_result_text_not_posted(self, thread: MagicMock, runner: MagicMock) -> None:
        """Issue #53: RESULT.text is dropped — never posted to Discord."""
        # <<REDACTED 10 lines>>
        assert answer_sends == [], "RESULT text must not reach Discord post-#53"
# <<REDACTED 1 lines>>
    @pytest.mark.asyncio
    async def test_result_saves_working_dir_to_repo(
        # <<REDACTED 4 lines>>
        config = _make_config(thread, runner, repo=repo, working_dir="/sessions/456")
        # <<REDACTED 1 lines>>

        await p.process(_make_result_event(session_id="s2"))

        repo.save.assert_called_once_with(thread.id, "s2", working_dir="/sessions/456")

# <<REDACTED 1 lines>>
class TestUsageLimitOutcome:
    """#631: a rate-limited RESULT reports the limit, not "send it again"."""

    @pytest.mark.asyncio
    async def test_limit_event_posts_limit_embed_and_sets_outcome(
        # <<REDACTED 2 lines>>
        from c_lord.claude.tmux_runner import USAGE_LIMIT_ERROR_PREFIX
        from c_lord.claude.types import UsageLimit

        # <<REDACTED 3 lines>>
        limit = UsageLimit("weekly limit", "Aug 29, 4pm (Asia/Tokyo)", "")
        await p.process(
            StreamEvent(
                # <<REDACTED 2 lines>>
                error=f"{USAGE_LIMIT_ERROR_PREFIX} Claude hit your weekly limit.",
                usage_limit=limit,
            )
        )
# <<REDACTED 3 lines>>
        embed = embed_sends[0].kwargs["embed"]
        assert "Aug 29, 4pm (Asia/Tokyo)" in embed.description
        assert "もう一度送る" not in embed.description

        # The turn produced nothing, and the caller must know *why*.
        assert config.outcome.no_response is True
        assert config.outcome.usage_limit == limit

    @pytest.mark.asyncio
    async def test_limit_embed_tells_the_mirror_not_to_repeat_it(
        # <<REDACTED 2 lines>>
        """#631 AC8: the same fact must not arrive again in English a second later.

        The mirror meets Claude's own banner in the transcript and folds it into
        a line of its own; this registration is how it learns that this thread
        has already been told, in the richer wording, by c-lord.
        """
        from c_lord.claude.tmux_runner import USAGE_LIMIT_ERROR_PREFIX
        from c_lord.claude.types import UsageLimit
        from c_lord.usage_limit import usage_limit_notices

        usage_limit_notices.clear_all()
        # <<REDACTED 1 lines>>
        assert usage_limit_notices.announced(thread.id) is False

        await EventProcessor(config).process(
            StreamEvent(
                # <<REDACTED 2 lines>>
                error=f"{USAGE_LIMIT_ERROR_PREFIX} Claude hit your weekly limit.",
                usage_limit=UsageLimit("weekly limit", "Aug 29, 4pm (Asia/Tokyo)", ""),
            )
        )

        assert usage_limit_notices.announced(thread.id) is True
        usage_limit_notices.clear_all()

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_leaves_the_mirror_free_to_speak(
        # <<REDACTED 2 lines>>
        """Only a limit notice silences the mirror — not every error embed."""
        from c_lord.usage_limit import usage_limit_notices

        usage_limit_notices.clear_all()
        # <<REDACTED 1 lines>>

        await EventProcessor(config).process(
            StreamEvent(
                # <<REDACTED 2 lines>>
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
        # <<REDACTED 2 lines>>
        sent: list[MagicMock] = []

        async def fake_send(*args, **kwargs):
            sent.append(_deletable_message())
            return sent[-1]

        thread.send = AsyncMock(side_effect=fake_send)

        p = EventProcessor(_make_config(thread, runner))
        # <<REDACTED 3 lines>>
        file_sends = [c for c in thread.send.await_args_list if "file" in c.kwargs]
        assert file_sends == [], f"an empty progress.txt was posted: {file_sends!r}"
        sent[0].delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embed_is_deleted_only_once(self, thread: MagicMock, runner: MagicMock) -> None:
        start = _deletable_message()
        thread.send = AsyncMock(return_value=start)

        p = EventProcessor(_make_config(thread, runner))
        # <<REDACTED 4 lines>>
        start.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failed_delete_does_not_break_the_turn(
        # <<REDACTED 2 lines>>
        start = _deletable_message()
        start.delete = AsyncMock(side_effect=RuntimeError("Session is closed"))
        thread.send = AsyncMock(return_value=start)

        p = EventProcessor(_make_config(thread, runner))
        # <<REDACTED 1 lines>>
        await p.process(_make_result_event(session_id="s1"))  # must not raise

    @pytest.mark.asyncio
    async def test_resumed_turn_deletes_and_attaches_nothing(
        # <<REDACTED 2 lines>>
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
        # <<REDACTED 1 lines>>
        await p.process(_make_result_event(session_id="s1", error="boom"))

        start, error = sent[0], sent[1]
        start.delete.assert_awaited_once()
        error.delete.assert_not_called()
        assert [c for c in error.edit.await_args_list if "attachments" in c.kwargs] == []
