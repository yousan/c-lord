"""#977: stopping a workspace ends a turn parked on a Discord question card.

The workspace stop withdraws the runner and waits 10s for the turn to end. A
turn waiting on ``bridge_pane_ask`` never looked at that: it kept waiting for a
click (or the pane) for up to ``ASK_ANSWER_TIMEOUT`` (24h) — after the window
had been killed, a pane capture fails, which the bridge counts as "unknown,
keep waiting". The run then logged ``run_claude: enter`` with no ``exit`` and
the card stayed clickable in the archived thread.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask

THREAD = 977_001


def _question() -> AskQuestion:
    return AskQuestion(
        question="どちらにしますか？",
        header="Pick",
        options=[AskOption(label="A", description=""), AskOption(label="B", description="")],
        multi_select=False,
    )


def _runner(question: AskQuestion) -> MagicMock:
    runner = MagicMock()
    runner.stopped = False
    runner.peek_pending_ask = AsyncMock(return_value=question)
    # The window is gone: every capture fails, which the bridge must not
    # read as "the menu was answered in the pane".
    runner.peek_menu_state = AsyncMock(return_value=(None, False))
    runner.cancel_menu = AsyncMock(return_value=True)
    runner.answer_menu = AsyncMock(return_value=True)
    return runner


def _thread() -> tuple[MagicMock, MagicMock]:
    thread = MagicMock()
    thread.id = THREAD
    msg = MagicMock()
    msg.id = THREAD + 1
    msg.edit = AsyncMock()
    thread.send = AsyncMock(return_value=msg)
    return thread, msg


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    yield
    ask_bus.unregister(THREAD)


async def test_a_stopped_runner_releases_the_question_card() -> None:
    """RED (#977): the bridge kept waiting after the runner was stopped."""
    question = _question()
    runner = _runner(question)
    thread, msg = _thread()
    task = asyncio.create_task(bridge_pane_ask(thread, question, runner))
    await asyncio.sleep(0.05)
    assert not task.done()  # parked on the card, as in production

    runner.stopped = True  # end_turn_for_workspace_stop → runner.withdraw()
    await asyncio.wait_for(task, 1.0)

    edits = [c.kwargs for c in msg.edit.await_args_list]
    assert any(e.get("view") is None and "停止" in (e.get("content") or "") for e in edits), (
        "the card must lose its buttons and say why"
    )
    assert ask_bus.closed_reason(THREAD) == "stopped"
    runner.answer_menu.assert_not_awaited()


async def test_a_runner_that_is_not_stopped_keeps_waiting() -> None:
    """AC2: nothing changes for a turn nobody stopped."""
    question = _question()
    runner = _runner(question)
    thread, _msg = _thread()
    task = asyncio.create_task(bridge_pane_ask(thread, question, runner))
    await asyncio.sleep(0.1)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestTheStopDoesNotLeaveTheTurnRunning:
    """Backstop: a turn still running after the wait is cancelled as a user stop."""

    def _chat(self):
        from c_lord.cogs.claude_chat import ClaudeChatCog

        bot = MagicMock()
        bot.channel_id = 999
        return ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())

    async def test_the_turn_task_is_cancelled_after_the_wait(self) -> None:
        chat = self._chat()
        task = asyncio.create_task(asyncio.sleep(10))
        chat._active_tasks[555] = task
        await asyncio.wait_for(chat.wait_for_turn_end(555, timeout=0.05), 2.0)
        assert task.cancelled()

    async def test_a_cancelled_run_of_a_stopped_runner_exits_as_stopped(self, caplog) -> None:
        """``run_claude: exit (outcome=stopped)`` — not ``cancelled``."""
        import logging

        from c_lord.cogs import _run_helper
        from c_lord.cogs.run_config import RunConfig

        runner = MagicMock()
        runner.stopped = True
        runner.preempted = False
        thread = MagicMock()
        thread.id = 556
        config = RunConfig(thread=thread, runner=runner, repo=None, prompt="hi")

        async def parked(_config, _ctx):
            await asyncio.sleep(10)

        with caplog.at_level(logging.INFO):
            orig = _run_helper._run_turn
            _run_helper._run_turn = parked  # type: ignore[assignment]
            try:
                task = asyncio.create_task(_run_helper.run_claude_with_config(config))
                await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                _run_helper._run_turn = orig  # type: ignore[assignment]
        assert "outcome=stopped" in caplog.text
