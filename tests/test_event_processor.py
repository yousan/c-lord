"""EventProcessor: what c-lord itself posts for one turn's runner events.

The runner reports two things — SYSTEM (a session id, a menu, an unknown
prompt) and RESULT (the turn ended). From those the processor keeps the session
id, saves it, announces a new session once, sets the lamp, and on failure posts
the embed, pings whoever is waiting and records why on ``RunConfig.outcome``.
Claude's own words never go through here (the transcript mirror posts them),
and several tests below exist only to keep it that way.
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


def _make_config(thread: MagicMock, runner: MagicMock, **overrides) -> RunConfig:
    """A RunConfig for *thread* / *runner*; everything else defaults unless overridden."""
    return RunConfig(thread=thread, runner=runner, prompt="hello", **overrides)


def _make_system_event(session_id: str | None = "s1") -> StreamEvent:
    return StreamEvent(message_type=MessageType.SYSTEM, session_id=session_id)


def _make_result_event(
    session_id: str | None = None, error: str | None = None, text: str | None = None
) -> StreamEvent:
    return StreamEvent(
        message_type=MessageType.RESULT,
        is_complete=True,
        session_id=session_id,
        error=error,
        text=text,
    )


def _embed_sends(thread: MagicMock) -> list:
    return [c for c in thread.send.call_args_list if "embed" in c.kwargs]


def _status() -> MagicMock:
    status = MagicMock()
    status.set_done = AsyncMock()
    status.set_error = AsyncMock()
    return status


@pytest.fixture(autouse=True)
def _quiet_login_alert():
    """Keep the owner-DM side channel (#1309) out of these tests; expose it to those that ask."""
    alert = MagicMock()
    alert.report = AsyncMock()
    alert.recovered = AsyncMock()
    with patch("c_lord.cogs.event_processor.login_alert", new=alert):
        yield alert


class TestEventProcessorProperties:
    """``session_id`` is what the turn hands back to its caller."""

    def test_a_new_session_has_no_id_yet(self, thread: MagicMock, runner: MagicMock) -> None:
        assert EventProcessor(_make_config(thread, runner)).session_id is None

    def test_a_resumed_session_starts_from_the_id_being_resumed(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        p = EventProcessor(_make_config(thread, runner, session_id="resumed-1"))
        assert p.session_id == "resumed-1"

    @pytest.mark.asyncio
    async def test_finalize_adds_nothing_to_the_turn(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """Callers end every turn with ``finalize()`` in a ``finally`` — before any
        event, after the result, possibly twice. Wherever it lands it posts
        nothing, leaves the lamp where the turn put it, and the id it hands
        back to the caller is still the turn's."""
        status = _status()
        p = EventProcessor(_make_config(thread, runner, status=status))

        await p.finalize()  # a turn that never saw an event
        thread.send.assert_not_called()
        assert p.session_id is None

        await p.process(_make_system_event("s1"))
        await p.process(_make_result_event(session_id="s1"))
        posted = thread.send.call_count
        await p.finalize()
        await p.finalize()

        assert p.session_id == "s1"
        assert thread.send.call_count == posted
        status.set_done.assert_awaited_once()
        status.set_error.assert_not_called()


class TestOnSystem:
    """SYSTEM events: the session id is learned, saved and announced."""

    @pytest.mark.asyncio
    async def test_the_reported_session_id_becomes_the_turns(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        p = EventProcessor(_make_config(thread, runner))
        await p.process(_make_system_event("s1"))
        assert p.session_id == "s1"

    @pytest.mark.asyncio
    async def test_a_later_id_replaces_an_earlier_one(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        p = EventProcessor(_make_config(thread, runner, session_id="old"))
        await p.process(_make_system_event("new"))
        assert p.session_id == "new"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", [None, ""], ids=["none", "empty"])
    async def test_an_event_without_an_id_changes_nothing(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock, missing: str | None
    ) -> None:
        p = EventProcessor(_make_config(thread, runner, repo=repo, session_id="kept"))
        await p.process(_make_system_event(missing))
        assert p.session_id == "kept"
        repo.save.assert_not_called()
        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_session_is_saved_for_the_thread(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        p = EventProcessor(_make_config(thread, runner, repo=repo))
        await p.process(_make_system_event("s1"))

        repo.save.assert_called_once_with(thread.id, "s1", working_dir=None)

    @pytest.mark.asyncio
    async def test_saves_working_dir_to_repo(self, thread: MagicMock, runner: MagicMock) -> None:
        repo = MagicMock()
        repo.save = AsyncMock()
        config = _make_config(thread, runner, repo=repo, working_dir="/tmp/session/123")
        p = EventProcessor(config)

        await p.process(_make_system_event("s1"))

        repo.save.assert_called_once_with(thread.id, "s1", working_dir="/tmp/session/123")

    @pytest.mark.asyncio
    async def test_without_a_repo_nothing_is_saved_and_nothing_breaks(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """Webhook runs pass ``repo=None`` — the id is still tracked for the caller."""
        p = EventProcessor(_make_config(thread, runner, repo=None))
        await p.process(_make_system_event("s1"))
        assert p.session_id == "s1"

    @pytest.mark.asyncio
    async def test_a_new_session_is_announced(self, thread: MagicMock, runner: MagicMock) -> None:
        p = EventProcessor(_make_config(thread, runner))
        await p.process(_make_system_event("abcdef12-3456"))

        sends = _embed_sends(thread)
        assert len(sends) == 1
        embed = sends[0].kwargs["embed"]
        assert "abcdef12" in f"{embed.title} {embed.description} {embed.footer.text}"

    @pytest.mark.asyncio
    async def test_a_resumed_session_is_not_announced_again(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """The turn that started the session already said so."""
        p = EventProcessor(_make_config(thread, runner, session_id="s1"))
        await p.process(_make_system_event("s1"))
        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_announcement_is_made_once_per_turn(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """The runner may report the id more than once in a turn."""
        p = EventProcessor(_make_config(thread, runner))
        await p.process(_make_system_event("s1"))
        await p.process(_make_system_event("s1"))
        await p.process(_make_system_event("s1"))
        assert len(_embed_sends(thread)) == 1

    @pytest.mark.asyncio
    async def test_an_unknown_tui_prompt_is_reported_to_the_thread(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        """A prompt c-lord cannot answer must not stall the session silently."""
        p = EventProcessor(_make_config(thread, runner, repo=repo))
        await p.process(
            StreamEvent(
                message_type=MessageType.SYSTEM,
                unknown_tui_prompt="Do you trust this folder? (y/n)",
                session_id="ignored-here",
            )
        )

        sends = _embed_sends(thread)
        assert len(sends) == 1
        assert "Do you trust this folder?" in str(sends[0].kwargs["embed"].to_dict())
        # It is a warning about the pane, not a session report.
        assert p.session_id is None
        repo.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_warning_about_an_unknown_prompt_does_not_raise(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        thread.send = AsyncMock(side_effect=RuntimeError("Session is closed"))
        p = EventProcessor(_make_config(thread, runner))
        await p.process(StreamEvent(message_type=MessageType.SYSTEM, unknown_tui_prompt="???"))


class TestSessionSavedHook:
    """#856: ``on_session_saved`` fires once the session row exists."""

    @pytest.mark.asyncio
    async def test_the_hook_runs_after_the_first_save_only(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        order: list[str] = []
        repo.save = AsyncMock(side_effect=lambda *a, **k: order.append("save"))
        hook = AsyncMock(side_effect=lambda _tid: order.append("hook"))
        p = EventProcessor(_make_config(thread, runner, repo=repo, on_session_saved=hook))

        await p.process(_make_system_event("s1"))
        await p.process(_make_system_event("s1"))

        hook.assert_awaited_once_with(thread.id)
        assert order == ["save", "hook", "save"]

    @pytest.mark.asyncio
    async def test_the_hook_is_not_called_when_nothing_was_saved(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        hook = AsyncMock()
        p = EventProcessor(_make_config(thread, runner, repo=None, on_session_saved=hook))
        await p.process(_make_system_event("s1"))
        hook.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failing_hook_does_not_take_the_turn_down(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        hook = AsyncMock(side_effect=RuntimeError("stash write failed"))
        p = EventProcessor(_make_config(thread, runner, repo=repo, on_session_saved=hook))

        await p.process(_make_system_event("s1"))

        assert p.session_id == "s1"
        assert len(_embed_sends(thread)) == 1  # the session was still announced


class TestOnAssistantText:
    """Issue #53: assistant text is not posted from here — the transcript
    mirror delivers Claude's answer (#712). An ASSISTANT text event must NOT
    make the processor call ``thread.send``.
    """

    @pytest.mark.asyncio
    async def test_complete_text_does_not_send(self, thread: MagicMock, runner: MagicMock) -> None:
        p = EventProcessor(_make_config(thread, runner))
        await p.process(_make_system_event("s1"))
        thread.send.reset_mock()
        await p.process(
            StreamEvent(message_type=MessageType.ASSISTANT, text="Hello!", is_partial=False)
        )

        text_sends = [
            c for c in thread.send.call_args_list if c.args or c.kwargs.get("content") is not None
        ]
        assert text_sends == []

    @pytest.mark.asyncio
    async def test_partial_text_does_not_send(self, thread: MagicMock, runner: MagicMock) -> None:
        p = EventProcessor(_make_config(thread, runner))
        await p.process(_make_system_event("s1"))
        thread.send.reset_mock()
        await p.process(
            StreamEvent(message_type=MessageType.ASSISTANT, text="Hel", is_partial=True)
        )

        text_sends = [
            c for c in thread.send.call_args_list if c.args or c.kwargs.get("content") is not None
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
        p = EventProcessor(_make_config(thread, runner))
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
        p = EventProcessor(_make_config(thread, runner))
        ask = AskQuestion(question="Which?", options=[AskOption(label="A")])

        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await p.process(StreamEvent(message_type=MessageType.SYSTEM, pane_ask=ask))
        bridge.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_bridge_gets_the_menu_the_runner_and_who_may_answer(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        runner.answer_menu = AsyncMock()
        authorizer, ask_repo = MagicMock(), MagicMock()
        config = _make_config(
            thread, runner, authorizer=authorizer, ask_repo=ask_repo, notify_user_id=77
        )
        ask = AskQuestion(question="Which?", options=[AskOption(label="A")])

        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await EventProcessor(config).process(
                StreamEvent(message_type=MessageType.SYSTEM, pane_ask=ask)
            )

        assert bridge.call_args.args == (thread, ask, runner)
        assert bridge.call_args.kwargs == {
            "ask_repo": ask_repo,
            "authorizer": authorizer,
            "notify_user_id": 77,
        }

    @pytest.mark.asyncio
    async def test_a_runner_that_cannot_answer_menus_is_left_alone(self, thread: MagicMock) -> None:
        """No ``answer_menu`` → there is no pane to press keys in; skip, do not crash."""
        plain_runner = MagicMock(spec=["run", "interrupt"])
        p = EventProcessor(_make_config(thread, plain_runner))
        ask = AskQuestion(question="Which?", options=[AskOption(label="A")])

        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge:
            await p.process(StreamEvent(message_type=MessageType.SYSTEM, pane_ask=ask))
        bridge.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_menu_event_is_not_also_read_as_a_session_report(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        runner.answer_menu = AsyncMock()
        p = EventProcessor(_make_config(thread, runner, repo=repo))
        ask = AskQuestion(question="Which?", options=[AskOption(label="A")])

        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()):
            await p.process(
                StreamEvent(message_type=MessageType.SYSTEM, pane_ask=ask, session_id="s9")
            )

        assert p.session_id is None
        repo.save.assert_not_called()


class TestOnComplete:
    """RESULT events: the lamp, the error embed, and the final session id."""

    @pytest.mark.asyncio
    async def test_complete_does_not_send_session_complete_embed(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """Successful completion no longer posts a session_complete embed."""
        p = EventProcessor(_make_config(thread, runner, session_id="s1"))
        await p.process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                session_id="s1",
                cost_usd=0.01,
                duration_ms=1500,
            )
        )
        embed_sends = _embed_sends(thread)
        assert len(embed_sends) == 0

    @pytest.mark.asyncio
    async def test_a_clean_turn_turns_the_lamp_to_waiting(
        self, thread: MagicMock, runner: MagicMock, _quiet_login_alert: MagicMock
    ) -> None:
        status = _status()
        p = EventProcessor(_make_config(thread, runner, status=status))
        await p.process(_make_result_event(session_id="s1"))

        status.set_done.assert_awaited_once()
        status.set_error.assert_not_called()
        # #1309: a turn that ran proves Claude is logged in again.
        _quiet_login_alert.recovered.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failed_turn_posts_an_error_embed_and_a_red_lamp(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        status = _status()
        p = EventProcessor(_make_config(thread, runner, status=status))
        await p.process(_make_result_event(error="Something broke"))

        sends = _embed_sends(thread)
        assert len(sends) == 1
        assert "Something broke" in str(sends[0].kwargs["embed"].to_dict())
        status.set_error.assert_awaited_once()
        status.set_done.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_turn_without_a_lamp_still_posts_the_embed(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        p = EventProcessor(_make_config(thread, runner, status=None))
        await p.process(_make_result_event(error="Something broke"))
        assert len(_embed_sends(thread)) == 1

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
    async def test_the_failure_ping_falls_back_to_the_turns_poster(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """A caller that only knows ``notify_user_id`` still gets its failure pinged."""
        config = _make_config(thread, runner, notify_user_id=31, failure_notify_id=None)

        await EventProcessor(config).process(_make_result_event(error="boom"))

        assert _embed_sends(thread)[-1].kwargs.get("content") == "<@31>"
        # #915: recorded so the turn-end line does not ping the same person again.
        assert config.outcome.pinged_user_id == 31

    @pytest.mark.asyncio
    async def test_a_clean_turn_never_pings(self, thread: MagicMock, runner: MagicMock) -> None:
        """#681 AC4: the failure ping must not leak into normal completions."""
        config = _make_config(thread, runner, failure_notify_id=4242)
        p = EventProcessor(config)
        await p.process(_make_system_event("s1"))
        await p.process(_make_result_event(session_id="s1", text="All done."))

        assert not [c for c in thread.send.call_args_list if c.kwargs.get("content")]
        assert config.outcome.pinged_user_id is None

    @pytest.mark.asyncio
    async def test_result_text_not_posted(self, thread: MagicMock, runner: MagicMock) -> None:
        """Issue #53: RESULT.text is dropped — never posted to Discord."""
        p = EventProcessor(_make_config(thread, runner, session_id="s1"))
        await p.process(_make_result_event(session_id="s1", text="The final answer is 42."))

        answer_sends = [
            c
            for c in thread.send.call_args_list
            if "42" in " ".join(str(a) for a in c.args) or "42" in str(c.kwargs.get("content"))
        ]
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

    @pytest.mark.asyncio
    async def test_the_session_id_on_the_result_is_the_one_handed_back(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """Also with no repo: the caller still learns which session the turn ended on."""
        p = EventProcessor(_make_config(thread, runner, session_id="before", repo=None))
        await p.process(_make_result_event(session_id="after"))
        assert p.session_id == "after"

    @pytest.mark.asyncio
    async def test_a_result_without_a_session_id_keeps_the_known_one(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        p = EventProcessor(_make_config(thread, runner, session_id="known", repo=repo))
        await p.process(_make_result_event(session_id=None))
        assert p.session_id == "known"
        repo.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_system_event_that_also_ends_the_turn_is_handled_as_both(
        self, thread: MagicMock, runner: MagicMock, repo: MagicMock
    ) -> None:
        status = _status()
        p = EventProcessor(_make_config(thread, runner, repo=repo, status=status))
        await p.process(
            StreamEvent(message_type=MessageType.SYSTEM, session_id="s1", is_complete=True)
        )
        assert p.session_id == "s1"
        status.set_done.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_event_that_does_not_end_the_turn_leaves_the_lamp_alone(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        status = _status()
        p = EventProcessor(_make_config(thread, runner, status=status))
        await p.process(_make_system_event("s1"))
        await p.process(StreamEvent(message_type=MessageType.ASSISTANT, text="working…"))
        status.set_done.assert_not_called()
        status.set_error.assert_not_called()


class TestFailureOutcome:
    """Why a turn failed is recorded on ``RunConfig.outcome`` for the turn-end line."""

    @pytest.mark.asyncio
    async def test_a_clean_turn_leaves_the_outcome_untouched(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner)
        await EventProcessor(config).process(_make_result_event(session_id="s1"))
        assert config.outcome.no_response is False
        assert config.outcome.login_required is False
        assert config.outcome.usage_limit is None

    @pytest.mark.asyncio
    async def test_an_ordinary_error_is_not_a_turn_that_produced_nothing(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner)
        await EventProcessor(config).process(_make_result_event(error="Tool crashed"))
        assert config.outcome.no_response is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "prefix_name",
        [
            "NO_RESPONSE_ERROR_PREFIX",
            "TRUST_STUCK_ERROR_PREFIX",
            "TRUST_START_FAILED_ERROR_PREFIX",
            "FLEET_TMUX_RESTART_ERROR_PREFIX",
        ],
    )
    async def test_a_turn_that_never_produced_anything_is_marked_so(
        self, thread: MagicMock, runner: MagicMock, prefix_name: str
    ) -> None:
        """#562/#630/#684/#701: none of these may be announced as "Claude has finished"."""
        from c_lord.claude import tmux_runner

        config = _make_config(thread, runner)
        error = f"{getattr(tmux_runner, prefix_name)} details"
        await EventProcessor(config).process(_make_result_event(error=error))

        assert config.outcome.no_response is True
        assert config.outcome.login_required is False

    @pytest.mark.asyncio
    async def test_a_turn_refused_for_want_of_login_says_so_and_alerts_the_owner(
        self, thread: MagicMock, runner: MagicMock, _quiet_login_alert: MagicMock
    ) -> None:
        """#812/#1309: resending cannot help; someone has to run /login on the host."""
        from c_lord.claude.tmux_runner import LOGIN_REQUIRED_ERROR_PREFIX

        config = _make_config(thread, runner)
        await EventProcessor(config).process(
            _make_result_event(error=f"{LOGIN_REQUIRED_ERROR_PREFIX} Please run /login")
        )

        assert config.outcome.no_response is True
        assert config.outcome.login_required is True
        _quiet_login_alert.report.assert_awaited_once()
        assert _quiet_login_alert.report.call_args.args[0] is thread
        _quiet_login_alert.recovered.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_failures_do_not_raise_the_login_alert(
        self, thread: MagicMock, runner: MagicMock, _quiet_login_alert: MagicMock
    ) -> None:
        await EventProcessor(_make_config(thread, runner)).process(
            _make_result_event(error="Tool crashed")
        )
        _quiet_login_alert.report.assert_not_called()
        # A failed turn proves nothing about being logged in either.
        _quiet_login_alert.recovered.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refused_model_is_named_on_the_outcome(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """#484: only ``/model`` can fix it, so the turn-end line has to name the model."""
        from c_lord.claude.tmux_runner import MODEL_UNAVAILABLE_ERROR_PREFIX

        config = _make_config(thread, runner)
        error = f'{MODEL_UNAVAILABLE_ERROR_PREFIX} the model "opus-9" is not available'
        await EventProcessor(config).process(_make_result_event(error=error))

        assert config.outcome.model_unavailable == "opus-9"
        assert config.outcome.no_response is True

    @pytest.mark.asyncio
    async def test_an_api_error_carries_advice_but_is_not_an_empty_turn(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """#908: Claude did produce output before the API failed."""
        from c_lord.api_error import API_ERROR_PREFIX, advice_for_error

        error = f"{API_ERROR_PREFIX} 529 Overloaded"
        config = _make_config(thread, runner)
        await EventProcessor(config).process(_make_result_event(error=error))

        assert config.outcome.api_error == advice_for_error(error)
        assert config.outcome.api_error
        assert config.outcome.no_response is False

    @pytest.mark.asyncio
    async def test_the_failure_ping_is_recorded_on_the_outcome(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        config = _make_config(thread, runner, failure_notify_id=4242, notify_user_id=31)
        await EventProcessor(config).process(_make_result_event(error="boom"))
        assert config.outcome.pinged_user_id == 4242


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

        embed_sends = _embed_sends(thread)
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
        await p.process(_make_system_event("s1"))
        await p.process(_make_result_event(session_id="s1"))

        file_sends = [c for c in thread.send.await_args_list if "file" in c.kwargs]
        assert file_sends == [], f"an empty progress.txt was posted: {file_sends!r}"
        sent[0].delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embed_is_deleted_only_once(self, thread: MagicMock, runner: MagicMock) -> None:
        start = _deletable_message()
        thread.send = AsyncMock(return_value=start)

        p = EventProcessor(_make_config(thread, runner))
        await p.process(_make_system_event("s1"))
        await p.process(_make_result_event(session_id="s1"))
        # A second RESULT (the runner is allowed to repeat itself) finds nothing to remove.
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
        await p.process(_make_system_event("s1"))
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
        await p.process(_make_system_event("s1"))
        await p.process(_make_result_event(session_id="s1", error="boom"))

        start, error = sent[0], sent[1]
        start.delete.assert_awaited_once()
        error.delete.assert_not_called()
        assert [c for c in error.edit.await_args_list if "attachments" in c.kwargs] == []

    @pytest.mark.asyncio
    async def test_a_stopped_turn_takes_its_embed_away_too(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        from c_lord.claude.tmux_runner import STOPPED_BY_USER_ERROR

        start = _deletable_message()
        thread.send = AsyncMock(return_value=start)

        p = EventProcessor(_make_config(thread, runner))
        await p.process(_make_system_event("s1"))
        await p.process(_make_result_event(error=STOPPED_BY_USER_ERROR))

        start.delete.assert_awaited_once()
