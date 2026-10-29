"""#1010: a workspace stop ends the turn whatever entry point started it.

``/workspace-stop`` reached the turn through ``ClaudeChatCog``'s own
``_active_runners`` / ``_active_tasks``. A ``/skill`` turn registers in neither
(it runs ``run_claude_with_config`` straight from ``SkillCommandCog``), so its
runner was never withdrawn: parked on a question card it waited out the 24h
``ASK_ANSWER_TIMEOUT`` with ``run_claude: enter`` and no ``exit`` (#977 for
``/skill``), and #909's ``outcome=stopped`` never reached it either.

Every run is in ``_run_helper``'s in-flight ledger, so the stop reads that.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from c_lord.cogs import _run_helper
from c_lord.cogs.run_config import RunConfig

THREAD = 1_010_001


class _Runner:
    """Just enough of ``TmuxClaudeRunner`` for the stop."""

    preempted = False

    def __init__(self) -> None:
        self.stopped = False

    def withdraw(self, *, silent: bool = False) -> None:
        self.stopped = True


def _chat():
    from c_lord.cogs.claude_chat import ClaudeChatCog

    bot = MagicMock()
    bot.channel_id = 999
    return ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())


async def _skill_turn(monkeypatch, turn) -> tuple[_Runner, asyncio.Task]:
    """Start a run the way ``SkillCommandCog`` does — no chat bookkeeping."""
    monkeypatch.setattr(_run_helper, "_run_turn", turn)
    runner = _Runner()
    thread = MagicMock()
    thread.id = THREAD
    config = RunConfig(thread=thread, runner=runner, repo=None, prompt="/review")
    task = asyncio.create_task(_run_helper.run_claude_with_config(config))
    await asyncio.sleep(0.02)
    assert not task.done()
    return runner, task


async def _stop(chat) -> bool:
    """What ``SessionManageCog`` does around the window kill (#909/#977)."""
    ended = chat.end_turn_for_workspace_stop(THREAD)
    await asyncio.wait_for(chat.wait_for_turn_end(THREAD, timeout=0.2), 3.0)
    return ended


async def test_a_skill_turn_parked_on_a_card_ends_on_the_stop(monkeypatch, caplog) -> None:
    """AC1. RED: nothing withdrew the runner or cancelled the task — 24h wait."""

    async def parked(_config, _ctx):
        await asyncio.sleep(3600)  # a card nobody will answer; the pane is gone

    runner, task = await _skill_turn(monkeypatch, parked)
    with caplog.at_level(logging.INFO):
        ended = await _stop(_chat())
    assert ended is True
    assert runner.stopped is True
    assert task.done()
    assert "outcome=stopped" in caplog.text


async def test_a_skill_turn_not_on_a_card_ends_as_stopped(monkeypatch, caplog) -> None:
    """AC2: the runner sees the withdraw on its next poll — #909's ``Stopped by user``."""
    from c_lord.cogs._run_helper import STOPPED_BY_USER_ERROR

    async def polling(config, _ctx):
        while not config.runner.stopped:
            await asyncio.sleep(0.01)
        config.outcome.error = STOPPED_BY_USER_ERROR
        return None

    runner, task = await _skill_turn(monkeypatch, polling)
    with caplog.at_level(logging.INFO):
        await _stop(_chat())
    assert runner.stopped is True
    assert task.done() and not task.cancelled()
    assert "outcome=stopped" in caplog.text


async def test_nothing_running_is_nothing_ended() -> None:
    chat = _chat()
    assert chat.end_turn_for_workspace_stop(THREAD + 1) is False
    await asyncio.wait_for(chat.wait_for_turn_end(THREAD + 1, timeout=0.05), 1.0)


@pytest.fixture(autouse=True)
def _ledger():
    yield
    _run_helper._IN_FLIGHT.pop(THREAD, None)
