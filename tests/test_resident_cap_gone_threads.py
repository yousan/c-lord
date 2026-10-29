"""#1086: a thread deleted from Discord must not hold a resident slot forever.

Its window keeps running claude, so it counts as resident; the cap cannot sleep
it (sleeping goes through the Discord thread) and the #570 reaper leaves it
alone. With as many deleted threads as the limit, every live idle workspace was
slept on every tick instead. Now the cap closes the windows of threads Discord
answers **NotFound** for. **Forbidden** may be a permission that comes back, so
those windows are left alone (only skipped, as before).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.resident_cap import ResidentCapLoop
from c_lord.tmux import PaneTurnStates
from tests.test_resident_cap_failure_paths import OK, _bot, _cog, _rec, _repo, _slept

NONE = PaneTurnStates(frozenset(), frozenset())


def _not_found() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404), "gone")


def _forbidden() -> discord.Forbidden:
    return discord.Forbidden(MagicMock(status=403), "no access")


def _loop(resident: set[int], cog: MagicMock, fetch_error: Exception) -> ResidentCapLoop:
    bot = _bot(cog=cog, channels={1: MagicMock(id=1), 2: MagicMock(id=2), 3: MagicMock(id=3)})
    bot.fetch_channel = AsyncMock(side_effect=fetch_error)
    return ResidentCapLoop(
        bot,
        _repo([_rec(i, hours_ago=i) for i in range(1, 6)]),
        limit=2,
        resident_fn=lambda: set(resident),
        in_flight_fn=set,
        mem_fn=lambda: OK,
    )


def _sleeping_cog(resident: set[int]) -> MagicMock:
    async def impl(*, channel: object, reason: object, idle_label: object) -> bool:
        resident.discard(channel.id)  # type: ignore[attr-defined]
        return True

    return _cog(AsyncMock(side_effect=impl))


@pytest.mark.asyncio
async def test_deleted_threads_windows_are_closed_and_only_the_excess_live_one_sleeps() -> None:
    """AC1: resident {1..5}, limit 2, 4 and 5 (the oldest) deleted in Discord."""
    resident = {1, 2, 3, 4, 5}
    cog = _sleeping_cog(resident)

    def close(thread_id: int) -> int:
        resident.discard(thread_id)
        return 1

    with (
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
        patch("c_lord.tmux.close_thread_windows", side_effect=close) as closed,
    ):
        await _loop(resident, cog, _not_found()).tick()

    assert sorted(c.args[0] for c in closed.call_args_list) == [4, 5]
    assert _slept(cog) == [3]
    assert resident == {1, 2}


@pytest.mark.asyncio
async def test_known_deleted_thread_is_closed_on_a_later_tick_too() -> None:
    """A close that did not take (tmux hiccup) is retried, not forgotten."""
    resident = {1, 2, 3, 4, 5}
    cog = _sleeping_cog(resident)
    attempts: list[int] = []

    def close(thread_id: int) -> int:
        attempts.append(thread_id)
        if attempts.count(thread_id) == 1:
            return 0  # first try fails
        resident.discard(thread_id)
        return 1

    loop = _loop(resident, cog, _not_found())
    with (
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
        patch("c_lord.tmux.close_thread_windows", side_effect=close),
    ):
        await loop.tick()
        await loop.tick()

    assert 4 not in resident and 5 not in resident
    assert {1, 2} <= resident


@pytest.mark.asyncio
async def test_forbidden_thread_window_is_not_closed() -> None:
    """AC2: Forbidden may be a permission that comes back — skip, never close."""
    resident = {1, 2, 3, 4, 5}
    cog = _sleeping_cog(resident)

    with (
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
        patch("c_lord.tmux.close_thread_windows", return_value=1) as closed,
    ):
        await _loop(resident, cog, _forbidden()).tick()

    closed.assert_not_called()
    assert 4 in resident and 5 in resident


@pytest.mark.asyncio
async def test_a_deleted_thread_with_a_running_turn_is_not_closed() -> None:
    """In-flight work is never cut short, by sleep or by close."""
    resident = {1, 2, 3, 4, 5}
    cog = _sleeping_cog(resident)
    loop = _loop(resident, cog, _not_found())
    loop._in_flight_fn = lambda: {4, 5}  # type: ignore[assignment]

    with (
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
        patch("c_lord.tmux.close_thread_windows", return_value=1) as closed,
    ):
        await loop.tick()

    closed.assert_not_called()


@pytest.mark.asyncio
async def test_the_emergency_brake_closes_deleted_threads_too() -> None:
    """Found in review: under memory pressure the cap never runs, and the brake
    only skipped a deleted thread — the window held its memory all episode."""
    from tests.test_resident_cap_failure_paths import LOW

    resident = {1, 2, 3, 4, 5}
    cog = _sleeping_cog(resident)
    loop = _loop(resident, cog, _not_found())
    loop._mem_fn = lambda: LOW  # type: ignore[assignment]
    loop._low_streak = 10  # already braking

    def close(thread_id: int) -> int:
        resident.discard(thread_id)
        return 1

    with (
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
        patch("c_lord.tmux.close_thread_windows", side_effect=close) as closed,
    ):
        await loop.tick()
        await loop.tick()

    assert {c.args[0] for c in closed.call_args_list} == {4, 5}
    assert 4 not in resident and 5 not in resident
