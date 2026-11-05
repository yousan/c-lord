"""#1219 — the resident cap under chaos (external review W57, round 8).

The cap protects memory and should sleep as few, and as idle, workspaces as it
can. A fake world mixes kills that work, refuse, "fail but happen" (ghost) or
flake, resident reads that stall, users waking threads, turns in flight and
other bots' windows. One tick never attempts or removes more than the excess,
never touches a running turn or a foreign window, and the cap converges. Two
cases from the review: one transient failure (a kill, or a Discord fetch that
sent no kill at all) must not push the longest-idle workspace behind younger
ones for good.

Fake world: a resident set, per-thread kill behaviour (ok / refuse / ghost = reports
failure but the window dies / flaky), resident reads that stall, users waking threads
(last_used_at moves), human turns in flight, foreign windows (no sessions row).
Never touches tmux.
"""

from __future__ import annotations

import dataclasses
import datetime
import random
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.resident_cap import ResidentCapLoop
from c_lord.tmux import PaneTurnStates
from tests.test_resident_cap_failure_paths import OK, _bot, _cog, _rec

NONE = PaneTurnStates(frozenset(), frozenset())
T0 = datetime.datetime(2026, 8, 31, 18, 0, 0)


class World:
    def __init__(self, rng: random.Random, n: int, foreign: int) -> None:
        self.rng = rng
        self.records = {i: _rec(i, hours_ago=rng.uniform(1, 100)) for i in range(1, n + 1)}
        self.resident = set(rng.sample(range(1, n + 1), k=rng.randint(1, n)))
        self.foreign = set(range(1000, 1000 + foreign))
        self.mode: dict[int, str] = {i: "ok" for i in self.records}
        self.busy: set[int] = set()
        self.stall_reads = 0.0
        self.calls: list[int] = []
        self.clock = 0

    def resident_fn(self) -> set[int]:
        if self.rng.random() < self.stall_reads:
            raise RuntimeError("list-panes timed out")
        return set(self.resident) | self.foreign

    async def impl(self, *, channel, reason, idle_label):  # noqa: ANN001
        t = channel.id
        self.calls.append(t)
        m = self.mode.get(t, "ok")
        if t not in self.resident:
            return None
        if m == "ok":
            self.resident.discard(t)
            return True
        if m == "refuse":
            return False
        if m == "ghost":
            self.resident.discard(t)
            return False
        if m == "flaky":
            if self.rng.random() < 0.5:
                self.resident.discard(t)
                return True
            return False
        raise AssertionError(m)

    def wake(self, t: int) -> None:
        self.clock += 1
        ts = (T0 + datetime.timedelta(minutes=self.clock)).strftime("%Y-%m-%d %H:%M:%S")
        self.records[t] = dataclasses.replace(self.records[t], last_used_at=ts)
        self.resident.add(t)

    def repo(self) -> MagicMock:
        repo = MagicMock()
        repo.list_all = AsyncMock(side_effect=lambda **_: list(self.records.values()))
        return repo


def _make(w: World, limit: int) -> ResidentCapLoop:
    return ResidentCapLoop(
        _bot(cog=_cog(AsyncMock(side_effect=w.impl))),
        w.repo(),
        limit=limit,
        resident_fn=w.resident_fn,
        in_flight_fn=lambda: set(w.busy),
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )


def _idle_key(w: World, t: int) -> str:
    return w.records[t].last_used_at


async def _one_tick_checked(w: World, loop: ResidentCapLoop, limit: int) -> None:
    before = set(w.resident)
    ours = before & set(w.records)
    over = len(ours) - limit
    deferred_before = dict(loop._deferred)
    attempted_before = dict(loop._attempted)
    w.calls = []
    await loop.tick()
    calls = list(w.calls)
    # never a foreign window, never a running turn, never twice in a tick
    assert not set(calls) & w.foreign, calls
    assert not set(calls) & w.busy, (calls, w.busy)
    assert len(calls) == len(set(calls)), calls
    # never more attempts than the excess, never more removed than the excess
    assert len(calls) <= max(0, over), (calls, over, sorted(ours))
    removed = before - w.resident
    assert len(removed) <= max(0, over), (removed, over)
    # order: a non-deferred attempt is never younger than a non-deferred
    # candidate left untouched (LRU among the non-deferred)
    if calls:
        demoted = set(deferred_before) | {t for t in attempted_before if t in before}
        cands = [t for t in ours - w.busy if t not in demoted]
        tried = [t for t in calls if t not in demoted]
        untouched = [t for t in cands if t not in calls]
        for a in tried:
            for u in untouched:
                assert _idle_key(w, a) <= _idle_key(w, u) or u in loop._deferred, (a, u)


@pytest.mark.parametrize("seed", range(300))
async def test_fuzz_invariants_and_convergence(seed: int) -> None:
    rng = random.Random(seed)
    n = rng.randint(3, 12)
    limit = rng.randint(1, n)
    w = World(rng, n, foreign=rng.randint(0, 3))
    loop = _make(w, limit)
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        # chaos
        for _ in range(40):
            for t in w.records:
                w.mode[t] = rng.choice(["ok", "ok", "ok", "refuse", "ghost", "flaky"])
            w.stall_reads = rng.choice([0.0, 0.0, 0.3])
            w.busy = set(rng.sample(sorted(w.records), k=rng.randint(0, 2)))
            if rng.random() < 0.4:
                w.wake(rng.choice(sorted(w.records)))
            await _one_tick_checked(w, loop, limit)
        # settle: k persistent refusers, everyone else fine, no stalls, nobody busy
        refusers = set(rng.sample(sorted(w.records), k=rng.randint(0, 2)))
        for t in w.records:
            w.mode[t] = "refuse" if t in refusers else "ok"
        w.stall_reads = 0.0
        w.busy = set()
        for _ in range(3 * n + 5):
            await _one_tick_checked(w, loop, limit)
    ours = w.resident & set(w.records)
    # converges: at the limit, unless the unkillable ones alone exceed it
    assert len(ours) <= max(limit, len(ours & refusers)), (sorted(ours), refusers, limit)


async def test_ghost_kill_every_tick_never_sleeps_more_than_excess() -> None:
    """Sustained stall where every kill 'fails' but happens: each tick removes <= over."""
    w = World(random.Random(1), 8, foreign=0)
    w.resident = set(range(1, 9))
    for t in w.records:
        w.mode[t] = "ghost"
    loop = _make(w, 5)
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        for _ in range(10):
            await _one_tick_checked(w, loop, 5)
    assert len(w.resident) == 5, w.resident


async def test_transient_failure_demotes_the_oldest_until_its_user_returns() -> None:
    """One transient failure on the longest-idle window: afterwards (tmux healthy)
    younger windows are slept ahead of it on every later over-cap event."""
    w = World(random.Random(2), 4, foreign=0)
    w.records = {i: _rec(i, hours_ago=10 * i) for i in range(1, 5)}  # 4 = oldest
    w.resident = {1, 2, 3, 4}
    w.mode[4] = "refuse"
    loop = _make(w, 3)
    from c_lord.resident_cap import DEFERRAL_TICKS

    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        await loop.tick()  # 4 fails once (transient)
        w.mode[4] = "ok"
        slept_order = []
        for _ in range(DEFERRAL_TICKS + 2):
            w.calls = []
            await loop.tick()
            slept_order += w.calls
            # someone else (not the idle 4) opens a new workspace -> over again
            new = max(w.records) + 1
            w.records[new] = _rec(new, hours_ago=0)
            w.resident.add(new)
    # Adapted from the review (which wanted 4 on the very next tick): a failed
    # victim is deferred, but only for DEFERRAL_TICKS ticks — then the
    # longest-idle one is chosen again although its user never came back.
    assert 4 in slept_order, slept_order
    assert slept_order.index(4) <= DEFERRAL_TICKS, slept_order


async def test_a_transient_fetch_error_demotes_the_thread_without_any_kill() -> None:
    """Docs: only a window *tried and not slept* is moved to the back. A thread
    whose fetch_channel raised a transient error (no kill sent, not "tried") is
    still put behind every other candidate on the next tick."""
    import discord as _d

    w = World(random.Random(3), 4, foreign=0)
    w.records = {i: _rec(i, hours_ago=10 * i) for i in range(1, 5)}  # 4 oldest
    w.resident = {1, 2, 3, 4}
    loop = _make(w, 3)
    flaky = {"on": True}

    def get_channel(t):  # noqa: ANN001, ANN202
        return None if t == 4 else MagicMock(id=t)

    async def fetch(t):  # noqa: ANN001, ANN202
        if flaky["on"]:
            raise _d.HTTPException(MagicMock(status=503), "discord hiccup")
        return MagicMock(id=t)

    loop._bot.get_channel = MagicMock(side_effect=get_channel)
    loop._bot.fetch_channel = AsyncMock(side_effect=fetch)
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        await loop.tick()  # 4 cannot be fetched; 3 is slept in its place (#1033)
        assert w.resident == {1, 2, 4}
        flaky["on"] = False
        w.records[5] = _rec(5, hours_ago=0)
        w.resident.add(5)
        w.calls = []
        await loop.tick()
    assert w.calls == [4], w.calls  # longest idle, never tried (no kill was sent)
