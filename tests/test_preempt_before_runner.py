"""A turn interrupted before its runner even exists must not run (#800).

``_preempt_prior_turn`` interrupts the prior turn's runner — but a turn that
is still cloning, waiting for a session slot or creating its tmux window has no
runner registered yet. The interrupt then had nothing to stop, and the prior
turn could reach ``runner.run()`` within the 5 s drain grace and type the
request the user had just cancelled. The preempted task is remembered, and the
runner it registers later starts out stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner
from c_lord.cogs.claude_chat import ClaudeChatCog


def _cog() -> ClaudeChatCog:
    cog = ClaudeChatCog.__new__(ClaudeChatCog)
    cog._active_runners = {}
    cog._preempted_tasks = weakref.WeakSet()
    return cog


def _runner() -> TmuxClaudeRunner:
    mgr = MagicMock()
    mgr.session_name = "clord"
    return TmuxClaudeRunner(tmux_manager=mgr, thread_id=42)


@pytest.mark.asyncio
async def test_runner_registered_after_preempt_starts_stopped() -> None:
    cog = _cog()
    thread = MagicMock(id=42)
    thread.send = AsyncMock()
    registered: list[TmuxClaudeRunner] = []

    async def preparing_turn() -> None:
        await asyncio.sleep(0.05)  # still cloning when the next message lands
        runner = _runner()
        cog._register_runner(42, runner, asyncio.current_task())
        registered.append(runner)

    task = asyncio.create_task(preparing_turn())
    await asyncio.sleep(0)
    try:
        await cog._preempt_prior_turn(thread, task, None)
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    assert len(registered) == 1
    runner = registered[0]
    assert runner.stopped and runner.preempted
    runner._tmux.send_interrupt.assert_not_called()  # no stray C-c to the pane


def test_runner_of_a_turn_nobody_preempted_is_not_stopped() -> None:
    cog = _cog()
    runner = _runner()
    cog._register_runner(42, runner, None)
    assert cog._active_runners[42] is runner
    assert not runner.stopped
