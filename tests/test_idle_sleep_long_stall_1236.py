"""#1236 — idle-sleep after a long tmux stall (review 8, the rest of #1232 / #1228).

Idle-sleep gives memory back. A window whose sleep fails three ticks in a row is
not tried again until someone speaks there (#1228) — but a stalled tmux (every
call timing out) is not the window's fault. Under a stall of 30 minutes or more
(three idle-sleep ticks) the windows tried then were given up on, and stayed
awake after tmux recovered until a human spoke. Also: one stalled tick stays
bounded in time, the stalled ticks rotate through every thread, and a human
speaking after a real give-up starts the count again.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import c_lord.idle_sleep as idle_sleep_mod
import c_lord.tmux as tmux_mod
from c_lord.idle_sleep import IdleSleepLoop
from tests.test_idle_sleep import NOW, _rec
from tests.test_resident_cap_stalled_lookup_1206 import _real_impl_cog

N = 50


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.calls = 0

    def __call__(self) -> float:
        return self.t


def _setup(real_count: bool = True):  # type: ignore[no-untyped-def]
    mgr = tmux_mod.TmuxSessionManager(session_name="clord", mapping_path="")
    for i in range(1, N + 1):
        mgr._thread_to_window[i] = f"@{i}"
    cog = _real_impl_cog(mgr)
    if not real_count:
        cog._count_tool_shells = AsyncMock(return_value=0)  # type: ignore[method-assign]
    threads = {}
    for i in range(1, N + 1):
        th = MagicMock(spec=discord.Thread)
        th.id = i
        th.parent_id = 10
        threads[i] = th
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    bot.get_channel = MagicMock(side_effect=lambda i: threads.get(i))
    recs = [_rec(i, hours_ago=100 - i) for i in range(1, N + 1)]
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=recs)
    loop = IdleSleepLoop(bot, repo, threshold_hours=4, now_fn=lambda: NOW)
    return loop, repo, recs


def _stalled_with(clock: Clock):  # type: ignore[no-untyped-def]
    def run(argv: list[str]) -> MagicMock:
        if argv[:2] == ["tmux", "-V"]:
            return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
        clock.t += tmux_mod._RUN_TIMEOUT_SECONDS
        clock.calls += 1
        return MagicMock(returncode=tmux_mod._RUN_TIMEOUT_RETURNCODE, stdout="", stderr="timed out")

    return run


async def test_stalled_tick_cost_with_real_count_tool_shells() -> None:
    """How long (in tmux timeouts) does one tick cost while tmux stays stalled?"""
    loop, _, _ = _setup(real_count=True)
    clock = Clock()
    attempted = []
    real_impl = loop._bot.get_cog.return_value._sleep_workspace_impl

    async def spy(**kw):  # type: ignore[no-untyped-def]
        attempted.append(kw["channel"].id)
        return await real_impl(**kw)

    loop._bot.get_cog.return_value._sleep_workspace_impl = spy
    with (
        patch.object(idle_sleep_mod, "_clock", clock),
        patch("c_lord.tmux._run", side_effect=_stalled_with(clock)),
        patch("c_lord.cogs.session_manage.containers_for_session_dir", AsyncMock(return_value=[])),
    ):
        await loop.tick()
    print(f"\nattempted={len(attempted)} tmux_timeouts={clock.calls} fake_seconds={clock.t}")
    # 1 tick should be bounded to a few minutes, not N * timeouts
    assert clock.t <= 600, (len(attempted), clock.calls, clock.t)


async def test_stalled_ticks_rotate_through_all_threads() -> None:
    """Under a persistent stall, are threads beyond the first 5 ever tried? (starvation)"""
    loop, _, _ = _setup(real_count=False)
    clock = Clock()
    attempted: list[list[int]] = []
    real_impl = loop._bot.get_cog.return_value._sleep_workspace_impl

    async def spy(**kw):  # type: ignore[no-untyped-def]
        attempted[-1].append(kw["channel"].id)
        return await real_impl(**kw)

    loop._bot.get_cog.return_value._sleep_workspace_impl = spy
    with (
        patch.object(idle_sleep_mod, "_clock", clock),
        patch("c_lord.tmux._run", side_effect=_stalled_with(clock)),
        patch("c_lord.cogs.session_manage.containers_for_session_dir", AsyncMock(return_value=[])),
    ):
        for _ in range(40):
            attempted.append([])
            await loop.tick()
    tried = {t for per in attempted for t in per}
    print("\nper tick:", [len(a) for a in attempted][:12], "distinct:", len(tried))
    assert tried == set(range(1, N + 1))


async def test_human_speaks_after_giving_up_recounts() -> None:
    loop, repo, recs = _setup(real_count=False)
    clock = Clock()
    impl = AsyncMock(return_value=False)
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl
    repo.list_all = AsyncMock(return_value=recs[:1])
    with patch.object(idle_sleep_mod, "_clock", clock):
        for _ in range(5):
            await loop.tick()
        assert impl.await_count == 3
        # someone speaks: last_used_at changes (still past 4h for the test)
        repo.list_all = AsyncMock(
            return_value=[dataclasses.replace(recs[0], last_used_at="2026-08-31 06:00:00")]
        )
        for _ in range(5):
            await loop.tick()
    assert impl.await_count == 6


def _healthy(argv: list[str]) -> MagicMock:
    if argv[:2] == ["tmux", "-V"]:
        return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
    if argv[1] == "show-option":
        # the window's own @thread_id: echo back the target's number
        return MagicMock(returncode=0, stdout=argv[-2].lstrip("@") + "\n", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


async def test_a_30_minute_stall_leaves_windows_awake_after_recovery() -> None:
    """#1228 says: a stalled tmux must not keep an idle window awake until a human speaks.

    Stall for 3 ticks (30 min at the 600 s interval), then tmux recovers. Every
    one of the 50 idle windows should be slept within the following ticks.
    """
    loop, _, _ = _setup(real_count=False)
    clock = Clock()
    killed: set[int] = set()
    real_impl = loop._bot.get_cog.return_value._sleep_workspace_impl

    async def spy(**kw):  # type: ignore[no-untyped-def]
        r = await real_impl(**kw)
        if r:
            killed.add(kw["channel"].id)
        return r

    loop._bot.get_cog.return_value._sleep_workspace_impl = spy
    cs = patch("c_lord.cogs.session_manage.containers_for_session_dir", AsyncMock(return_value=[]))
    with patch.object(idle_sleep_mod, "_clock", clock), cs:
        with patch("c_lord.tmux._run", side_effect=_stalled_with(clock)):
            for _ in range(3):
                await loop.tick()
        with patch("c_lord.tmux._run", side_effect=_healthy):
            for _ in range(5):
                await loop.tick()
    awake = sorted(set(range(1, N + 1)) - killed)
    assert not awake, f"still awake after tmux recovered (until someone speaks): {awake}"


async def test_one_window_that_always_times_out() -> None:
    """Review of 4ef0710: a slow failure never counted toward giving up, so a
    window whose ``kill-window`` always times out (30 s) — tmux itself fine, the
    other windows sleeping normally — was tried again on every tick, forever.
    It now has its own, larger budget of slow failures and is then given up on
    like after three ordinary ones; the other windows are slept."""
    loop, _, _ = _setup(real_count=False)
    clock = Clock()
    killed: set[int] = set()
    tries: list[int] = []
    real_impl = loop._bot.get_cog.return_value._sleep_workspace_impl

    async def spy(**kw):  # type: ignore[no-untyped-def]
        if kw["channel"].id == 1:
            tries.append(1)
        r = await real_impl(**kw)
        if r:
            killed.add(kw["channel"].id)
        return r

    def run(argv: list[str]) -> MagicMock:
        if argv[1] == "kill-window" and "@1" in argv:
            clock.t += tmux_mod._RUN_TIMEOUT_SECONDS
            return MagicMock(
                returncode=tmux_mod._RUN_TIMEOUT_RETURNCODE, stdout="", stderr="timed out"
            )
        return _healthy(argv)

    loop._bot.get_cog.return_value._sleep_workspace_impl = spy
    cs = patch("c_lord.cogs.session_manage.containers_for_session_dir", AsyncMock(return_value=[]))
    with patch.object(idle_sleep_mod, "_clock", clock), cs, patch("c_lord.tmux._run", run):
        for _ in range(60):
            await loop.tick()
    assert killed == set(range(2, N + 1))
    assert idle_sleep_mod._MAX_SLOW == 10
    assert len(tries) == idle_sleep_mod._MAX_SLOW, len(tries)


def _times_out_for(bad: set[int], clock: Clock):  # type: ignore[no-untyped-def]
    """tmux healthy, but ``kill-window`` of the windows in *bad* always times out."""
    targets = {f"@{t}" for t in bad}

    def run(argv: list[str]) -> MagicMock:
        if argv[1] == "kill-window" and targets & set(argv):
            clock.t += tmux_mod._RUN_TIMEOUT_SECONDS
            return MagicMock(
                returncode=tmux_mod._RUN_TIMEOUT_RETURNCODE, stdout="", stderr="timed out"
            )
        return _healthy(argv)

    return run


def _spy(loop: IdleSleepLoop, killed: set[int], tries: Counter[int]) -> None:
    real_impl = loop._bot.get_cog.return_value._sleep_workspace_impl

    async def spy(**kw):  # type: ignore[no-untyped-def]
        tries[kw["channel"].id] += 1
        r = await real_impl(**kw)
        if r:
            killed.add(kw["channel"].id)
        return r

    loop._bot.get_cog.return_value._sleep_workspace_impl = spy


@pytest.mark.parametrize("stall_ticks", [10, 30])
@pytest.mark.parametrize("n", [1, 2, 4])
async def test_small_fleet_long_stall_gives_nobody_up(n: int, stall_ticks: int) -> None:
    """A tmux-wide stall with fewer due windows than ``MAX_UNSURE_PER_TICK``.

    The stall used to be told apart by "five slow answers in one tick", so with
    1-4 idle windows a 100-minute stall used up their slow budget and they stayed
    awake after tmux came back, until someone spoke. Now a tick is a stall when
    no attempt answered fast and a cheap tmux probe is slow too."""
    loop, repo, recs = _setup(real_count=False)
    repo.list_all = AsyncMock(return_value=recs[:n])
    clock = Clock()
    killed: set[int] = set()
    tries: Counter[int] = Counter()
    _spy(loop, killed, tries)
    cs = patch("c_lord.cogs.session_manage.containers_for_session_dir", AsyncMock(return_value=[]))
    with patch.object(idle_sleep_mod, "_clock", clock), cs:
        with patch("c_lord.tmux._run", side_effect=_stalled_with(clock)):
            for _ in range(stall_ticks):
                await loop.tick()
        with patch("c_lord.tmux._run", side_effect=_healthy):
            for _ in range(3):
                await loop.tick()
    assert killed == set(range(1, n + 1)), (sorted(killed), dict(tries))


async def test_six_windows_that_always_time_out_among_twenty() -> None:
    """Six windows whose ``kill-window`` always times out, tmux otherwise fine.

    With "five slow answers in one tick" as the sign of a stall, these six filled
    every tick and were retried forever (5 x 30 s per tick). Each is now given up
    after exactly ``_MAX_SLOW`` tries, and the other fourteen are slept."""
    loop, repo, recs = _setup(real_count=False)
    repo.list_all = AsyncMock(return_value=recs[:20])
    bad = set(range(1, 7))
    clock = Clock()
    killed: set[int] = set()
    tries: Counter[int] = Counter()
    _spy(loop, killed, tries)
    cs = patch("c_lord.cogs.session_manage.containers_for_session_dir", AsyncMock(return_value=[]))
    with (
        patch.object(idle_sleep_mod, "_clock", clock),
        cs,
        patch("c_lord.tmux._run", side_effect=_times_out_for(bad, clock)),
    ):
        for _ in range(40):
            await loop.tick()
    assert killed == set(range(7, 21))
    assert {t: tries[t] for t in bad} == {t: idle_sleep_mod._MAX_SLOW for t in bad}


async def test_a_fast_answer_resets_the_slow_streak() -> None:
    """ "In a row" means in a row: a fast answer for the window in between (here a
    fast "could not sleep") starts its slow count again."""
    loop, repo, recs = _setup(real_count=False)
    repo.list_all = AsyncMock(return_value=recs[:1])
    clock = Clock()
    plan = ["slow"] * 9 + ["fast"] + ["slow"] * 9
    seen: list[str] = []

    async def impl(**kw):  # type: ignore[no-untyped-def]
        step = plan[len(seen)] if len(seen) < len(plan) else "slow"
        seen.append(step)
        if step == "slow":
            clock.t += tmux_mod._RUN_TIMEOUT_SECONDS
        return False

    loop._bot.get_cog.return_value._sleep_workspace_impl = impl
    with patch.object(idle_sleep_mod, "_clock", clock), patch("c_lord.tmux._run", _healthy):
        for _ in range(len(plan)):
            await loop.tick()
        assert len(seen) == len(plan)  # not given up: the fast one broke the streak
        await loop.tick()  # the 10th slow one in a row
        await loop.tick()
    assert len(seen) == len(plan) + 1, seen


async def test_a_fast_could_not_sleep_does_not_prove_tmux_answered() -> None:
    """W57 review 12: a fast ``False`` ("not a thread", "no tmux manager") may never
    have reached tmux, so it must not stand in for the probe. Here tmux is stalled
    (the probe fails); one window answers a fast ``False`` every tick and another
    times out every tick. The slow one must not be given up during the stall."""
    loop, repo, recs = _setup(real_count=False)
    repo.list_all = AsyncMock(return_value=recs[:2])
    clock = Clock()
    seen: Counter[int] = Counter()

    async def impl(**kw):  # type: ignore[no-untyped-def]
        tid = kw["channel"].id
        seen[tid] += 1
        if tid == 2:
            clock.t += tmux_mod._RUN_TIMEOUT_SECONDS
        return False

    loop._bot.get_cog.return_value._sleep_workspace_impl = impl
    loop._tmux_answers = AsyncMock(return_value=False)  # type: ignore[method-assign]
    with (
        patch.object(idle_sleep_mod, "_clock", clock),
        patch.object(idle_sleep_mod, "_MAX_UNSURE", 1000),  # keep the fast one coming
        patch("c_lord.tmux._run", _healthy),
    ):
        for _ in range(idle_sleep_mod._MAX_SLOW + 5):
            await loop.tick()
    assert seen[2] == idle_sleep_mod._MAX_SLOW + 5, dict(seen)
