"""#1040: an interrupt that lands before the card's bridge registered.

``_preempt_prior_turn`` interrupts the runner and posts an empty answer. When
the runner has found the menu but the bridge has not registered yet, that empty
answer has no receiver and is dropped — and since #1007 a pre-empted runner no
longer ends the wait as a stop, so the card that is posted afterwards stayed
clickable (and a 5 s drain cancel left it so for good).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask
from tests.test_stop_parked_ask import THREAD, _question, _runner, _thread


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    yield
    ask_bus.unregister(THREAD)


def _closed(msg) -> list[dict]:
    return [c.kwargs for c in msg.edit.await_args_list if c.kwargs.get("view", 0) is None]


async def test_preempt_lands_before_register_card_is_not_left_live() -> None:
    """The issue's repro: the menu is still in the pane, the empty answer is lost."""
    q = _question()
    runner = _runner(q)
    runner.peek_menu_state = AsyncMock(return_value=(q, True))
    thread, msg = _thread()
    runner.stopped = True
    runner.preempted = True
    assert ask_bus.post_answer(THREAD, []) is False  # no waiter yet — dropped
    task = asyncio.create_task(bridge_pane_ask(thread, q, runner))
    try:
        await asyncio.wait_for(asyncio.shield(task), 0.5)
    except asyncio.TimeoutError:
        task.cancel()  # what _drain_thread_task does after 5s
        await asyncio.gather(task, return_exceptions=True)
    closed = _closed(msg)
    assert closed, f"card left with live buttons; closed_reason={ask_bus.closed_reason(THREAD)!r}"
    assert "新しい指示" in (closed[-1].get("content") or "")
    assert ask_bus.closed_reason(THREAD) == "interrupted"
    runner.cancel_menu.assert_awaited()  # the menu in the pane is Esc'd too

