"""An interrupted turn must still start the new one (#565).

Production symptom: a reply arrives while a turn is in flight, Discord shows
``⚡ Interrupted. Starting with new instruction...``, the prior turn ends — and
then nothing.  No ``run_claude: enter`` in the journal, no traceback, no
reaction on the message, no error in the thread.  The message the user typed is
simply gone, and the thread stays silent until the bot is restarted.

The mechanism, reproduced deterministically below:

``_drain_thread_task`` cancels a prior turn that will not wind down on its own
(one parked on a bridged menu ignores the interrupt), then does::

    with contextlib.suppress(Exception):
        await task

``asyncio.CancelledError`` derives from ``BaseException``, **not** ``Exception``,
so ``suppress(Exception)`` does not catch it.  Awaiting the task it just
cancelled therefore raises straight through ``_drain_thread_task`` →
``_preempt_prior_turn`` → ``_handle_thread_reply``, aborting the handler *before*
it reaches ``asyncio.create_task(self._run_claude(...))``.

That explains every part of the report: the interrupt notice is posted (it is the
first statement of ``_preempt_prior_turn``), the prior turn does end, the new
turn never starts, and nothing is logged — discord.py treats a CancelledError
escaping an event handler as ordinary cancellation, not an error.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog


def _bare_cog() -> ClaudeChatCog:
    """A Cog instance with no Discord wiring — _drain_thread_task needs none."""
    return ClaudeChatCog.__new__(ClaudeChatCog)


async def _parked_turn() -> None:
    """A turn parked on a bridged menu: never finishes on its own."""
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_cancelled_error_must_not_escape_the_drain() -> None:
    """#565: draining a stubborn turn must not blow up the caller.

    If it does, ``_handle_thread_reply`` unwinds before dispatching the new turn
    and the user's message vanishes with no trace.
    """
    task = asyncio.create_task(_parked_turn())
    await asyncio.sleep(0)

    try:
        await _bare_cog()._drain_thread_task(task, grace=0.15)
    except asyncio.CancelledError:  # pragma: no cover - this IS the bug
        pytest.fail(
            "CancelledError escaped _drain_thread_task — _handle_thread_reply "
            "would abort before starting the new turn (#565)"
        )
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


@pytest.mark.asyncio
async def test_the_new_turn_is_dispatched_after_a_stubborn_interrupt() -> None:
    """End-to-end shape of the bug: the statement after the drain must run.

    Mirrors ``_handle_thread_reply``: pre-empt the prior turn, then dispatch the
    new one. In production the dispatch is
    ``asyncio.create_task(self._run_claude(...))`` — the line that never ran.
    """
    dispatched: list[str] = []
    task = asyncio.create_task(_parked_turn())
    await asyncio.sleep(0)

    async def handle_reply() -> None:
        await _bare_cog()._drain_thread_task(task, grace=0.15)
        dispatched.append("run_claude")  # the line #565 never reached

    try:
        await handle_reply()
    except asyncio.CancelledError:  # pragma: no cover - this IS the bug
        pass
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    assert dispatched == ["run_claude"], "the interrupted reply must still start a new turn (#565)"


@pytest.mark.asyncio
async def test_drain_still_ends_the_prior_turn() -> None:
    """Swallowing the CancelledError must not leave the old turn running."""
    task = asyncio.create_task(_parked_turn())
    await asyncio.sleep(0)

    await _bare_cog()._drain_thread_task(task, grace=0.15)

    assert task.done(), "the prior turn must be finished before the new one starts"


@pytest.mark.asyncio
async def test_a_turn_that_winds_down_on_its_own_is_not_cancelled() -> None:
    """The graceful path must keep working: no cancel when the turn ends itself."""
    finished: list[str] = []

    async def _winds_down() -> None:
        await asyncio.sleep(0.01)
        finished.append("done")

    task = asyncio.create_task(_winds_down())
    await _bare_cog()._drain_thread_task(task, grace=1.0)

    assert finished == ["done"], "a turn that ends on its own must not be cancelled"
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_drain_survives_a_prior_turn_that_raised() -> None:
    """A prior turn that died of its own exception must not break the new one."""

    async def _explodes() -> None:
        raise RuntimeError("prior turn blew up")

    task = asyncio.create_task(_explodes())
    await asyncio.sleep(0)

    await _bare_cog()._drain_thread_task(task, grace=0.5)
    assert task.done()


@pytest.mark.asyncio
async def test_drain_handles_a_task_cancelled_before_we_got_there() -> None:
    """A task cancelled elsewhere must also be handled.

    ``wait_for`` re-raises that CancelledError from the *first* await, which the
    ``except (TimeoutError, ...)`` / ``except Exception`` pair does not catch
    either — the same escape, one line earlier.
    """
    task = asyncio.create_task(_parked_turn())
    await asyncio.sleep(0)
    task.cancel()

    await _bare_cog()._drain_thread_task(task, grace=0.5)
    assert task.done()


# --------------------------------------------------------------------------
# AC1 — a turn that dies before it logs must not vanish
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_task_that_raises_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """``_active_tasks`` pins the task, so Python never reports it itself.

    An un-awaited task's exception is only surfaced when the task is garbage
    collected, and the registry keeps a strong reference — so without an explicit
    done-callback a turn that crashes before its first log line is silent
    everywhere. That silence is what made #565 impossible to place.
    """

    async def _dies() -> None:
        raise RuntimeError("turn blew up before logging anything")

    task = asyncio.create_task(_dies())
    with contextlib.suppress(BaseException):
        await task

    with caplog.at_level("ERROR", logger="c_lord.cogs.claude_chat"):
        ClaudeChatCog._report_turn_task_outcome(4242, task)

    assert any("turn task died" in r.message for r in caplog.records), (
        "a turn that dies before logging must still produce a log line (#565)"
    )
    assert any("thread=4242" in r.getMessage() for r in caplog.records), (
        "the line must carry log_ctx(thread_id=…) so it is greppable per thread"
    )


@pytest.mark.asyncio
async def test_a_cancelled_turn_task_is_not_reported_as_an_error() -> None:
    """Cancellation is how _preempt_prior_turn tears a turn down — not an error."""
    task = asyncio.create_task(_parked_turn())
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(BaseException):
        await task

    # Must not raise (task.exception() on a cancelled task would).
    ClaudeChatCog._report_turn_task_outcome(4242, task)
