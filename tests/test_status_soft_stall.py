"""The ⏳ half of the stall lamp — 10 seconds of silence (#769).

``docs/specs/thread-lamp.md`` gives the lamp two warning steps: ⏳ after 10s
with nothing visible from Claude, ⚠️ after 30s. ``tests/test_status.py`` pins
the ⚠️ step; the ⏳ step had no test of its own. #769 is the issue that made
both of them mean *silence* rather than *time since the turn started*:
"⏳ も同じで、10 秒で必ず付きます" was half of that report, and the fix's own
comment says that after Claude moves again "the next silence may warn again".
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.discord_ui.status import (
    EMOJI_RUNNING,
    EMOJI_STALL_HARD,
    EMOJI_STALL_SOFT,
    EMOJI_WAITING,
    STALL_HARD_SECONDS,
    STALL_SOFT_SECONDS,
    StatusManager,
)
from c_lord.turn_activity import turn_activity


def _make_message() -> MagicMock:
    msg = MagicMock()
    msg.add_reaction = AsyncMock()
    msg.remove_reaction = AsyncMock()
    msg.guild = MagicMock()
    msg.guild.me = MagicMock()
    return msg


def _silent_for(sm: StatusManager, seconds: float) -> None:
    sm._last_activity = asyncio.get_running_loop().time() - seconds


def _painted(msg: MagicMock, emoji: str) -> int:
    return sum(1 for c in msg.add_reaction.await_args_list if c.args[0] == emoji)


@pytest.mark.asyncio
async def test_ten_seconds_of_silence_shows_the_hourglass() -> None:
    """#769: ⏳ = "10 秒 動きなし" — not ⚠️ yet, and not 🟢 any more."""
    msg = _make_message()
    sm = StatusManager(msg)
    await sm.set_running()
    _silent_for(sm, STALL_SOFT_SECONDS + 5)
    await asyncio.sleep(2.5)
    assert sm._current_emoji == EMOJI_STALL_SOFT
    assert _painted(msg, EMOJI_STALL_HARD) == 0
    await sm.cleanup()


@pytest.mark.asyncio
async def test_the_hourglass_is_painted_once_per_silence() -> None:
    """Staying silent between 10s and 30s must not re-add ⏳ on every 2s tick."""
    msg = _make_message()
    sm = StatusManager(msg)
    await sm.set_running()
    _silent_for(sm, STALL_SOFT_SECONDS + 1)
    await asyncio.sleep(4.5)  # two monitor ticks, still under 30s of silence
    assert sm._current_emoji == EMOJI_STALL_SOFT
    assert _painted(msg, EMOJI_STALL_SOFT) == 1
    await sm.cleanup()


@pytest.mark.asyncio
async def test_a_silence_that_goes_on_moves_from_hourglass_to_warning() -> None:
    msg = _make_message()
    sm = StatusManager(msg)
    await sm.set_running()
    _silent_for(sm, STALL_SOFT_SECONDS + 1)
    await asyncio.sleep(2.5)
    assert sm._current_emoji == EMOJI_STALL_SOFT
    _silent_for(sm, STALL_HARD_SECONDS + 1)
    await asyncio.sleep(2.2)
    assert sm._current_emoji == EMOJI_STALL_HARD
    await sm.cleanup()


@pytest.mark.asyncio
async def test_activity_after_the_hourglass_brings_back_green_and_rearms_it() -> None:
    """#769: Claude moved again → 🟢, and the *next* silence warns again."""
    msg = _make_message()
    sm = StatusManager(msg, thread_id=7691)
    await sm.set_running()
    _silent_for(sm, STALL_SOFT_SECONDS + 1)
    await asyncio.sleep(2.5)
    assert sm._current_emoji == EMOJI_STALL_SOFT

    turn_activity.note(7691)  # the transcript mirror read an event
    await asyncio.sleep(2.2)
    assert sm._current_emoji == EMOJI_RUNNING

    _silent_for(sm, STALL_SOFT_SECONDS + 1)
    await asyncio.sleep(2.2)
    assert sm._current_emoji == EMOJI_STALL_SOFT
    assert _painted(msg, EMOJI_STALL_SOFT) == 2
    await sm.cleanup()


@pytest.mark.asyncio
async def test_a_turn_that_ends_under_the_hourglass_reads_waiting_only() -> None:
    """#718: the override is temporary — the finished turn shows 🟡 alone."""
    msg = _make_message()
    sm = StatusManager(msg)
    await sm.set_running()
    _silent_for(sm, STALL_SOFT_SECONDS + 1)
    await asyncio.sleep(2.5)
    assert sm._current_emoji == EMOJI_STALL_SOFT
    await sm.set_waiting()
    assert sm._current_emoji == EMOJI_WAITING
    removed = [c.args[0] for c in msg.remove_reaction.await_args_list]
    assert removed[-1] == EMOJI_STALL_SOFT


@pytest.mark.asyncio
async def test_a_lamp_without_a_thread_is_never_displaced() -> None:
    """#718: ``displaced`` needs the transcript feed; an older caller has none."""
    sm = StatusManager(_make_message())
    await sm.set_running()
    assert sm.displaced is False
    await sm.cleanup()
