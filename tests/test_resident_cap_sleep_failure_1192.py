"""#1192 — a sleep that failed with the window still there was counted as "gone".

The resident cap protects memory. #1153 (#1094) taught it not to make up for a
window that vanished on its own, but it read *every* failed sleep that way:
``kill-window`` failing, no tmux manager, the window in another session. With
limit 1 and residents {1, 2, 3}, a kill that keeps failing on the oldest left
{1, 3} resident for good. A sleep that really failed must be made up for by the
next candidate; only "there was nothing to kill" lowers the count. No real tmux.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord

from c_lord.cogs.session_manage import SessionManageCog
from c_lord.resident_cap import ResidentCapLoop
from c_lord.tmux import PaneTurnStates
from tests.test_resident_cap_failure_paths import OK, _bot, _cog, _rec, _repo

NONE = PaneTurnStates(frozenset(), frozenset())


async def test_cap_stuck_above_limit_when_sleep_fails_with_window_alive() -> None:
    resident = {1, 2, 3}

    async def impl(*, channel: object, reason: object, idle_label: object) -> bool:
        if channel.id == 3:  # type: ignore[attr-defined]  # oldest: kill-window fails
            return False
        resident.discard(channel.id)  # type: ignore[attr-defined]
        return True

    cog = _cog(AsyncMock(side_effect=impl))
    loop = ResidentCapLoop(
        _bot(cog=cog),
        _repo([_rec(i, hours_ago=i) for i in range(1, 4)]),
        limit=1,
        resident_fn=lambda: set(resident),
        in_flight_fn=set,
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        for _ in range(5):
            await loop.tick()
    assert len(resident) <= 1, f"still {sorted(resident)} resident after 5 ticks (limit 1)"


def _real_impl_cog(mgr: MagicMock) -> SessionManageCog:
    cog = SessionManageCog.__new__(SessionManageCog)
    cog._resolve_tmux_manager = AsyncMock(return_value=mgr)  # type: ignore[method-assign]
    cog._stop_transcript_mirror = AsyncMock()  # type: ignore[method-assign]
    cog.repo = MagicMock()
    cog.repo.set_slept = AsyncMock()
    return cog


def _thread3() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = 3
    thread.parent_id = 10
    return thread


async def test_real_sleep_impl_kill_failure_is_not_window_gone() -> None:
    mgr = MagicMock()
    mgr.session_exists = MagicMock(return_value=True)  # the window stays
    mgr.kill_session = MagicMock(return_value=False)  # tmux kill-window rc!=0 (timeout etc.)
    cog = _real_impl_cog(mgr)
    loop = ResidentCapLoop(
        _bot(cog=cog, channels={3: _thread3()}),
        _repo([]),
        limit=1,
        resident_fn=set,
        in_flight_fn=set,
        mem_fn=lambda: OK,
    )
    # expected: False (the window is still there — the next candidate makes up)
    assert await loop._sleep_one(3, MagicMock(), "1本") is not None


async def test_real_sleep_impl_no_window_reads_as_gone() -> None:
    """#1094 still holds: nothing to kill means the window left by itself."""
    mgr = MagicMock()
    mgr.session_exists = MagicMock(return_value=False)
    cog = _real_impl_cog(mgr)
    loop = ResidentCapLoop(
        _bot(cog=cog, channels={3: _thread3()}),
        _repo([]),
        limit=1,
        resident_fn=set,
        in_flight_fn=set,
        mem_fn=lambda: OK,
    )
    assert await loop._sleep_one(3, MagicMock(), "1本") is None


async def test_real_sleep_impl_window_vanished_during_kill_reads_as_gone() -> None:
    """The kill found nothing because claude ended between the check and the kill."""
    mgr = MagicMock()
    mgr.session_exists = MagicMock(side_effect=[True, False])
    mgr.kill_session = MagicMock(return_value=False)
    cog = _real_impl_cog(mgr)
    loop = ResidentCapLoop(
        _bot(cog=cog, channels={3: _thread3()}),
        _repo([]),
        limit=1,
        resident_fn=set,
        in_flight_fn=set,
        mem_fn=lambda: OK,
    )
    assert await loop._sleep_one(3, MagicMock(), "1本") is None
