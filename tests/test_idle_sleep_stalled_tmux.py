"""#1228: a stalled tmux during the 4h idle sleep is "could not tell" — it must be retried.

``_sleep_workspace_impl`` answers ``False`` when it could not tell or could not
kill (a stalled tmux), and ``None`` when there was no window to kill (#1192 /
#1206). IdleSleepLoop squashed both into "not slept" and marked the thread
visited, so a single stalled tick kept that idle window awake — holding its
memory — until a human spoke in the thread again. ``False`` is now treated like
the busy skip of #922 / #991: the next tick looks again. ``None`` is still
"visited".
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import c_lord.tmux as T
from c_lord.idle_sleep import MAX_SLEEPS_PER_TICK, MAX_UNSURE_PER_TICK, IdleSleepLoop
from tests.test_idle_sleep import NOW, _rec
from tests.test_resident_cap_stalled_lookup_1206 import _real_impl_cog, _stalled


def _healthy(argv: list[str]) -> MagicMock:
    if argv[:2] == ["tmux", "-V"]:
        return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
    if argv[1] == "show-option":
        return MagicMock(returncode=0, stdout="3\n", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")  # kill-window etc.


def _loop() -> IdleSleepLoop:
    mgr = T.TmuxSessionManager(session_name="clord", mapping_path="")
    mgr._thread_to_window[3] = "@3"
    cog = _real_impl_cog(mgr)
    cog._count_tool_shells = AsyncMock(return_value=0)  # type: ignore[method-assign]
    thread = MagicMock(spec=discord.Thread)
    thread.id = 3
    thread.parent_id = 10
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    bot.get_channel = MagicMock(return_value=thread)
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[_rec(3, hours_ago=9)])
    return IdleSleepLoop(bot, repo, threshold_hours=4, now_fn=lambda: NOW)


async def test_idle_sleep_retries_after_a_stalled_lookup() -> None:
    loop = _loop()
    no_containers = AsyncMock(return_value=[])
    with (
        patch("c_lord.tmux._run", side_effect=_stalled),
        patch("c_lord.cogs.session_manage.containers_for_session_dir", no_containers),
    ):
        assert await loop.tick() == 0  # stalled: nothing slept
    with (
        patch("c_lord.tmux._run", side_effect=_healthy),
        patch("c_lord.cogs.session_manage.containers_for_session_dir", no_containers),
    ):
        slept = await loop.tick()
    assert slept == 1, "tmux recovered, but the idle window was marked visited and never retried"


async def test_sanity_healthy_first_tick_sleeps() -> None:
    loop = _loop()
    with (
        patch("c_lord.tmux._run", side_effect=_healthy),
        patch("c_lord.cogs.session_manage.containers_for_session_dir", AsyncMock(return_value=[])),
    ):
        assert await loop.tick() == 1


async def test_nothing_to_kill_is_still_visited_once() -> None:
    """``None`` (no window) stays "visited": the next tick does not ask again."""
    loop = _loop()
    impl = AsyncMock(return_value=None)
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl  # type: ignore[attr-defined]
    assert await loop.tick() == 0
    assert await loop.tick() == 0
    assert impl.await_count == 1


async def test_could_not_sleep_is_asked_again_next_tick() -> None:
    loop = _loop()
    impl = AsyncMock(return_value=False)
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl  # type: ignore[attr-defined]
    assert await loop.tick() == 0
    assert await loop.tick() == 0
    assert impl.await_count == 2


# --- review of the #1228 fix: ``False`` is also returned for causes that never go
# away (not a Thread, no repo binding / tmux manager). Retrying those every tick
# forever, silently, and without a per-tick bound, is its own bug.


async def test_could_not_sleep_three_times_is_given_up_with_one_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = _loop()
    impl = AsyncMock(return_value=False)
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl  # type: ignore[attr-defined]
    with caplog.at_level(logging.INFO, logger="c_lord.idle_sleep"):
        for _ in range(6):
            assert await loop.tick() == 0
    assert impl.await_count == 3  # then visited, as before #1228
    given_up = [
        r
        for r in caplog.records
        if "thread=3" in r.getMessage() and "not trying again" in r.getMessage()
    ]
    assert [r.levelno for r in given_up] == [logging.INFO]


async def test_a_success_in_between_resets_the_count() -> None:
    loop = _loop()
    impl = AsyncMock(side_effect=[False, False, None])
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl  # type: ignore[attr-defined]
    for _ in range(3):
        await loop.tick()
    assert loop._unsure_counts == {}


def _many_threads_loop(n: int, max_per_tick: int) -> tuple[IdleSleepLoop, AsyncMock]:
    loop = _loop()
    loop._max_per_tick = max_per_tick
    threads = {}
    for tid in range(100, 100 + n):
        t = MagicMock(spec=discord.Thread)
        t.id = tid
        t.parent_id = 10
        threads[tid] = t
    loop._bot.get_channel = MagicMock(side_effect=lambda tid: threads[tid])  # type: ignore[attr-defined]
    loop._repo.list_all = AsyncMock(  # type: ignore[attr-defined]
        return_value=[_rec(tid, hours_ago=9) for tid in threads]
    )
    impl = AsyncMock(return_value=False)
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl  # type: ignore[attr-defined]
    return loop, impl


async def test_a_stall_costs_at_most_max_per_tick_attempts() -> None:
    loop, impl = _many_threads_loop(10, max_per_tick=4)
    assert await loop.tick() == 0
    assert impl.await_count == 4  # not 10 tmux timeouts in one tick


# --- second review of the #1228 fix: the budget above is 1000 by default (and
# ``list_all`` returns at most 1000), so it never bounded a stall in practice;
# and the failure count outlived a human speaking in the thread.


def _slow_false(clock: list[float]) -> AsyncMock:
    """A stalled tmux: the answer is ``False`` and it took its timeouts to come."""

    async def answer(**_: object) -> bool:
        clock[0] += 30.0
        return False

    return AsyncMock(side_effect=answer)


async def test_a_stall_costs_a_few_attempts_per_tick_with_the_default_budget() -> None:
    loop, _ = _many_threads_loop(30, max_per_tick=MAX_SLEEPS_PER_TICK)
    clock = [0.0]
    impl = _slow_false(clock)
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl  # type: ignore[attr-defined]
    with patch("c_lord.idle_sleep._clock", lambda: clock[0], create=True):
        assert await loop.tick() == 0
        assert impl.await_count == MAX_UNSURE_PER_TICK
        assert await loop.tick() == 0  # the next tick moves on, it does not stop for good
        assert impl.await_count == 2 * MAX_UNSURE_PER_TICK


# --- third review of the #1228 fix: a ``False`` that comes back at once is a
# cause that never goes away (not a thread, no tmux manager), not a stall. It
# must not use up the per-tick cap and hold back the healthy threads behind it.


async def test_fast_falses_do_not_hold_back_healthy_threads() -> None:
    loop = _loop()
    bad = list(range(200, 205))  # oldest first
    good = [300, 301, 302]
    threads = {}
    for tid in bad + good:
        t = MagicMock(spec=discord.Thread)
        t.id = tid
        t.parent_id = 10
        threads[tid] = t
    loop._bot.get_channel = MagicMock(side_effect=lambda tid: threads[tid])  # type: ignore[attr-defined]
    loop._repo.list_all = AsyncMock(  # type: ignore[attr-defined]
        return_value=[_rec(t, hours_ago=10) for t in bad] + [_rec(t, hours_ago=9) for t in good]
    )

    async def answer(*, channel: object, **_: object) -> bool:
        return channel.id in good  # type: ignore[attr-defined]

    impl = AsyncMock(side_effect=answer)
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl  # type: ignore[attr-defined]
    assert await loop.tick() == 3  # was 0 (then 0, 0, 3)
    # the fast falses still meet the 3-strikes rule: tried 3 ticks, then left alone
    for _ in range(4):
        await loop.tick()
    tried = [c.kwargs["channel"].id for c in impl.await_args_list]
    assert all(tried.count(t) == 3 for t in bad)


async def test_successes_after_the_cap_are_not_blocked_by_earlier_ticks() -> None:
    """The cap is per tick: once tmux answers again, the next tick sleeps normally."""
    loop, impl = _many_threads_loop(30, max_per_tick=MAX_SLEEPS_PER_TICK)
    await loop.tick()
    impl.return_value = True
    assert await loop.tick() == 30


async def test_someone_speaking_resets_the_failure_count() -> None:
    loop = _loop()
    impl = AsyncMock(return_value=False)
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl  # type: ignore[attr-defined]
    await loop.tick()
    await loop.tick()  # two stalls
    assert impl.await_count == 2
    # someone speaks; later the thread is idle again
    loop._repo.list_all = AsyncMock(return_value=[_rec(3, hours_ago=5)])  # type: ignore[attr-defined]
    for _ in range(6):
        await loop.tick()
    assert impl.await_count == 2 + 3  # three fresh attempts, then visited
