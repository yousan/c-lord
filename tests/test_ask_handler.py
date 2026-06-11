"""Tests for bridge_pane_ask — the in-pane AskUserQuestion → Discord bridge.

#359: a menu can be answered/cancelled directly in the tmux pane (the human
attaches and types).  When that happens the bridge must stop waiting for a
Discord click — otherwise the suspended ``run_claude`` holds the thread lock
for up to 24h and the whole thread freezes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.discord_ui import ask_handler
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask


def _question() -> AskQuestion:
    return AskQuestion(
        question="repro?",
        header="テスト",
        options=[AskOption("A1", "one"), AskOption("A2", "two"), AskOption("A3", "three")],
    )


def _thread(thread_id: int) -> tuple[MagicMock, MagicMock]:
    thread = MagicMock()
    thread.id = thread_id
    msg = MagicMock()
    msg.edit = AsyncMock()
    thread.send = AsyncMock(return_value=msg)
    return thread, msg


@pytest.mark.asyncio
async def test_tui_resolution_unblocks_bridge(monkeypatch):
    """A menu resolved in the TUI (no Discord click) must NOT hang the bridge (#359).

    Before the fix bridge_pane_ask awaited the click for 24h, suspending
    run_claude and freezing the thread.  Now it watches the pane: once the menu
    is gone it returns, disables the stale buttons, and does NOT send keystrokes.
    """
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, msg = _thread(359_0001)
    runner = MagicMock()
    # Menu is open on the first peek, then gone (answered/cancelled in the TUI).
    runner.peek_pending_ask = AsyncMock(side_effect=[_question(), None, None, None, None])
    runner.answer_menu = AsyncMock()
    runner.answer_menu_text = AsyncMock()
    runner.cancel_menu = AsyncMock()

    try:
        await asyncio.wait_for(bridge_pane_ask(thread, _question(), runner), timeout=3.0)
    except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
        pytest.fail("bridge_pane_ask hung — TUI resolution did not unblock it (#359)")

    # No keystrokes sent (the menu was already answered in the TUI).
    runner.answer_menu.assert_not_called()
    runner.answer_menu_text.assert_not_called()
    runner.cancel_menu.assert_not_called()
    # Stale buttons disabled.
    msg.edit.assert_awaited()
    # Bus is cleaned up so the next menu in this thread can register.
    assert not ask_bus.is_active(thread.id)


@pytest.mark.asyncio
async def test_discord_click_still_answers_menu(monkeypatch):
    """Regression: a real Discord click still drives the TUI menu via answer_menu."""
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    thread, _msg = _thread(359_0002)
    runner = MagicMock()
    # Menu stays open throughout — the click should win the race.
    runner.peek_pending_ask = AsyncMock(return_value=_question())
    runner.answer_menu = AsyncMock()
    runner.answer_menu_text = AsyncMock()
    runner.cancel_menu = AsyncMock()

    async def _click_soon():
        await asyncio.sleep(0.05)
        ask_bus.post_answer(thread.id, ["A2"])

    await asyncio.gather(
        asyncio.wait_for(bridge_pane_ask(thread, _question(), runner), timeout=3.0),
        _click_soon(),
    )
    # Option index 1 (A2) selected via keystrokes.
    runner.answer_menu.assert_awaited_once_with(1)
    runner.cancel_menu.assert_not_called()
