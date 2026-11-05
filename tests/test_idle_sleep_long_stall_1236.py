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
from unittest.mock import AsyncMock, MagicMock, patch

import discord

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
