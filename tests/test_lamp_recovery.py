"""#718: a lamp orphaned by a bot restart mid-turn is taken back on startup.

The per-turn lamp lives in the process. A restart while a turn is running kills
its StatusManager, and whatever it last painted stays on the message forever —
all six "⚠️ only" lamps measured after #727 were exactly this: the thread's
turn was still running when the bot restarted (usually its own deploy).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from c_lord.discord_ui.lamp_recovery import adopt_orphaned_lamp
from c_lord.discord_ui.status import (
    EMOJI_ERROR,
    EMOJI_RUNNING,
    EMOJI_STALL_HARD,
    EMOJI_WAITING,
    StatusManager,
)
from c_lord.turn_activity import turn_activity
from c_lord.turn_end_bus import turn_end_bus


class _Message:
    """Reactions as Discord holds them: ``reactions`` is what the last run left."""

    def __init__(self, *emoji: str, mine: bool = True) -> None:
        self._emoji = list(emoji)
        self._mine = mine
        self.guild = MagicMock()

    @property
    def reactions(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(emoji=e, me=self._mine) for e in self._emoji]

    @property
    def lamps(self) -> list[str]:
        return list(self._emoji)

    async def add_reaction(self, emoji: str) -> None:
        if emoji not in self._emoji:
            self._emoji.append(emoji)
        await asyncio.sleep(0)

    async def remove_reaction(self, emoji: str, member: object) -> None:
        if emoji in self._emoji:
            self._emoji.remove(emoji)
        await asyncio.sleep(0)


def _running(value: bool):
    async def check() -> bool:
        return value

    return check


FAST = {"poll_seconds": 0.02, "recheck_seconds": 0.05}


@pytest.mark.asyncio
async def test_a_finished_turn_left_on_warning_ends_on_waiting() -> None:
    """The measured #718 case: ⚠️ left behind, the turn has since finished."""
    msg = _Message(EMOJI_STALL_HARD)
    await adopt_orphaned_lamp(msg, thread_id=7180, turn_running=_running(False), **FAST)
    assert msg.lamps == [EMOJI_WAITING]


@pytest.mark.asyncio
async def test_a_finished_turn_left_on_green_ends_on_waiting() -> None:
    """After #769 the orphan is 🟢 rather than ⚠️ — same bug, same cure."""
    msg = _Message(EMOJI_RUNNING)
    await adopt_orphaned_lamp(msg, thread_id=7181, turn_running=_running(False), **FAST)
    assert msg.lamps == [EMOJI_WAITING]


@pytest.mark.asyncio
@pytest.mark.parametrize("final", [EMOJI_WAITING, EMOJI_ERROR])
async def test_a_final_lamp_is_left_alone(final: str) -> None:
    msg = _Message(final)
    await adopt_orphaned_lamp(msg, thread_id=7182, turn_running=_running(True), **FAST)
    assert msg.lamps == [final]


@pytest.mark.asyncio
async def test_someone_elses_reaction_is_not_a_lamp() -> None:
    msg = _Message(EMOJI_STALL_HARD, mine=False)
    await adopt_orphaned_lamp(msg, thread_id=7183, turn_running=_running(False), **FAST)
    assert msg.lamps == [EMOJI_STALL_HARD]


@pytest.mark.asyncio
async def test_a_still_running_turn_goes_green_then_waiting_at_its_end() -> None:
    msg = _Message(EMOJI_STALL_HARD)
    task = asyncio.create_task(
        adopt_orphaned_lamp(msg, thread_id=7184, turn_running=_running(True), **FAST)
    )
    await asyncio.sleep(0.1)
    assert msg.lamps == [EMOJI_RUNNING]  # Claude is still working after the restart
    turn_end_bus.mark(7184)  # the mirror read the turn-end marker
    await asyncio.wait_for(task, 1)
    assert msg.lamps == [EMOJI_WAITING]


@pytest.mark.asyncio
async def test_a_turn_that_ends_without_a_marker_is_caught_by_the_recheck() -> None:
    state = {"running": True}

    async def check() -> bool:
        return state["running"]

    msg = _Message(EMOJI_RUNNING)
    task = asyncio.create_task(adopt_orphaned_lamp(msg, thread_id=7185, turn_running=check, **FAST))
    await asyncio.sleep(0.1)
    assert msg.lamps == [EMOJI_RUNNING]
    state["running"] = False
    await asyncio.wait_for(task, 1)
    assert msg.lamps == [EMOJI_WAITING]


@pytest.mark.asyncio
async def test_a_new_turn_in_the_thread_retires_the_adopted_lamp() -> None:
    """The next message's turn owns the thread now; the old one reads 🟡."""
    msg = _Message(EMOJI_RUNNING)
    task = asyncio.create_task(
        adopt_orphaned_lamp(msg, thread_id=7186, turn_running=_running(True), **FAST)
    )
    await asyncio.sleep(0.1)
    new = StatusManager(_Message(), thread_id=7186)
    await new.set_running()
    try:
        await asyncio.wait_for(task, 1)
        assert msg.lamps == [EMOJI_WAITING]
        turn_activity.note(7186)  # the new turn still hears its activity
    finally:
        await new.cleanup()


@pytest.mark.asyncio
async def test_a_lamp_that_cannot_be_read_is_skipped() -> None:
    msg = MagicMock()
    type(msg).reactions = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    await adopt_orphaned_lamp(msg, thread_id=7187, turn_running=_running(False), **FAST)


# ── #907: how the watch ended decides whether the turn gets its closing ─────


@pytest.mark.asyncio
async def test_a_turn_over_at_adoption_reports_over() -> None:
    msg = _Message(EMOJI_RUNNING)
    got = await adopt_orphaned_lamp(msg, thread_id=9070, turn_running=_running(False), **FAST)
    assert got == "over"


@pytest.mark.asyncio
async def test_a_turn_that_ends_while_watched_reports_ended() -> None:
    msg = _Message(EMOJI_RUNNING)
    task = asyncio.create_task(
        adopt_orphaned_lamp(msg, thread_id=9071, turn_running=_running(True), **FAST)
    )
    await asyncio.sleep(0.1)
    turn_end_bus.mark(9071)
    assert await asyncio.wait_for(task, 1) == "ended"


@pytest.mark.asyncio
async def test_a_displaced_turn_reports_nothing() -> None:
    """Like an interrupted turn (#583): the newer turn does the closing."""
    msg = _Message(EMOJI_RUNNING)
    task = asyncio.create_task(
        adopt_orphaned_lamp(msg, thread_id=9072, turn_running=_running(True), **FAST)
    )
    await asyncio.sleep(0.1)
    new = StatusManager(_Message(), thread_id=9072)
    await new.set_running()
    try:
        assert await asyncio.wait_for(task, 1) is None
    finally:
        await new.cleanup()


@pytest.mark.asyncio
async def test_no_open_lamp_reports_nothing() -> None:
    msg = _Message(EMOJI_WAITING)
    assert (
        await adopt_orphaned_lamp(msg, thread_id=9073, turn_running=_running(False), **FAST) is None
    )


class TestPastTheLongWatch:
    """#992: past ``max_seconds`` the watch slows down but never walks away.

    Decided 2026-10-29: after the cut-off somebody must still be looking at the
    lamp — the stall monitor keeps its ⚠️ honest, and when the turn ends the
    lamp reads 🟡. Before, the give-up stopped the very monitor its docstring
    handed the lamp to, and a 🟢 stayed on a finished turn for good.
    """

    LONG = {**FAST, "max_seconds": 0.05, "slow_poll_seconds": 0.02, "slow_recheck_seconds": 0.05}

    @pytest.mark.asyncio
    async def test_the_stall_monitor_still_runs_after_the_cut_off(self, monkeypatch) -> None:
        managers: list[StatusManager] = []
        real_adopt = StatusManager.adopt

        def spy(message, emoji, *, thread_id=None):
            manager = real_adopt(message, emoji, thread_id=thread_id)
            managers.append(manager)
            return manager

        monkeypatch.setattr(StatusManager, "adopt", staticmethod(spy))
        msg = _Message(EMOJI_RUNNING)
        task = asyncio.create_task(
            adopt_orphaned_lamp(msg, thread_id=9920, turn_running=_running(True), **self.LONG)
        )
        await asyncio.sleep(0.2)  # well past max_seconds
        try:
            assert not task.done()
            stall = managers[0]._stall_task
            assert stall is not None and not stall.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_a_turn_that_ends_after_the_cut_off_still_reads_waiting(self) -> None:
        msg = _Message(EMOJI_RUNNING)
        task = asyncio.create_task(
            adopt_orphaned_lamp(msg, thread_id=9921, turn_running=_running(True), **self.LONG)
        )
        await asyncio.sleep(0.2)
        turn_end_bus.mark(9921)
        assert await asyncio.wait_for(task, 1) == "ended"
        assert msg.lamps == [EMOJI_WAITING]

    @pytest.mark.asyncio
    async def test_the_recheck_still_catches_an_end_without_a_marker(self) -> None:
        state = {"running": True}

        async def check() -> bool:
            return state["running"]

        msg = _Message(EMOJI_RUNNING)
        task = asyncio.create_task(
            adopt_orphaned_lamp(msg, thread_id=9922, turn_running=check, **self.LONG)
        )
        await asyncio.sleep(0.2)
        state["running"] = False
        assert await asyncio.wait_for(task, 1) == "ended"
        assert msg.lamps == [EMOJI_WAITING]


class TestAWatchWithNoTurnLeft:
    """#992 review: a turn whose claude died never ends — the watch must not either.

    The transcript stays "mid-turn" for ever once claude is killed mid-tool (host
    restart, crash, the window slept or stopped). Watching without a cut-off then
    meant watching for the life of the process, re-recording the thread on every
    shutdown (#870) and re-adopting it — with a 🔄 line — on every start.
    """

    @pytest.mark.asyncio
    async def test_a_claude_positively_gone_ends_the_watch_on_warning(self) -> None:
        msg = _Message(EMOJI_RUNNING)

        async def gone() -> bool:
            return False

        got = await asyncio.wait_for(
            adopt_orphaned_lamp(
                msg, thread_id=9923, turn_running=_running(True), claude_alive=gone, **FAST
            ),
            1,
        )
        assert got is None  # no 📊 / 🟡 summons: the turn never reached its end
        assert msg.lamps == [EMOJI_STALL_HARD]

    @pytest.mark.asyncio
    async def test_a_live_claude_keeps_the_watch(self) -> None:
        msg = _Message(EMOJI_RUNNING)

        async def alive() -> bool:
            return True

        task = asyncio.create_task(
            adopt_orphaned_lamp(
                msg, thread_id=9924, turn_running=_running(True), claude_alive=alive, **FAST
            )
        )
        await asyncio.sleep(0.2)
        try:
            assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_past_the_cut_off_the_checks_slow_down() -> None:
    """#992: "watched at the slow pace" — the transcript is re-read far less often."""
    calls: list[float] = []
    loop = asyncio.get_running_loop()

    async def check() -> bool:
        calls.append(loop.time())
        return True

    msg = _Message(EMOJI_RUNNING)
    task = asyncio.create_task(
        adopt_orphaned_lamp(
            msg,
            thread_id=9925,
            turn_running=check,
            poll_seconds=0.01,
            recheck_seconds=0.02,
            max_seconds=0.1,
            slow_poll_seconds=0.3,
            slow_recheck_seconds=0.3,
        )
    )
    await asyncio.sleep(0.6)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    start = calls[0]
    fast = [t for t in calls if t - start < 0.1]
    slow = [t for t in calls if t - start > 0.2]
    assert len(fast) >= 3, calls  # every 0.02s before the cut-off
    assert len(slow) <= 2, calls  # every 0.3s after it — not every 0.02s
