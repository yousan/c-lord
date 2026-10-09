"""Tests for c_lord/session_stop.py — the one stop path behind ⏹ / /stop / !stop (#878)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.session_stop import (
    StopOutcome,
    StopResult,
    stop_thread_work,
    suppress_wakeups,
)

WORKING = "✶ Infusing… (4s · ↓ 140 tokens)\n────\n❯ \n────\n"
IDLE = "✻ Cooked for 3s · done 8:46 AM\n────\n❯ \n────\n"


def _tmux(*, pane: str = IDLE, interrupt_ok: bool = True, shells: int = 0) -> MagicMock:
    tmux = MagicMock()
    tmux.capture_pane = MagicMock(return_value=pane)
    tmux.is_claude_running = MagicMock(return_value=True)
    tmux.send_interrupt = MagicMock(return_value=interrupt_ok)
    tmux.stop_tool_shells = MagicMock(return_value=shells)
    return tmux


class TestStopThreadWork:
    async def test_running_turn_is_interrupted_through_its_runner(self) -> None:
        runner = MagicMock()
        runner.interrupt = AsyncMock(return_value=True)
        tmux = _tmux()
        result = await stop_thread_work(1, runner=runner, tmux=tmux)
        runner.interrupt.assert_awaited_once()
        tmux.send_interrupt.assert_not_called()  # the runner already sent C-c
        assert result.outcome is StopOutcome.STOPPED

    async def test_background_shells_are_stopped_too(self) -> None:
        runner = MagicMock()
        runner.interrupt = AsyncMock(return_value=True)
        result = await stop_thread_work(1, runner=runner, tmux=_tmux(shells=2))
        assert result.shells_stopped == 2

    async def test_turn_c_lord_did_not_start_is_interrupted_in_the_pane(self) -> None:
        """A turn woken by a task notification has no runner — it must still stop."""
        tmux = _tmux(pane=WORKING)
        result = await stop_thread_work(1, runner=None, tmux=tmux)
        tmux.send_interrupt.assert_called_once_with(1)
        assert result.outcome is StopOutcome.STOPPED

    async def test_idle_pane_gets_no_c_c(self) -> None:
        """A C-c at an idle prompt arms "press again to exit" — never send one there."""
        tmux = _tmux(pane=IDLE)
        result = await stop_thread_work(1, runner=None, tmux=tmux)
        tmux.send_interrupt.assert_not_called()
        assert result.outcome is StopOutcome.NOTHING_RUNNING

    async def test_idle_pane_with_background_shells_counts_as_stopped(self) -> None:
        result = await stop_thread_work(1, runner=None, tmux=_tmux(pane=IDLE, shells=1))
        assert result.outcome is StopOutcome.STOPPED

    async def test_failed_interrupt_is_not_reported_as_stopped(self) -> None:
        runner = MagicMock()
        runner.interrupt = AsyncMock(return_value=False)
        result = await stop_thread_work(1, runner=runner, tmux=_tmux())
        assert result.outcome is StopOutcome.FAILED

    async def test_failed_pane_interrupt_is_not_reported_as_stopped(self) -> None:
        tmux = _tmux(pane=WORKING, interrupt_ok=False)
        result = await stop_thread_work(1, runner=None, tmux=tmux)
        assert result.outcome is StopOutcome.FAILED

    async def test_runner_returning_none_counts_as_sent(self) -> None:
        """Interruptables that predate the bool return (``-> None``) still count."""
        runner = MagicMock()
        runner.interrupt = AsyncMock(return_value=None)
        result = await stop_thread_work(1, runner=runner, tmux=None)
        assert result.outcome is StopOutcome.STOPPED

    async def test_nothing_known_about_the_thread(self) -> None:
        result = await stop_thread_work(1, runner=None, tmux=None)
        assert result.outcome is StopOutcome.NOTHING_RUNNING


class TestSuppressWakeups:
    async def test_a_turn_woken_after_stop_is_interrupted(self) -> None:
        panes = iter([IDLE, WORKING, IDLE, IDLE])
        tmux = _tmux()
        tmux.capture_pane = MagicMock(side_effect=lambda _t: next(panes, IDLE))
        sent = await suppress_wakeups(
            1,
            tmux=tmux,
            still_ours=lambda: True,
            window=4,
            interval=1,
            settle=1,
            sleep=AsyncMock(),
        )
        assert sent == 1
        tmux.send_interrupt.assert_called_once_with(1)

    async def test_idle_pane_is_left_alone(self) -> None:
        tmux = _tmux(pane=IDLE)
        sent = await suppress_wakeups(
            1,
            tmux=tmux,
            still_ours=lambda: True,
            window=3,
            interval=1,
            settle=1,
            sleep=AsyncMock(),
        )
        assert sent == 0
        tmux.send_interrupt.assert_not_called()

    async def test_a_new_message_from_the_user_ends_the_guard(self) -> None:
        """The user's next turn must never be interrupted by the guard."""
        tmux = _tmux(pane=WORKING)
        sent = await suppress_wakeups(
            1,
            tmux=tmux,
            still_ours=lambda: False,
            window=10,
            interval=1,
            settle=1,
            sleep=AsyncMock(),
        )
        assert sent == 0
        tmux.send_interrupt.assert_not_called()

    async def test_interrupts_are_capped(self) -> None:
        tmux = _tmux(pane=WORKING)
        sent = await suppress_wakeups(
            1,
            tmux=tmux,
            still_ours=lambda: True,
            window=100,
            interval=1,
            settle=1,
            max_interrupts=2,
            sleep=AsyncMock(),
        )
        assert sent == 2


def test_stop_result_defaults() -> None:
    assert StopResult(StopOutcome.STOPPED).shells_stopped == 0


@pytest.mark.parametrize("outcome", list(StopOutcome))
def test_every_outcome_has_a_reply(outcome: StopOutcome) -> None:
    from c_lord.session_stop import stop_reply

    content, embed, ephemeral = stop_reply(StopResult(outcome, shells_stopped=1))
    assert (content or embed) is not None
    if outcome is StopOutcome.STOPPED:
        assert embed is not None and "stopped" in (embed.title or "").lower()
        assert ephemeral is False
    else:
        assert embed is None or "stopped" not in (embed.title or "").lower()
