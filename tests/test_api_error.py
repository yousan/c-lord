"""#908: a turn Claude Code ended on an API error."""

from __future__ import annotations

import pytest

from c_lord.api_error import (
    API_ERROR_PREFIX,
    GENERIC_ADVICE,
    RESUME_ADVICE,
    SAFEGUARDS_ADVICE,
    advice,
    advice_for_error,
    api_error_text,
    headline,
    runner_error,
)

MID = "API Error: Connection lost mid-response. The response above may be incomplete."
SERVER = "API Error: Server error mid-response. The response above may be incomplete."
FLAGGED = (
    "API Error: Opus 5.5's safeguards flagged this session (https://www.anthropic.com/legal/aup)."
    " You may be seeing this for the first time.\n\n"
    "Double press esc to edit your last message, or try a different model with /model.\n\n"
    "Request ID: req_1"
)


def api_error_event(text: str = MID, **extra: object) -> dict:
    event = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "error": "server_error",
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]},
    }
    event.update(extra)
    return event


class TestRecognition:
    def test_a_synthetic_api_error_is_recognised(self) -> None:
        assert api_error_text(api_error_event()) == MID

    def test_an_ordinary_answer_is_not(self) -> None:
        event = {"type": "assistant", "message": {"content": [{"type": "text", "text": MID}]}}
        assert api_error_text(event) is None

    def test_a_rate_limit_is_left_to_its_own_path(self) -> None:
        """AC5: #631 reports limits; this module must not claim them."""
        event = api_error_event("You've hit your weekly limit", error="rate_limit")
        event["apiErrorStatus"] = 429
        assert api_error_text(event) is None

    def test_a_subagent_error_is_not_the_threads_turn(self) -> None:
        assert api_error_text(api_error_event(isSidechain=True)) is None

    def test_odd_shapes_do_not_raise(self) -> None:
        for event in (None, "x", {}, {"type": "assistant", "isApiErrorMessage": True}):
            assert api_error_text(event) is None


class TestWhatTheReaderIsTold:
    def test_mid_response_says_send_continue(self) -> None:
        """AC3."""
        assert advice(MID) == RESUME_ADVICE
        assert advice(SERVER) == RESUME_ADVICE
        assert "続けて" in RESUME_ADVICE

    def test_safeguards_point_at_model_or_a_new_thread(self) -> None:
        """AC4."""
        assert advice(FLAGGED) == SAFEGUARDS_ADVICE
        assert "/model" in SAFEGUARDS_ADVICE and "新しいスレッド" in SAFEGUARDS_ADVICE

    def test_anything_else_gets_the_generic_line(self) -> None:
        assert advice("API Error: Can't reach the API server") == GENERIC_ADVICE

    def test_the_headline_drops_the_terminal_only_steps(self) -> None:
        """AC4: 'Double press esc' is for someone at the terminal."""
        line = headline(FLAGGED)
        assert line.startswith("API Error: Opus 5.5's safeguards flagged this session")
        assert "Double press esc" not in line
        assert headline(MID) == "API Error: Connection lost mid-response."

    def test_the_runner_error_round_trips_to_the_advice(self) -> None:
        error = runner_error(FLAGGED)
        assert error.startswith(API_ERROR_PREFIX)
        assert "Double press esc" not in error
        assert advice_for_error(error) == SAFEGUARDS_ADVICE
        assert advice_for_error(runner_error(MID)) == RESUME_ADVICE


class TestHowTheTurnEnds:
    """#908 AC1/AC3/AC4: ❌ on the lamp, the Japanese line in the thread."""

    def test_the_error_embed_says_what_to_do(self) -> None:
        from c_lord.cogs._run_helper import _make_error_embed

        embed = _make_error_embed(runner_error(MID))
        assert "API エラー" in (embed.title or "")
        assert RESUME_ADVICE in (embed.description or "")
        embed = _make_error_embed(runner_error(FLAGGED))
        assert SAFEGUARDS_ADVICE in (embed.description or "")
        assert "Double press esc" not in (embed.description or "")

    def test_the_turn_end_line_is_not_finished(self) -> None:
        from c_lord.discord_ui.thread_dashboard import _completion_text

        text = _completion_text(42, False, api_error=RESUME_ADVICE)
        assert text.startswith("❌")
        assert RESUME_ADVICE in text
        assert "has finished" not in text
        assert text.endswith("<@42>")

    @pytest.mark.asyncio
    async def test_the_lamp_goes_red_and_the_outcome_knows_why(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.claude.types import MessageType, StreamEvent
        from c_lord.cogs.event_processor import EventProcessor
        from c_lord.cogs.run_config import RunConfig

        thread = MagicMock()
        thread.id = 908
        thread.send = AsyncMock(return_value=MagicMock())
        status = MagicMock()
        status.set_error = AsyncMock()
        status.set_done = AsyncMock()
        config = RunConfig(thread=thread, runner=MagicMock(), prompt="hello", status=status)
        p = EventProcessor(config)
        await p.process(
            StreamEvent(message_type=MessageType.RESULT, is_complete=True, error=runner_error(MID))
        )
        status.set_error.assert_awaited_once()
        status.set_done.assert_not_awaited()
        assert config.outcome.api_error == RESUME_ADVICE
        # Claude did produce output before the error: not a "never started" turn.
        assert config.outcome.no_response is False

    def test_the_turn_end_event_calls_it_an_api_error(self) -> None:
        from unittest.mock import MagicMock

        from c_lord.cogs._run_helper import _failure_reason
        from c_lord.cogs.run_config import RunConfig

        config = RunConfig(thread=MagicMock(), runner=MagicMock(), prompt="x")
        config.outcome.error = runner_error(MID)
        config.outcome.api_error = RESUME_ADVICE
        assert _failure_reason(config, "error") == "api_error"
