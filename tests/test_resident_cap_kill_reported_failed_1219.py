"""#1219 — a kill that *reported* failure but did kill the window slept the whole fleet.

The resident cap protects memory, and sleeping a workspace someone is using should
happen as little as possible. Since #1192 a failed kill counts as "still there" and
the next candidate makes up for it. On a slow host ``kill-window`` can time out
while tmux still carries it out: the window is gone but the kill says it failed.
Every candidate in the tick then "failed", each was made up for by the next, and
one tick with one workspace over the limit slept all five. After a failed kill
the cap asks again with a lookup that can say "did not answer": gone only when
tmux answers that the window is nowhere.

Each patch to "make up within the tick" opened a new stuck or over-sleep case,
so the rule is now (decided 2026-11-05): every attempt in a tick counts toward
``over`` — one tick never sleeps more than the excess — and a failed victim is
tried after every other candidate on later ticks. The next tick re-counts, so a
real failed kill (#1192) is made up for then. No real tmux.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.resident_cap import ResidentCapLoop
from c_lord.tmux import PaneTurnStates
from tests.test_resident_cap_failure_paths import OK, _bot, _cog, _rec, _repo
from tests.test_resident_cap_sleep_failure_1192 import _real_impl_cog, _thread3

NONE = PaneTurnStates(frozenset(), frozenset())


def _loop(cog, resident: set[int], n: int, limit: int) -> ResidentCapLoop:  # noqa: ANN001
    return ResidentCapLoop(
        _bot(cog=cog),
        _repo([_rec(i, hours_ago=i) for i in range(1, n + 1)]),
        limit=limit,
        resident_fn=lambda: set(resident),
        in_flight_fn=set,
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )


async def test_persistent_kill_failure_does_not_sleep_more_each_tick() -> None:
    """Oldest (6) always fails to die. Limit 3, 6 resident. Over 20 ticks the cap must
    converge at 3 resident (6 + two others) and then stop sleeping."""
    resident = {1, 2, 3, 4, 5, 6}
    calls: list[int] = []

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        calls.append(channel.id)
        if channel.id == 6:
            return False
        resident.discard(channel.id)
        return True

    loop = _loop(_cog(AsyncMock(side_effect=impl)), resident, 6, 3)
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        for _ in range(20):
            await loop.tick()
    assert resident == {6, 2, 1}, resident
    assert calls == [6, 5, 4, 3], calls  # one pass, then nothing


async def test_kill_reported_failed_but_window_died_sleeps_whole_fleet_in_one_tick() -> None:
    """kill-window timed out (rc=124 -> kill_session False) but tmux executed it.
    Over is 1 (limit 4, 5 resident). W65: 「まれに1本多く」. Here every kill in the
    tick behaves that way (one stalled-but-executing tmux), and the loop keeps
    making up for each with the next candidate until the list runs out."""
    resident = {1, 2, 3, 4, 5}

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        resident.discard(channel.id)  # the window is gone ...
        return False  # ... but the kill said it failed

    loop = _loop(_cog(AsyncMock(side_effect=impl)), resident, 5, 4)
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        await loop.tick()
    assert len(resident) >= 3, f"over was 1, but one tick left {sorted(resident)} resident"


async def test_a_stuck_oldest_with_unanswered_re_reads_does_not_hold_the_cap() -> None:
    """Review of db65406: the oldest's kill always fails with the window staying and
    its re-read never answers, while the count does. Stopping at the first
    unanswered re-read picked the same victim first every tick and slept nothing
    behind it — 50 ticks later all 5 were still resident (limit 3)."""
    resident = {1, 2, 3, 4, 5}

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        if channel.id == 5:
            return False
        resident.discard(channel.id)
        return True

    loop = _production_loop(_cog(AsyncMock(side_effect=impl)), resident, 5, 3)
    with (
        patch("c_lord.tmux.resident_thread_ids", side_effect=lambda: set(resident)),
        patch("c_lord.tmux.is_resident_anywhere", return_value=None),
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
    ):
        for _ in range(50):
            await loop.tick()
    assert len(resident) <= 3, f"still {sorted(resident)} resident after 50 ticks (limit 3)"


async def test_the_unanswered_re_read_warning_is_sampled(caplog) -> None:  # noqa: ANN001
    """Review of db65406: for as long as a stall lasts the WARNING came once per tick.
    Sampled like the loop's other repeated lines ("cap-stuck", "cap-short")."""
    resident = {1, 2, 3, 4, 5}
    loop = _production_loop(_cog(AsyncMock(return_value=False)), resident, 5, 4)
    with (
        patch("c_lord.tmux.resident_thread_ids", side_effect=lambda: set(resident)),
        patch("c_lord.tmux.is_resident_anywhere", return_value=None),
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
    ):
        for _ in range(10):
            await loop.tick()
    unanswered = [r for r in caplog.records if "did not answer the re-read" in r.getMessage()]
    assert len(unanswered) <= 1, f"{len(unanswered)} WARNINGs in 10 ticks"


def _production_loop(cog, resident: set[int], n: int, limit: int) -> ResidentCapLoop:  # noqa: ANN001
    """No ``resident_fn``: the loop reads tmux itself, as in production."""
    return ResidentCapLoop(
        _bot(cog=cog),
        _repo([_rec(i, hours_ago=i) for i in range(1, n + 1)]),
        limit=limit,
        in_flight_fn=set,
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )


async def test_the_re_read_after_a_failed_kill_does_not_block_the_event_loop() -> None:
    """Review of e971435: the re-read is ``tmux -V`` plus ``list-panes`` (30 s
    timeouts each) and ran on the event loop, freezing the whole bot."""
    import asyncio
    import time

    resident = {1, 2, 3}
    beats = {"n": 0}
    seen: list[int] = []

    async def heartbeat() -> None:
        while True:
            beats["n"] += 1
            await asyncio.sleep(0.01)

    def slow_re_read(thread_id: int) -> bool:
        before = beats["n"]
        time.sleep(0.3)
        seen.append(beats["n"] - before)
        return True  # still there

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        if channel.id == 3:
            return False
        resident.discard(channel.id)
        return True

    loop = _production_loop(_cog(AsyncMock(side_effect=impl)), resident, 3, 2)
    task = asyncio.create_task(heartbeat())
    try:
        with (
            patch("c_lord.tmux.resident_thread_ids", side_effect=lambda: set(resident)),
            patch("c_lord.tmux.is_resident_anywhere", side_effect=slow_re_read),
            patch("c_lord.tmux.pane_turn_states", return_value=NONE),
        ):
            assert await loop._enforce_cap() == 0  # no make-up within the tick
    finally:
        task.cancel()
    assert seen and seen[0] >= 5, f"the event loop was blocked during the re-read ({seen})"


def _failed_kill_mgr(second_look: bool | None) -> MagicMock:
    mgr = MagicMock()
    mgr.window_present = MagicMock(side_effect=[True, second_look])
    mgr.kill_session = MagicMock(return_value=False)
    return mgr


async def test_sleep_impl_failed_kill_with_window_gone_everywhere_is_gone() -> None:
    cog = _real_impl_cog(_failed_kill_mgr(False))
    with patch("c_lord.tmux.is_resident_anywhere", return_value=False):
        out = await cog._sleep_workspace_impl(channel=_thread3(), reason=MagicMock())
    assert out is None, "tmux answered that the window is gone, but it counted as still there"


async def test_sleep_impl_failed_kill_stays_failed_unless_tmux_says_gone() -> None:
    for second_look, elsewhere in ((None, False), (True, False), (False, True), (False, None)):
        cog = _real_impl_cog(_failed_kill_mgr(second_look))
        with patch("c_lord.tmux.is_resident_anywhere", return_value=elsewhere):
            out = await cog._sleep_workspace_impl(channel=_thread3(), reason=MagicMock())
        assert out is False, (second_look, elsewhere)


async def test_two_stuck_oldest_with_unanswered_re_reads_do_not_hold_the_cap() -> None:
    """Review of f15c8ee: with the two oldest stuck (kill always fails, re-read never
    answers), the oldest used the one make-up and the next ended the tick — every
    tick, so nothing behind them was ever slept. They go to the end of the next
    tick's order, so the ones behind them get tried."""
    resident = {1, 2, 3, 4, 5, 6}

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        if channel.id in (5, 6):
            return False
        resident.discard(channel.id)
        return True

    loop = _production_loop(_cog(AsyncMock(side_effect=impl)), resident, 6, 3)
    with (
        patch("c_lord.tmux.resident_thread_ids", side_effect=lambda: set(resident)),
        patch("c_lord.tmux.is_resident_anywhere", return_value=None),
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
    ):
        for _ in range(50):
            await loop.tick()
    assert len(resident) <= 3, f"still {sorted(resident)} resident after 50 ticks (limit 3)"
    assert {5, 6} <= resident


def _attempt_counting_loop(n: int, limit: int, stuck: set[int], *, happens: bool):
    """n residents; kills of *stuck* always report failure (and are carried out
    when *happens*); every re-read is unanswered (a sustained stall)."""
    resident = set(range(1, n + 1))

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        if channel.id in stuck or happens:
            if happens:
                resident.discard(channel.id)
            return False
        resident.discard(channel.id)
        return True

    loop = _production_loop(_cog(AsyncMock(side_effect=impl)), resident, n, limit)
    return loop, resident


async def _ticks(loop, resident: set[int], limit: int, ticks: int) -> None:  # noqa: ANN001
    with (
        patch("c_lord.tmux.resident_thread_ids", side_effect=lambda: set(resident)),
        patch("c_lord.tmux.is_resident_anywhere", return_value=None),
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
    ):
        for tick in range(ticks):
            before = len(resident)
            await loop.tick()
            over = max(0, before - limit)
            assert before - len(resident) <= over, f"tick {tick}: slept more than over={over}"


@pytest.mark.parametrize(("n", "stuck"), [(8, 4), (10, 2), (10, 3), (10, 4), (10, 5)])
async def test_stuck_oldest_with_unanswered_re_reads_converge(n: int, stuck: int) -> None:
    """Review of 5641d83 / the persistent deferral: the 4 oldest of 8 always fail
    (limit 6) — the cap must still reach its limit, never sleeping more than over."""
    limit = 6
    stuck_ids = set(range(n - stuck + 1, n + 1))  # the oldest
    loop, resident = _attempt_counting_loop(n, limit, stuck_ids, happens=False)
    await _ticks(loop, resident, limit, 50)
    assert len(resident) <= limit, f"still {sorted(resident)} resident after 50 ticks"
    assert stuck_ids <= resident


async def test_kills_that_fail_but_happen_never_sleep_more_than_the_excess() -> None:
    """Every kill reports failure but is carried out, and tmux never answers the
    re-read. Per tick no more than over; overall no more than the true excess."""
    n, limit = 8, 5
    loop, resident = _attempt_counting_loop(n, limit, set(), happens=True)
    await _ticks(loop, resident, limit, 50)
    assert len(resident) == limit, f"{sorted(resident)} resident (limit {limit})"


async def test_a_real_failed_kill_is_made_up_for_on_the_next_tick() -> None:
    """#1192: the oldest's kill really fails with its window staying. Not made up
    for within the tick any more — the next tick re-counts and takes the next one."""
    resident = {1, 2, 3}

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        if channel.id == 3:
            return False
        resident.discard(channel.id)
        return True

    loop = _loop(_cog(AsyncMock(side_effect=impl)), resident, 3, 2)
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        assert await loop._enforce_cap() == 0
        assert resident == {1, 2, 3}
        assert await loop._enforce_cap() == 1
    assert resident == {1, 3}


async def test_a_window_gone_by_itself_is_not_made_up_for() -> None:
    """#1094 holds: nothing to kill (``None``) is one fewer over, not a failure."""
    resident = {1, 2, 3, 4}
    calls: list[int] = []

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        calls.append(channel.id)
        resident.discard(channel.id)
        return None if channel.id == 4 else True

    loop = _loop(_cog(AsyncMock(side_effect=impl)), resident, 4, 2)
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        assert await loop._enforce_cap() == 1
    assert calls == [4, 3]
    assert resident == {1, 2}
