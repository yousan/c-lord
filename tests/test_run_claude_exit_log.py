"""Every ``run_claude: enter`` gets a ``run_claude: exit`` (#293).

The pair is how a stuck turn is told apart from a finished one:
``grep "thread=<id>"`` showing an ``enter`` with no ``exit`` is supposed to mean
"this turn is still running".  The ``exit`` line used to sit at the very end of
``run_claude_with_config``, *outside* its ``finally``, so three ordinary ways of
leaving the function skipped it:

* a turn cancelled by the user's next message (#315's ``task.cancel()`` — the
  ``CancelledError`` is not an ``Exception`` and bypassed the handler),
* the runner raising (the ``except`` block ``return``-ed before the log line),
* the AskUserQuestion answer resuming in a nested call (``return await ...``
  jumped over the outer turn's line).

Production had 18 such unpaired ``enter`` lines from the cancel path alone
(2026-09-01 … 09-23), which made every one of them look like a hang.

The second half is orphan detection: a new turn that starts while an older run
for the same thread is still in flight says so, with how long the older one has
been running — that is the only situation left in which an ``exit`` can be
missing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.claude.types import AskOption, AskQuestion, MessageType, StreamEvent
from c_lord.cogs import _run_helper
from c_lord.cogs._run_helper import run_claude_with_config
from c_lord.cogs.run_config import RunConfig

_LOGGER = "c_lord.cogs._run_helper"


def _thread(thread_id: int = 4242) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return t


def _config(runner: MagicMock, thread: MagicMock | None = None) -> RunConfig:
    return RunConfig(
        thread=thread or _thread(),
        runner=runner,
        prompt="hello",
        session_id=None,
        repo=None,
    )


def _runner(*turns: list[StreamEvent]) -> MagicMock:
    """A runner whose successive ``run()`` calls yield *turns* in order."""
    runner = MagicMock()
    runner.preempted = False
    runner.interrupt = AsyncMock()
    pending = list(turns)

    def run(*args, **kwargs):
        events = pending.pop(0)

        async def gen():
            for event in events:
                yield event

        return gen()

    runner.run = run
    return runner


def _started() -> StreamEvent:
    return StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-1")


def _done(error: str | None = None) -> StreamEvent:
    return StreamEvent(
        message_type=MessageType.RESULT, is_complete=True, session_id="sess-1", error=error
    )


def _lines(caplog: pytest.LogCaptureFixture, what: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and f"run_claude: {what}" in r.getMessage()
    ]


def _outcome(line: str) -> str:
    match = re.search(r"outcome=(\w+)", line)
    assert match, f"exit line does not say how the turn ended: {line!r}"
    return match.group(1)


@pytest.fixture(autouse=True)
def _info_logs(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    yield
    getattr(_run_helper, "_IN_FLIGHT", {}).clear()


@pytest.mark.asyncio
async def test_normal_turn_logs_exit_ok(caplog) -> None:
    await run_claude_with_config(_config(_runner([_done()])))

    exits = _lines(caplog, "exit")
    assert len(_lines(caplog, "enter")) == 1
    assert len(exits) == 1
    assert _outcome(exits[0]) == "ok"


@pytest.mark.asyncio
async def test_errored_turn_logs_exit_error(caplog) -> None:
    await run_claude_with_config(_config(_runner([_done(error="boom")])))

    exits = _lines(caplog, "exit")
    assert len(exits) == 1
    assert _outcome(exits[0]) == "error"


@pytest.mark.asyncio
async def test_runner_exception_still_logs_exit(caplog) -> None:
    """RED: the ``except`` block returned before the exit line."""
    runner = _runner()

    def explode(*args, **kwargs):
        async def gen():
            raise RuntimeError("tmux went away")
            yield  # pragma: no cover

        return gen()

    runner.run = explode

    await run_claude_with_config(_config(runner))

    exits = _lines(caplog, "exit")
    assert len(exits) == 1, "a runner that raised must still close its enter line"
    assert _outcome(exits[0]) == "error"


@pytest.mark.asyncio
async def test_cancelled_turn_logs_exit_and_stays_cancelled(caplog) -> None:
    """RED: the #315 interrupt cancels the task — the most common orphan in prod."""
    started = asyncio.Event()
    runner = _runner()

    def parked(*args, **kwargs):
        async def gen():
            started.set()
            await asyncio.sleep(3600)
            yield  # pragma: no cover

        return gen()

    runner.run = parked

    task = asyncio.create_task(run_claude_with_config(_config(runner)))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    exits = _lines(caplog, "exit")
    assert len(exits) == 1, "a cancelled turn must still close its enter line"
    assert _outcome(exits[0]) == "cancelled"


@pytest.mark.asyncio
async def test_preempted_turn_says_so(caplog) -> None:
    """A turn wound down by the next message is not "ok" — say what ended it."""
    runner = _runner([_done()])
    runner.preempted = True

    await run_claude_with_config(_config(runner))

    assert _outcome(_lines(caplog, "exit")[0]) == "preempted"


@pytest.mark.asyncio
async def test_ask_resume_closes_both_turns(caplog) -> None:
    """RED: the answer resumes in a nested call whose ``return await`` skipped
    the outer turn's exit line."""
    question = AskQuestion(question="Which?", options=[AskOption("A"), AskOption("B")])
    asking = StreamEvent(
        message_type=MessageType.ASSISTANT, session_id="sess-1", ask_questions=[question]
    )
    runner = _runner([_started(), asking], [_done()])

    with patch(
        "c_lord.cogs._run_helper.collect_ask_answers", AsyncMock(return_value="A")
    ) as collect:
        await run_claude_with_config(_config(runner))

    collect.assert_awaited_once()
    assert len(_lines(caplog, "enter")) == 2
    assert len(_lines(caplog, "exit")) == 2


@pytest.mark.asyncio
async def test_nothing_left_in_flight_after_any_exit(caplog) -> None:
    await run_claude_with_config(_config(_runner([_done()])))
    assert not _run_helper._IN_FLIGHT


# -- orphan detection -----------------------------------------------------------


def _orphan_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == logging.WARNING and "orphan" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_new_turn_reports_a_run_still_in_flight(caplog) -> None:
    """A second run for a thread whose first run never exited is named."""
    started = asyncio.Event()
    stuck = _runner()

    def parked(*args, **kwargs):
        async def gen():
            started.set()
            await asyncio.sleep(3600)
            yield  # pragma: no cover

        return gen()

    stuck.run = parked
    thread = _thread()
    first = asyncio.create_task(run_claude_with_config(_config(stuck, thread)))
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        await run_claude_with_config(_config(_runner([_done()]), thread))
    finally:
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first

    warnings = _orphan_warnings(caplog)
    assert len(warnings) == 1
    assert f"thread={thread.id}" in warnings[0]


@pytest.mark.asyncio
async def test_other_threads_are_not_orphans(caplog) -> None:
    started = asyncio.Event()
    stuck = _runner()

    def parked(*args, **kwargs):
        async def gen():
            started.set()
            await asyncio.sleep(3600)
            yield  # pragma: no cover

        return gen()

    stuck.run = parked
    first = asyncio.create_task(run_claude_with_config(_config(stuck, _thread(1))))
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        await run_claude_with_config(_config(_runner([_done()]), _thread(2)))
    finally:
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first

    assert _orphan_warnings(caplog) == []


@pytest.mark.asyncio
async def test_ask_resume_is_not_an_orphan(caplog) -> None:
    """The nested resume runs inside its own turn — that is not a second run."""
    question = AskQuestion(question="Which?", options=[AskOption("A"), AskOption("B")])
    asking = StreamEvent(
        message_type=MessageType.ASSISTANT, session_id="sess-1", ask_questions=[question]
    )
    with patch("c_lord.cogs._run_helper.collect_ask_answers", AsyncMock(return_value="A")):
        await run_claude_with_config(_config(_runner([_started(), asking], [_done()])))

    assert _orphan_warnings(caplog) == []
