"""#718 / #907: the edges of taking back a lamp a restart left behind.

``tests/test_lamp_recovery.py`` pins the main path (⚠️/🟢 → 🟡 when the turn
is over or ends while watched). This file pins the rest of the contract the
module docstring states:

* ``find_open_lamp`` — the fallback for a thread whose trigger id was never
  recorded: "The newest lamp of either kind decides: a 🟡/❌ above an old ⚠️
  means that ⚠️ belongs to a turn that is long over".
* a watch that runs past ``MAX_SECONDS`` gives up and leaves the lamp to the
  stall monitor — it must not paint 🟡 on a turn nobody knows is over, and
  returns ``None`` so the caller posts no 📊 / 🟡 summons for it (#907);
* production restarts several times a day (#718: usually c-lord deploying
  itself), so the watch can itself be cancelled by the next restart; and the
  transcript check can fail. "Failures are logged and dropped — the lamp is
  decoration (#632)".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from c_lord.discord_ui.lamp_recovery import adopt_orphaned_lamp, find_open_lamp
from c_lord.discord_ui.status import (
    EMOJI_ERROR,
    EMOJI_RUNNING,
    EMOJI_STALL_HARD,
    EMOJI_STALL_SOFT,
    EMOJI_WAITING,
)


class _Message:
    """A message whose ``reactions`` are what the previous process left."""

    def __init__(self, *emoji: str, mine: bool = True, msg_id: int = 0) -> None:
        self.id = msg_id
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


async def _history(*messages: _Message) -> AsyncIterator[_Message]:
    """``thread.history()`` — newest first."""
    for m in messages:
        yield m


def _running(value: bool):
    async def check() -> bool:
        return value

    return check


# ── find_open_lamp: the fallback when no trigger id is on record ─────────────


class TestFindOpenLamp:
    @pytest.mark.asyncio
    async def test_the_newest_open_lamp_is_found(self) -> None:
        newest = _Message(msg_id=3)
        lamp = _Message(EMOJI_STALL_HARD, msg_id=2)
        older = _Message(EMOJI_RUNNING, msg_id=1)
        assert await find_open_lamp(_history(newest, lamp, older)) is lamp  # type: ignore[arg-type]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("final", [EMOJI_WAITING, EMOJI_ERROR])
    async def test_a_final_lamp_above_an_old_warning_means_that_warning_is_not_ours(
        self, final: str
    ) -> None:
        """The newest lamp decides: the ⚠️ below a 🟡/❌ is a long-finished turn's."""
        closed = _Message(final, msg_id=2)
        stale = _Message(EMOJI_STALL_HARD, msg_id=1)
        assert await find_open_lamp(_history(closed, stale)) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_someone_elses_hourglass_is_not_a_lamp(self) -> None:
        theirs = _Message(EMOJI_STALL_SOFT, mine=False, msg_id=2)
        assert await find_open_lamp(_history(theirs)) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_thread_with_no_lamp_at_all_yields_nothing(self) -> None:
        assert await find_open_lamp(_history(_Message(msg_id=1))) is None  # type: ignore[arg-type]


# ── a watch that cannot finish ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_watch_past_max_seconds_gives_up_without_claiming_the_turn_ended() -> None:
    """Still "running" after MAX_SECONDS: stop watching, paint nothing final (#907)."""
    msg = _Message(EMOJI_STALL_HARD)
    got = await asyncio.wait_for(
        adopt_orphaned_lamp(
            msg,  # type: ignore[arg-type]
            thread_id=7190,
            turn_running=_running(True),
            poll_seconds=0.01,
            recheck_seconds=10.0,
            max_seconds=0.05,
        ),
        2,
    )
    assert got is None  # no 📊 / 🟡 summons for a turn not known to be over
    assert EMOJI_WAITING not in msg.lamps
    assert msg.lamps == [EMOJI_RUNNING]  # adopted as running, left as running


@pytest.mark.asyncio
async def test_a_second_restart_cancels_the_watch_and_leaves_the_lamp() -> None:
    """#718: the next deploy kills the watcher — it must not paint 🟡 on its way out."""
    msg = _Message(EMOJI_RUNNING)
    task = asyncio.create_task(
        adopt_orphaned_lamp(
            msg,  # type: ignore[arg-type]
            thread_id=7191,
            turn_running=_running(True),
            poll_seconds=0.01,
            recheck_seconds=10.0,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert msg.lamps == [EMOJI_RUNNING]  # the next process adopts it again


@pytest.mark.asyncio
async def test_a_transcript_check_that_fails_is_logged_and_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#632: the lamp is decoration — a broken check ends the watch, not the bot."""

    async def broken() -> bool:
        raise OSError("transcript dir vanished")

    msg = _Message(EMOJI_STALL_HARD)
    with caplog.at_level("WARNING", logger="c_lord.discord_ui.lamp_recovery"):
        got = await adopt_orphaned_lamp(
            msg,  # type: ignore[arg-type]
            thread_id=7192,
            turn_running=broken,
        )
    assert got is None
    assert msg.lamps == [EMOJI_STALL_HARD]  # nothing known, nothing painted
    assert any("lamp recovery failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_recheck_that_fails_mid_watch_is_logged_and_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = {"n": 0}

    async def flaky() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            return True
        raise OSError("disk went away")

    msg = _Message(EMOJI_RUNNING)
    with caplog.at_level("WARNING", logger="c_lord.discord_ui.lamp_recovery"):
        got = await asyncio.wait_for(
            adopt_orphaned_lamp(
                msg,  # type: ignore[arg-type]
                thread_id=7193,
                turn_running=flaky,
                poll_seconds=0.01,
                recheck_seconds=0.02,
            ),
            2,
        )
    assert got is None
    assert EMOJI_WAITING not in msg.lamps
    assert any("lamp recovery failed" in r.getMessage() for r in caplog.records)
