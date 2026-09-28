"""``wait_for_idle_prompt`` — "has the interrupted turn let go of the pane yet" (#803)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c_lord.claude.tmux_runner import wait_for_idle_prompt

_IDLE = "────\n❯ \n────\n  ⏵⏵ bypass permissions on"
_BUSY = "✻ Thinking… (3s · esc to interrupt)\n────\n❯ \n────"


@pytest.mark.asyncio
async def test_true_once_the_pane_is_idle() -> None:
    tmux = MagicMock()
    tmux.capture_pane = MagicMock(side_effect=[_BUSY, _BUSY, _IDLE])
    tmux.is_claude_running = MagicMock(return_value=True)

    assert await wait_for_idle_prompt(tmux, 1, timeout=1.0, interval=0.01) is True
    assert tmux.capture_pane.call_count == 3


@pytest.mark.asyncio
async def test_false_when_it_stays_busy() -> None:
    tmux = MagicMock()
    tmux.capture_pane = MagicMock(return_value=_BUSY)
    tmux.is_claude_running = MagicMock(return_value=True)

    assert await wait_for_idle_prompt(tmux, 1, timeout=0.05, interval=0.01) is False


@pytest.mark.asyncio
async def test_a_dead_pane_is_not_idle() -> None:
    """A shell's ``❯`` after Claude exited must not read as Claude's input box."""
    tmux = MagicMock()
    tmux.capture_pane = MagicMock(return_value=_IDLE)
    tmux.is_claude_running = MagicMock(return_value=False)

    assert await wait_for_idle_prompt(tmux, 1, timeout=0.05, interval=0.01) is False
