"""#812: a turn Claude refused because its login expired says so.

Claude Code answers every prompt with ``Login expired · Please run /login``
when its credentials are gone, then goes idle.  c-lord used to report that as
"Claude never started this turn — send the message again": the wrong cause,
and advice that cannot work until someone runs ``/login`` on the host.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import (
    LOGIN_REQUIRED_ERROR_PREFIX,
    NO_RESPONSE_ERROR_PREFIX,
    TmuxClaudeRunner,
    extract_login_required,
)

FIXTURES = Path(__file__).parent / "fixtures" / "panes"

# Retry advice that must never accompany a login failure (AC2/AC3).
_FORBIDDEN = ("もう一度送る", "Send the message again", "開始しませんでした")


def _pane(name: str) -> str:
    return (FIXTURES / name).read_text()


# Real capture: Claude Code 2.1.282 with expired credentials, after "hello".
LOGIN_PANE = _pane("login_expired.txt")
# The same claude one keystroke earlier — the footer already says
# "Not logged in · Run /login", but no turn has been refused yet.
CLEAN_PANE = "\n".join(
    line
    for line in LOGIN_PANE.splitlines()
    if "Login expired" not in line and "Cooked" not in line and "hello" not in line
)


class TestExtractLoginRequired:
    def test_real_pane_is_recognised(self) -> None:
        assert extract_login_required(LOGIN_PANE) == "Login expired · Please run /login"

    def test_tool_result_gutter_variant(self) -> None:
        """The shape the #812 report reconstructed from Discord."""
        pane = "> do it\n  ⎿  Login expired · Please run /login\n\n❯ \n"
        assert extract_login_required(pane) == "Login expired · Please run /login"

    def test_other_auth_failures_share_the_suffix(self) -> None:
        pane = "● Invalid API key · Please run /login\n❯ \n"
        assert extract_login_required(pane) == "Invalid API key · Please run /login"

    def test_footer_hint_alone_is_not_a_refusal(self) -> None:
        """The status-line hint is there before any turn — it refused nothing."""
        assert extract_login_required(CLEAN_PANE) is None

    def test_prose_quoting_the_message_is_not_a_refusal(self) -> None:
        pane = (
            "● ログインが切れると「Login expired · Please run /login」と出ます。\n"
            "  `Login expired · Please run /login` を検知するのが #812 です。\n❯ \n"
        )
        assert extract_login_required(pane) is None

    def test_empty(self) -> None:
        assert extract_login_required("") is None


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
    return TmuxClaudeRunner(tmux_manager=tmux_manager, thread_id=812, timeout_seconds=60)


async def _run(
    runner: TmuxClaudeRunner, captures: list[str], final: str, *, idle: float = 30.0
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
            return [e async for e in runner.run("hello")]

        events = await asyncio.wait_for(_drain(), timeout=8)
    return [e for e in events if e.is_complete]


class TestRunnerReportsLogin:
    @pytest.mark.asyncio
    async def test_login_expired_turn_is_a_login_error(self, runner: TmuxClaudeRunner) -> None:
        """AC1: RED before the fix — the turn ended as "No response — … never started"."""
        result = await _run(runner, [CLEAN_PANE, CLEAN_PANE], LOGIN_PANE)
        assert len(result) == 1
        error = result[0].error
        assert error is not None
        assert error.startswith(LOGIN_REQUIRED_ERROR_PREFIX), error
        assert not error.startswith(NO_RESPONSE_ERROR_PREFIX)
        # AC2
        assert "/login" in error
        for phrase in _FORBIDDEN:
            assert phrase not in error

    @pytest.mark.asyncio
    async def test_report_shape_is_a_login_error(self, runner: TmuxClaudeRunner) -> None:
        """The pane shape of the #812 report: nothing scrapable as an answer.

        RED before the fix: "No response — Claude never started this turn …
        Send the message again", exactly what production posted.
        """
        clean = "> 起動して\n\n────\n❯ \n────\n  ? for shortcuts\n"
        refused = (
            "> 起動して\n  ⎿  Login expired · Please run /login\n\n────\n❯ \n────\n"
            "  ? for shortcuts\n"
        )
        result = await _run(runner, [clean, clean], refused, idle=0.3)
        assert len(result) == 1
        error = result[0].error or ""
        assert error.startswith(LOGIN_REQUIRED_ERROR_PREFIX), error

    @pytest.mark.asyncio
    async def test_banner_from_an_earlier_turn_does_not_refuse_this_one(
        self, runner: TmuxClaudeRunner
    ) -> None:
        """A resumed session redraws yesterday's refusal; today's turn is not refused."""
        runner.timeout_seconds = 1  # a frozen residual pane only ends at the backstop
        result = await _run(runner, [LOGIN_PANE, LOGIN_PANE], LOGIN_PANE, idle=0.3)
        assert len(result) == 1
        assert not (result[0].error or "").startswith(LOGIN_REQUIRED_ERROR_PREFIX)


class TestDiscordWording:
    def test_embed_names_login_and_no_retry(self) -> None:
        """AC3 (embed)."""
        from c_lord.cogs._run_helper import _make_error_embed

        embed = _make_error_embed(
            f"{LOGIN_REQUIRED_ERROR_PREFIX} Claude Code replied "
            '"Login expired · Please run /login" and did not run this turn.'
        )
        body = f"{embed.title}\n{embed.description}"
        assert "ログイン" in (embed.title or "")
        assert "/login" in body
        for phrase in _FORBIDDEN:
            assert phrase not in body, body

    def test_completion_line_names_login_and_no_retry(self) -> None:
        """AC3 (the turn-end line that replaces "終わりました")."""
        from c_lord.discord_ui.thread_dashboard import _completion_text

        text = _completion_text(42, True, None, login_required=True)
        assert "/login" in text
        assert "<@42>" in text
        for phrase in _FORBIDDEN:
            assert phrase not in text, text


class TestEventProcessorOutcome:
    @pytest.mark.asyncio
    async def test_login_error_sets_outcome(self) -> None:
        from c_lord.claude.types import MessageType, StreamEvent
        from c_lord.cogs.event_processor import EventProcessor
        from c_lord.cogs.run_config import RunConfig

        thread = MagicMock()
        thread.id = 812
        thread.send = AsyncMock(return_value=MagicMock())
        config = RunConfig(thread=thread, runner=MagicMock(), prompt="hello")
        p = EventProcessor(config)
        await p.process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error=f"{LOGIN_REQUIRED_ERROR_PREFIX} Claude Code needs /login.",
            )
        )
        assert config.outcome.no_response is True
        assert config.outcome.login_required is True
