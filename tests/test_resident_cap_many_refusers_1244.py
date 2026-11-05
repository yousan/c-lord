"""#1244 — the resident cap with many windows that keep refusing the kill (review 9).

The cap gives memory back (#576): even when some old windows refuse to be killed,
the workspaces that *can* be slept must bring it back under the limit (#1192 /
#1219). After #1233 a failed window was put last for DEFERRAL_TICKS (10) ticks,
each on its own clock. With 11 or more refusers exactly one of them came back to
the front on every tick and used the tick's only attempt, so the one healthy
workspace was never tried and the cap stayed at limit+1 for good.

Reuses the fake world of ``tests/test_resident_cap_chaos_1219.py`` (no tmux). Each
tick: resident read, kill outcome per mode (ok / refuse).
"""

from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from c_lord.resident_cap import DEFERRAL_TICKS
from tests.test_resident_cap_chaos_1219 import NONE, World, _make, _one_tick_checked
from tests.test_resident_cap_failure_paths import _rec


def _world(n: int) -> World:
    w = World(random.Random(0), n, foreign=0)
    w.records = {i: _rec(i, hours_ago=10 * i) for i in range(1, n + 1)}  # n = oldest
    w.resident = set(range(1, n + 1))
    w.mode = {i: "ok" for i in w.records}
    return w


@pytest.mark.parametrize("quiet_ticks", range(0, 13))
async def test_boundary_transient_failure_released_after_ten_ticks(quiet_ticks: int) -> None:
    """Oldest (4) fails once, tmux then healthy. After `quiet_ticks` ticks at the
    limit, one more workspace opens: 4 is picked again once its deferral expired."""
    w = _world(4)
    w.mode[4] = "refuse"
    loop = _make(w, 3)
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        await loop.tick()  # tick 1: 4 tried, fails; nothing else this tick
        assert w.calls == [4] and w.resident == {1, 2, 3, 4}
        w.mode[4] = "ok"
        w.resident.discard(1)  # at the limit -> quiet ticks do nothing
        for _ in range(quiet_ticks):
            w.calls = []
            await loop.tick()
            assert w.calls == []
        w.records[99] = _rec(99, hours_ago=0)
        w.resident.add(99)
        w.calls = []
        await loop.tick()
    expired = quiet_ticks >= DEFERRAL_TICKS  # deferred on ticks 2..11, front again on 12
    assert w.calls == ([4] if expired else [3]), (quiet_ticks, w.calls)


async def test_refuser_returning_every_eleven_ticks_costs_that_tick() -> None:
    """One window that always refuses, oldest. Every tick a new workspace opens
    (over = 1). The tick the refuser comes back to the front, nobody else is slept."""
    w = _world(4)
    w.mode[4] = "refuse"
    loop = _make(w, 3)
    stuck_ticks = []
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        for tick in range(1, 34):
            w.calls = []
            await _one_tick_checked(w, loop, 3)
            if w.calls == [4]:
                stuck_ticks.append(tick)
            new = max(w.records) + 1
            w.records[new] = _rec(new, hours_ago=0)
            w.mode[new] = "ok"
            w.resident.add(new)
    # observed cadence: tick 1, 12, 23 (each one leaves the cap over for a tick)
    assert stuck_ticks == [1, 12, 23], stuck_ticks


async def test_eleven_refusers_starve_the_one_healthy_workspace() -> None:
    """limit 11, 11 refusing windows (oldest) + 1 healthy (youngest): over = 1.
    Before be332bc (no expiry) the refusers all ended at the back and the healthy
    one was slept on tick 12. With a 10-tick expiry exactly one refuser is back
    at the front on every tick, so the healthy one is never tried."""
    n = 12
    w = _world(n)
    for t in range(2, n + 1):
        w.mode[t] = "refuse"
    loop = _make(w, 11)
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        for _ in range(200):
            await _one_tick_checked(w, loop, 11)
            if 1 not in w.resident:
                break
    assert 1 not in w.resident, "healthy workspace 1 never slept; cap stuck at limit+1"


@pytest.mark.parametrize("seed", range(60))
async def test_fuzz_many_refusers_still_converge(seed: int) -> None:
    """11-30 windows that always refuse, mostly the oldest, and a few healthy ones,
    with a small excess (1-3): every tick keeps the excess bound and the LRU order,
    and the healthy ones are slept until the cap is back at its limit."""
    rng = random.Random(seed)
    k = rng.randint(11, 30)
    h = rng.randint(1, 6)
    n = k + h
    w = World(rng, n, foreign=0)
    # ages: the refusers older than every healthy one, except a few swapped in
    order = list(range(1, n + 1))  # 1 = youngest
    for _ in range(rng.randint(0, 2)):
        i, j = rng.randrange(n), rng.randrange(n)
        order[i], order[j] = order[j], order[i]
    w.records = {t: _rec(t, hours_ago=10 * order[t - 1]) for t in range(1, n + 1)}
    w.resident = set(range(1, n + 1))
    refusers = set(range(h + 1, n + 1))
    w.mode = {t: "refuse" if t in refusers else "ok" for t in w.records}
    limit = max(k, n - rng.randint(1, min(3, h)))
    loop = _make(w, limit)
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        for _ in range(4 * (n + DEFERRAL_TICKS)):
            await _one_tick_checked(w, loop, limit)
    ours = w.resident & set(w.records)
    assert len(ours) <= limit, (sorted(ours - refusers), limit, k)
