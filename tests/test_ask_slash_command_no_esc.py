"""Review #1051: /compact typed while the turn is parked on a question card.

_deliver_slash_command interrupts the running turn with interrupt(silent=True)
— which marks the runner `preempted` — and posts NO empty answer. With #1051
the bridge's stop watcher now reads that as a pre-emption and Esc's the menu,
on its 2 s poll, i.e. after the slash command has already been typed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.cogs import claude_chat as cc
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask
from tests.test_stop_parked_ask import THREAD, _question, _runner, _thread


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    ask_bus.unregister(THREAD)


async def test_no_esc_reaches_the_pane_after_compact_was_typed(monkeypatch) -> None:
    pane: list[str] = []
    q = _question()
    runner = _runner(q)
    runner.peek_menu_state = AsyncMock(return_value=(q, True))
    runner.cancel_menu = AsyncMock(side_effect=lambda: pane.append("Escape"))

    async def interrupt(*, silent: bool = False) -> bool:
        pane.append("C-c")
        runner.stopped = True
        runner.preempted = silent
        runner.peek_menu_state = AsyncMock(return_value=(None, True))  # C-c closed it
        return True

    runner.interrupt = interrupt
    thread, _msg = _thread()
    bridge = asyncio.create_task(bridge_pane_ask(thread, q, runner))  # real 2 s poll
    await asyncio.sleep(0.3)  # parked on the card

    monkeypatch.setattr(cc, "wait_for_idle_prompt", AsyncMock(return_value=True))
    bot = MagicMock()
    bot.channel_id = 999
    chat = cc.ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
    chat._active_runners[THREAD] = runner
    tmux = MagicMock()
    tmux.send_literal = MagicMock(side_effect=lambda _t, c: pane.append(c) or True)
    tmux.send_keys = MagicMock(side_effect=lambda _t, k: pane.append(k) or True)
    assert await chat._deliver_slash_command(thread, tmux, "/compact") is None
    await asyncio.wait_for(bridge, 5.0)
    print("pane keystrokes:", pane, "closed:", ask_bus.closed_reason(THREAD))
    typed = pane.index("/compact")
    assert "Escape" not in pane[typed:], f"Esc sent after /compact was typed: {pane}"
