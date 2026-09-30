"""#718: a lamp orphaned by a bot restart mid-turn is taken back on startup.

The per-turn lamp lives in the process. A restart while a turn is running kills
its StatusManager, and whatever it last painted stays on the message forever —
all six "⚠️ only" lamps measured after #727 were exactly this: the thread's
turn was still running when the bot restarted (usually its own deploy).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from c_lord.discord_ui.lamp_recovery import adopt_orphaned_lamp
from c_lord.discord_ui.status import (
    EMOJI_ERROR,
    EMOJI_RUNNING,
    EMOJI_STALL_HARD,
    EMOJI_WAITING,
    StatusManager,
)
from c_lord.turn_activity import turn_activity
from c_lord.turn_end_bus import turn_end_bus


class _Message:
    """Reactions as Discord holds them: ``reactions`` is what the last run left."""

    def __init__(self, *emoji: str, mine: bool = True) -> None:
        self._emoji = list(emoji)
        self._mine = mine
        self.guild = MagicMock()

    @property
    def reactions(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(emoji=e, me=self._mine) for e in self._emoji]

    @property
    def lamps(self) -> list[str]:
        return list(self._emoji)

    async def add_reaction(self, emoji: str) -> None:
        if emoji not in self._emoji:
            self._emoji.append(emoji)
        await asyncio.sleep(0)

    async def remove_reaction(self, emoji: str, member: object) -> None:
        if emoji in self._emoji:
            self._emoji.remove(emoji)
        await asyncio.sleep(0)


def _running(value: bool):
    async def check() -> bool:
        return value

    return check


FAST = {"poll_seconds": 0.02, "recheck_seconds": 0.05}


@pytest.mark.asyncio
async def test_a_finished_turn_left_on_warning_ends_on_waiting() -> None:
    """The measured #718 case: ⚠️ left behind, the turn has since finished."""
    msg = _Message(EMOJI_STALL_HARD)
    await adopt_orphaned_lamp(msg, thread_id=7180, turn_running=_running(False), **FAST)
    assert msg.lamps == [EMOJI_WAITING]


@pytest.mark.asyncio
async def test_a_finished_turn_left_on_green_ends_on_waiting() -> None:
    """After #769 the orphan is 🟢 rather than ⚠️ — same bug, same cure."""
    msg = _Message(EMOJI_RUNNING)
    await adopt_orphaned_lamp(msg, thread_id=7181, turn_running=_running(False), **FAST)
    assert msg.lamps == [EMOJI_WAITING]


@pytest.mark.asyncio
@pytest.mark.parametrize("final", [EMOJI_WAITING, EMOJI_ERROR])
async def test_a_final_lamp_is_left_alone(final: str) -> None:
    msg = _Message(final)
    await adopt_orphaned_lamp(msg, thread_id=7182, turn_running=_running(True), **FAST)
    assert msg.lamps == [final]


@pytest.mark.asyncio
async def test_someone_elses_reaction_is_not_a_lamp() -> None:
    msg = _Message(EMOJI_STALL_HARD, mine=False)
    await adopt_orphaned_lamp(msg, thread_id=7183, turn_running=_running(False), **FAST)
    assert msg.lamps == [EMOJI_STALL_HARD]


@pytest.mark.asyncio
async def test_a_still_running_turn_goes_green_then_waiting_at_its_end() -> None:
    msg = _Message(EMOJI_STALL_HARD)
    task = asyncio.create_task(
        adopt_orphaned_lamp(msg, thread_id=7184, turn_running=_running(True), **FAST)
    )
    await asyncio.sleep(0.1)
    assert msg.lamps == [EMOJI_RUNNING]  # Claude is still working after the restart
    turn_end_bus.mark(7184)  # the mirror read the turn-end marker
    await asyncio.wait_for(task, 1)
    assert msg.lamps == [EMOJI_WAITING]


@pytest.mark.asyncio
async def test_a_turn_that_ends_without_a_marker_is_caught_by_the_recheck() -> None:
    state = {"running": True}

    async def check() -> bool:
        return state["running"]

    msg = _Message(EMOJI_RUNNING)
    task = asyncio.create_task(adopt_orphaned_lamp(msg, thread_id=7185, turn_running=check, **FAST))
    await asyncio.sleep(0.1)
    assert msg.lamps == [EMOJI_RUNNING]
    state["running"] = False
    await asyncio.wait_for(task, 1)
    assert msg.lamps == [EMOJI_WAITING]


@pytest.mark.asyncio
async def test_a_new_turn_in_the_thread_retires_the_adopted_lamp() -> None:
    """The next message's turn owns the thread now; the old one reads 🟡."""
    msg = _Message(EMOJI_RUNNING)
    task = asyncio.create_task(
        adopt_orphaned_lamp(msg, thread_id=7186, turn_running=_running(True), **FAST)
    )
    await asyncio.sleep(0.1)
    new = StatusManager(_Message(), thread_id=7186)
    await new.set_running()
    try:
        await asyncio.wait_for(task, 1)
        assert msg.lamps == [EMOJI_WAITING]
        turn_activity.note(7186)  # the new turn still hears its activity
    finally:
        await new.cleanup()


@pytest.mark.asyncio
async def test_a_lamp_that_cannot_be_read_is_skipped() -> None:
    msg = MagicMock()
    type(msg).reactions = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    await adopt_orphaned_lamp(msg, thread_id=7187, turn_running=_running(False), **FAST)
