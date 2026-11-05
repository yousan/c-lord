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

import c_lord.tmux as T
from c_lord.resident_cap import ResidentCapLoop
from c_lord.tmux import PaneTurnStates
from tests.test_resident_cap_failure_paths import OK, _bot, _cog, _rec, _repo
from tests.test_resident_cap_stalled_lookup_1206 import _real_impl_cog

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


# --- review of 469a137: our own windows with an old owner livelocked the cap ---
#
# The count reads every owner (our windows survive a restart with an old
# ``@clord_owner``, #503), but the checks after a sleep read only this instance's
# panes. An old-owner window of ours whose kill failed read as "gone", was never
# deferred, and — being the oldest — took the tick's one attempt for good. The
# fuzz below drives the real readers and the real sleep through a fake tmux.


class _FakeTmux:
    """One tmux server: windows of threads, in our session or another one."""

    def __init__(self, threads: dict[int, tuple[str, str, str]]) -> None:
        # thread -> (owner, where: bound|elsewhere, kill: ok|stuck|timeout_done)
        self.threads = threads
        self.alive = set(threads)
        self.killed_this_tick: list[int] = []

    def _bound_alive(self) -> list[int]:
        return sorted(t for t in self.alive if self.threads[t][1] == "bound")

    def run(self, argv: list[str]) -> MagicMock:
        def ok(out: str = "") -> MagicMock:
            return MagicMock(returncode=0, stdout=out, stderr="")

        if argv[:2] == ["tmux", "-V"]:
            return ok("tmux 3.4")
        if argv[:3] == ["tmux", "list-panes", "-a"]:
            own = any(T.OWNER_OPTION in a for a in argv)
            lines = [
                f"{t}\tclaude\t%{t}" + (f"\t{self.threads[t][0]}" if own else "")
                for t in sorted(self.alive)
            ]
            return ok("".join(f"{line}\n" for line in lines))
        if argv[:2] == ["tmux", "list-windows"] and "-a" not in argv:
            return ok("".join(f"@{t}\tw{t}\t{t}\t/x/{t}\n" for t in self._bound_alive()))
        if argv[:2] == ["tmux", "show-option"]:
            t = int(argv[argv.index("-t") + 1].lstrip("@"))
            if t in self.alive and self.threads[t][1] == "bound":
                return ok(f"{t}\n")
            return MagicMock(returncode=1, stdout="", stderr="no such window")
        if argv[:2] == ["tmux", "kill-window"]:
            t = int(argv[argv.index("-t") + 1].lstrip("@"))
            kind = self.threads[t][2]
            if kind == "stuck" or t not in self.alive:
                return MagicMock(returncode=1, stdout="", stderr="failed")
            self.alive.discard(t)
            self.killed_this_tick.append(t)
            if kind == "timeout_done":
                return MagicMock(returncode=T._RUN_TIMEOUT_RETURNCODE, stdout="", stderr="")
            return ok()
        return ok()


def _thread(tid: int) -> MagicMock:
    th = MagicMock(spec=discord.Thread)
    th.id = tid
    th.parent_id = 10
    return th


async def _drive(threads: dict[int, tuple[str, str, str]], limit: int, ticks: int) -> _FakeTmux:
    tmux = _FakeTmux(threads)
    cog = _real_impl_cog(T.TmuxSessionManager(session_name="ours", mapping_path=""))
    n = len(threads)
    loop = ResidentCapLoop(
        _bot(cog=cog, channels={t: _thread(t) for t in threads}),
        _repo([_rec(i, hours_ago=i) for i in range(1, n + 1)]),
        limit=limit,
        in_flight_fn=set,
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )
    with (
        patch("c_lord.tmux._run", side_effect=tmux.run),
        patch("c_lord.tmux.instance_owner", return_value="mine"),
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
    ):
        for tick in range(ticks):
            over = max(0, len(tmux.alive) - limit)
            tmux.killed_this_tick.clear()
            await loop.tick()
            assert len(tmux.killed_this_tick) <= over, (tick, threads, limit, over)
    return tmux


async def test_old_owner_window_with_a_failing_kill_does_not_hog_the_cap() -> None:
    """The review's case: the oldest is ours, bound, with an old owner, and its
    kill always fails. Limit 1 of 3: the two behind it must still be slept."""
    threads = {1: ("mine", "bound", "ok"), 2: ("", "bound", "ok"), 3: ("old", "bound", "stuck")}
    tmux = await _drive(threads, limit=1, ticks=10)
    assert tmux.alive == {3}, sorted(tmux.alive)


async def test_old_owner_window_left_in_another_session_does_not_hog_the_cap() -> None:
    """Our old-owner window in another session reads as "gone" to the sleep every
    time, while the count keeps it. It must not take the front for good."""
    threads = {
        1: ("mine", "bound", "ok"),
        2: ("mine", "bound", "ok"),
        3: ("old", "elsewhere", "ok"),
    }
    tmux = await _drive(threads, limit=2, ticks=10)
    assert len(tmux.alive) <= 2, sorted(tmux.alive)


@pytest.mark.parametrize("elsewhere", [False, True], ids=["bound", "bound+elsewhere"])
@pytest.mark.parametrize("seed", range(120))
async def test_fuzz_cap_through_real_readers(seed: int, elsewhere: bool) -> None:
    """From the review of 469a137 (seeded, smaller): owners mine / empty / old,
    windows in our session or (variant 2) another one, kills ok / stuck /
    timeout_done. Converges, never killing more than over in a tick."""
    rng = random.Random(seed * 2 + int(elsewhere))
    n = rng.randint(2, 10)
    limit = rng.randint(1, n)
    threads = {
        t: (
            rng.choice(["mine", "", "old"]),
            rng.choice(["bound", "elsewhere"]) if elsewhere else "bound",
            rng.choice(["ok", "stuck", "timeout_done"]),
        )
        for t in range(1, n + 1)
    }
    tmux = await _drive(threads, limit, 3 * n + 5)
    unkillable = {
        t for t, (_, where, kill) in threads.items() if where != "bound" or kill == "stuck"
    }
    alive = tmux.alive
    assert len(alive) <= max(limit, len(unkillable & alive)) or not (alive - unkillable), (
        threads,
        limit,
        sorted(alive),
    )
