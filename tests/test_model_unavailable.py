"""#484: a turn Claude refused because the configured model is unusable says so.

``/model set`` accepts any well-formed model ID and leaves the verdict to the
CLI (#478).  Claude Code 2.1.283 does NOT fail to start on a model it does not
know — it starts, and answers every prompt with one line, then goes idle::

    ● There's an issue with the selected model (claude-nonexistent-zzz). It may
      not exist or you may not have access to it. Run /model to pick a different
      model.

Measured on staging: c-lord ended that turn as a normal answer ("🟡 Claude has
finished"), with nothing saying the c-lord setting is what has to change.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import (
    MODEL_UNAVAILABLE_ERROR_PREFIX,
    TmuxClaudeRunner,
    extract_model_unavailable,
)

FIXTURES = Path(__file__).parent / "fixtures" / "panes"

# Advice that must never accompany an unusable model: resending cannot help.
_FORBIDDEN = ("もう一度送る", "Send the message again", "開始しませんでした")

# Real capture: Claude Code 2.1.283 on staging, global model set to
# ``claude-nonexistent-zzz`` (160-column window, so the line wraps).
MODEL_PANE = (FIXTURES / "model_not_found.txt").read_text()
PROMPT = "#484 RED: こんにちは。1行で返事してください"
# The same claude before the turn: the banner already names the model, but no
# turn has been refused yet.
CLEAN_PANE = "\n".join(
    line
    for line in MODEL_PANE.splitlines()
    if "issue with the selected model" not in line
    and line.strip() != "model."
    and "Worked for" not in line
    and "#484 RED" not in line
)


class TestExtractModelUnavailable:
    def test_real_pane_is_recognised(self) -> None:
        assert extract_model_unavailable(MODEL_PANE) == "claude-nonexistent-zzz"

    def test_tool_result_gutter_variant(self) -> None:
        pane = (
            "> hi\n  ⎿  There's an issue with the selected model (claude-fable-5). "
            "It may not exist or you may not have access to it.\n\n❯ \n"
        )
        assert extract_model_unavailable(pane) == "claude-fable-5"

    def test_banner_alone_is_not_a_refusal(self) -> None:
        """The start banner names the model before any turn — it refused nothing."""
        assert extract_model_unavailable(CLEAN_PANE) is None

    def test_prose_quoting_the_message_is_not_a_refusal(self) -> None:
        pane = (
            "● 無効なモデルだと「There's an issue with the selected model (x)」と出ます。\n"
            "  `There's an issue with the selected model (x)` を検知するのが #484 です。\n❯ \n"
        )
        assert extract_model_unavailable(pane) is None

    def test_empty(self) -> None:
        assert extract_model_unavailable("") is None


@pytest.fixture
def tmux_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.capture_pane.return_value = ""
    mgr.is_claude_running.return_value = True
    mgr.start_claude.return_value = True
    mgr.send_input.return_value = True
    mgr.send_interrupt.return_value = True
    mgr.kill_session.return_value = True
    mgr.duplicate_window_names.return_value = []
    mgr.session_name = "clord"
    return mgr


@pytest.fixture
def runner(tmux_manager: MagicMock) -> TmuxClaudeRunner:
    return TmuxClaudeRunner(tmux_manager=tmux_manager, thread_id=484, timeout_seconds=60)


async def _run(
    runner: TmuxClaudeRunner,
    captures: list[str],
    final: str,
    *,
    idle: float = 30.0,
    prompt: str = PROMPT,
) -> list:
    def _capture(*_a: object, **_k: object) -> str:
        return captures.pop(0) if captures else final

    runner._tmux.capture_pane.side_effect = _capture  # type: ignore[attr-defined]
    with (
        patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
        patch("c_lord.claude.tmux_runner._IDLE_TIMEOUT", idle),
        patch("c_lord.claude.tmux_runner._TURN_START_GRACE", idle),
        patch("c_lord.claude.tmux_runner._STARTUP_TIMEOUT", 0.04),
        patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
    ):

        async def _drain() -> list:
            return [e async for e in runner.run(prompt)]

        events = await asyncio.wait_for(_drain(), timeout=8)
    return [e for e in events if e.is_complete]


class TestRunnerReportsModel:
    @pytest.mark.asyncio
    async def test_refused_turn_is_a_model_error(self, runner: TmuxClaudeRunner) -> None:
        """RED before the fix: the turn ended as a normal answer (error=None)."""
        result = await _run(runner, [CLEAN_PANE, CLEAN_PANE], MODEL_PANE)
        assert len(result) == 1
        error = result[0].error
        assert error is not None
        assert error.startswith(MODEL_UNAVAILABLE_ERROR_PREFIX), error
        assert "claude-nonexistent-zzz" in error
        assert "/model set" in error
        for phrase in _FORBIDDEN:
            assert phrase not in error

    @pytest.mark.asyncio
    async def test_refusal_drawn_before_the_first_poll_is_caught(
        self, runner: TmuxClaudeRunner
    ) -> None:
        """The refusal takes ~2s, so the first capture can already show it."""
        result = await _run(runner, [], MODEL_PANE, idle=0.3)
        assert len(result) == 1
        assert (result[0].error or "").startswith(MODEL_UNAVAILABLE_ERROR_PREFIX)

    @pytest.mark.asyncio
    async def test_refusal_from_an_earlier_turn_does_not_refuse_this_one(
        self, runner: TmuxClaudeRunner
    ) -> None:
        """After ``/model set`` fixes it, a --resume redraws the old refusal above."""
        runner._tmux.is_claude_running.return_value = False  # type: ignore[attr-defined]
        runner.timeout_seconds = 1
        pane = (
            "❯ 前の依頼\n\n● There's an issue with the selected model (bogus). It may "
            "not exist or you may not have access to it.\n\n"
            "❯ 前の依頼\n\n● はい、起動しました。\n\n────\n❯ \n────\n"
        )
        with patch.object(TmuxClaudeRunner, "_handle_startup_prompts", AsyncMock()):
            result = await _run(runner, [], pane, idle=0.3, prompt="前の依頼")
        assert len(result) == 1
        assert not (result[0].error or "").startswith(MODEL_UNAVAILABLE_ERROR_PREFIX)


class TestDiscordWording:
    def test_embed_names_the_model_and_the_fix(self) -> None:
        from c_lord.cogs._run_helper import _make_error_embed

        embed = _make_error_embed(
            f"{MODEL_UNAVAILABLE_ERROR_PREFIX} Claude Code cannot use the model "
            '"claude-nonexistent-zzz" and did not run this turn.'
        )
        body = f"{embed.title}\n{embed.description}"
        assert "モデル" in (embed.title or "")
        assert "claude-nonexistent-zzz" in body
        assert "/model set" in body
        for phrase in _FORBIDDEN:
            assert phrase not in body, body

    def test_completion_line_names_the_fix(self) -> None:
        from c_lord.discord_ui.thread_dashboard import _completion_text

        text = _completion_text(42, True, None, model_unavailable="claude-nonexistent-zzz")
        assert "/model set" in text
        assert "claude-nonexistent-zzz" in text
        assert "<@42>" in text
        for phrase in _FORBIDDEN:
            assert phrase not in text, text


class TestEventProcessorOutcome:
    @pytest.mark.asyncio
    async def test_model_error_sets_outcome(self) -> None:
        from c_lord.claude.types import MessageType, StreamEvent
        from c_lord.cogs.event_processor import EventProcessor
        from c_lord.cogs.run_config import RunConfig

        thread = MagicMock()
        thread.id = 484
        thread.send = AsyncMock(return_value=MagicMock())
        config = RunConfig(thread=thread, runner=MagicMock(), prompt="hello")
        p = EventProcessor(config)
        await p.process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error=(
                    f'{MODEL_UNAVAILABLE_ERROR_PREFIX} Claude Code cannot use the model "bogus" '
                    "and did not run this turn."
                ),
            )
        )
        assert config.outcome.no_response is True
        assert config.outcome.model_unavailable == "bogus"
