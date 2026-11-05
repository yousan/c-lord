"""#1219 — the order and the memory of failed victims (whole-branch review of 79fc532).

The resident cap protects memory and should sleep as few workspaces as it can.
Since #1219 one tick tries at most the excess, and a victim whose sleep failed is
tried after every other candidate. Two gaps in that:

* failed victims were retried oldest-first, so behind one window that can never
  be killed (a window left in its old session, #1213), a younger one that failed
  only once was never tried again: limit 1, {1, 2, 3} stayed at {2, 3} for good.
  They are now retried in the order they last failed.
* a resident read that timed out read as "nobody is resident", which emptied the
  memory of failed victims. A read that did not answer now skips the tick.

No real tmux.
"""

from __future__ import annotations

import random
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.resident_cap import ResidentCapLoop
from c_lord.tmux import PaneTurnStates
from tests.test_resident_cap_failure_paths import OK, _bot, _cog, _rec, _repo

NONE = PaneTurnStates(frozenset(), frozenset())


async def test_transiently_failed_victim_behind_stuck_oldest_is_never_retried() -> None:
    resident = {1, 2, 3}  # 3 oldest: kill never works (e.g. #1213 window elsewhere)
    failed_once: set[int] = set()
    calls: list[list[int]] = []

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        calls[-1].append(channel.id)
        if channel.id == 3:
            return False
        if channel.id == 2 and 2 not in failed_once:  # one tmux hiccup
            failed_once.add(2)
            return False
        resident.discard(channel.id)
        return True

    loop = ResidentCapLoop(
        _bot(cog=_cog(AsyncMock(side_effect=impl))),
        _repo([_rec(i, hours_ago=i) for i in (1, 2, 3)]),
        limit=1,
        resident_fn=lambda: set(resident),
        in_flight_fn=set,
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        for _ in range(20):
            calls.append([])
            await loop.tick()
    assert len(resident) <= 1, f"{sorted(resident)} after 20 ticks (limit 1); calls {calls[:5]}"


async def test_stalled_read_between_ticks_wipes_deferred() -> None:
    """Every other tick the resident read times out. Adapted from the review: the
    cap's ``resident_fn`` says "could not read" by raising (as the real reader now
    does on a timeout), not by answering an empty set."""
    real = {1, 2}  # 2 oldest, its kill always fails (window stays)
    stalled = [False]
    calls: list[int] = []

    def resident_fn() -> set[int]:
        if stalled[0]:
            raise RuntimeError("list-panes timed out")
        return set(real)

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        calls.append(channel.id)
        if channel.id == 2:
            return False
        real.discard(channel.id)
        return True

    loop = ResidentCapLoop(
        _bot(cog=_cog(AsyncMock(side_effect=impl))),
        _repo([_rec(i, hours_ago=i) for i in (1, 2)]),
        limit=1,
        resident_fn=resident_fn,
        in_flight_fn=set,
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        for i in range(10):
            stalled[0] = i % 2 == 1  # every other tick the listing times out
            await loop.tick()
    assert len(real) <= 1, f"{sorted(real)} resident after 10 ticks (limit 1)"
    assert calls == [2, 1], f"the failed victim was forgotten and retried first: {calls}"


KINDS = ["ok", "stuck", "timeout_done", "self_gone", "forbidden", "raise", "flaky"]


@pytest.mark.parametrize("seed", range(300))
async def test_fuzz_cap(seed: int) -> None:
    """From the review: every seed converges, never killing more than over a tick."""
    rng = random.Random(seed)
    n = rng.randint(2, 12)
    limit = rng.randint(1, n)
    kind = {i: rng.choice(KINDS) for i in range(1, n + 1)}
    resident = set(range(1, n + 1))
    killed_this_tick: list[int] = []

    async def impl(*, channel, reason, idle_label):  # noqa: ANN001
        k = kind[channel.id]
        if k == "ok":
            resident.discard(channel.id)
            killed_this_tick.append(channel.id)
            return True
        if k == "stuck":
            return False
        if k == "timeout_done":
            resident.discard(channel.id)
            killed_this_tick.append(channel.id)
            return False
        if k == "self_gone":
            resident.discard(channel.id)
            return None
        if k == "raise":
            raise RuntimeError("boom")
        if k == "flaky":
            if rng.random() < 0.5:
                return False
            resident.discard(channel.id)
            killed_this_tick.append(channel.id)
            return True
        raise AssertionError(k)

    def get_channel(t: int):  # noqa: ANN202
        return None if kind[t] == "forbidden" else MagicMock(id=t)

    bot = _bot(cog=_cog(AsyncMock(side_effect=impl)))
    bot.get_channel = MagicMock(side_effect=get_channel)
    bot.fetch_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    loop = ResidentCapLoop(
        bot,
        _repo([_rec(i, hours_ago=i) for i in range(1, n + 1)]),
        limit=limit,
        resident_fn=lambda: set(resident),
        in_flight_fn=set,
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        for _ in range(3 * n + 5):
            over = max(0, len(resident) - limit)
            killed_this_tick.clear()
            await loop.tick()
            assert len(killed_this_tick) <= over, (seed, kind, limit, killed_this_tick, over)
    unkillable = {t for t, k in kind.items() if k in ("stuck", "forbidden", "raise")}
    killable_left = resident - unkillable
    assert len(resident) <= max(limit, len(unkillable & resident)) or not killable_left, (
        seed,
        kind,
        limit,
        sorted(resident),
    )
    if not unkillable and all(k in ("ok", "self_gone") for k in kind.values()):
        assert len(resident) == min(limit, n), (seed, kind, limit, sorted(resident))
