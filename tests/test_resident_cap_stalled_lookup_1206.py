"""#1206 — a stalled tmux at the *first* lookup of a sleep read as "window gone".

The resident cap protects the host's memory. Before it kills a window, the
sleep asks tmux whether the window is there (``session_exists``). On a stalled
host ``show-option`` / ``list-windows`` / ``tmux -V`` time out, and that lookup
answered "no window": the sleep reported nothing to kill, the cap counted the
thread as gone and did not make up for it with the next candidate. With limit 1
and residents {1, 2, 3}, a stalled lookup on the oldest left {1, 3} resident for
good. "Could not tell" must count as "still there". No real tmux.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord

import c_lord.tmux as T
from c_lord.cogs.session_manage import SessionManageCog
from c_lord.resident_cap import ResidentCapLoop
from c_lord.tmux import PaneTurnStates
from tests.test_resident_cap_failure_paths import OK, _bot, _rec, _repo

NONE = PaneTurnStates(frozenset(), frozenset())


def _real_impl_cog(mgr: object) -> SessionManageCog:
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


def _stalled(argv: list[str]) -> MagicMock:
    if argv[:2] == ["tmux", "-V"]:
        return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
    return MagicMock(returncode=T._RUN_TIMEOUT_RETURNCODE, stdout="", stderr="timed out")


def _stalled_mgr() -> T.TmuxSessionManager:
    m = T.TmuxSessionManager(session_name="clord", mapping_path="")
    m._thread_to_window[3] = "@3"  # the window is known and alive
    return m


async def test_stalled_lookup_before_kill_is_not_gone() -> None:
    cog = _real_impl_cog(_stalled_mgr())
    loop = ResidentCapLoop(
        _bot(cog=cog, channels={3: _thread3()}),
        _repo([]),
        limit=1,
        resident_fn=set,
        in_flight_fn=set,
        mem_fn=lambda: OK,
    )
    with patch("c_lord.tmux._run", side_effect=_stalled):
        outcome = await loop._sleep_one(3, MagicMock(), "1本")
    # expected: False (could not tell -> made up for by the next candidate)
    assert outcome is not None, "a stalled show-option/list-windows was counted as 'window gone'"


async def test_cap_stays_above_limit_when_lookup_stalls() -> None:
    resident = {1, 2, 3}
    real = _real_impl_cog(_stalled_mgr())

    async def impl(*, channel: object, reason: object, idle_label: object) -> bool | None:
        if channel.id == 3:  # type: ignore[attr-defined]
            with patch("c_lord.tmux._run", side_effect=_stalled):
                return await real._sleep_workspace_impl(
                    channel=channel, reason=reason, idle_label=idle_label
                )
        resident.discard(channel.id)  # type: ignore[attr-defined]
        return True

    cog = MagicMock()
    cog._sleep_workspace_impl = AsyncMock(side_effect=impl)
    ch = {i: MagicMock(id=i, parent_id=10) for i in (1, 2)}
    ch[3] = _thread3()
    loop = ResidentCapLoop(
        _bot(cog=cog, channels=ch),
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


async def test_version_timeout_before_kill_is_not_gone() -> None:
    """Same with ``tmux -V`` timing out: the manager says "not available",
    session_exists says False, and the sleep reported "nothing to kill"."""

    def all_stalled(argv: list[str]) -> MagicMock:
        return MagicMock(returncode=T._RUN_TIMEOUT_RETURNCODE, stdout="", stderr="timed out")

    cog = _real_impl_cog(T.TmuxSessionManager(session_name="clord", mapping_path=""))
    loop = ResidentCapLoop(
        _bot(cog=cog, channels={3: _thread3()}),
        _repo([]),
        limit=1,
        resident_fn=set,
        in_flight_fn=set,
        mem_fn=lambda: OK,
    )
    with patch("c_lord.tmux._run", side_effect=all_stalled):
        outcome = await loop._sleep_one(3, MagicMock(), "1本")
    assert outcome is not None, "a `tmux -V` timeout was counted as 'window gone'"


def test_window_present_tells_no_window_from_no_answer() -> None:
    """tmux answering "no such session" is an answer (False), a timeout is not (None)."""

    def no_session(argv: list[str]) -> MagicMock:
        if argv[:2] == ["tmux", "-V"]:
            return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
        return MagicMock(returncode=1, stdout="", stderr="can't find session: clord")

    with patch("c_lord.tmux._run", side_effect=no_session):
        assert _stalled_mgr().window_present(3) is False
    with patch("c_lord.tmux._run", side_effect=_stalled):
        assert _stalled_mgr().window_present(3) is None


# --- independent review of the #1206 fix: one lookup, not two ---


async def test_stall_between_window_present_and_session_exists() -> None:
    calls = {"n": 0}

    def flaky(argv: list[str]) -> MagicMock:
        if argv[:2] == ["tmux", "-V"]:
            return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
        calls["n"] += 1
        if calls["n"] == 1:  # window_present's show-option answers
            return MagicMock(returncode=0, stdout="3\n", stderr="")
        return MagicMock(returncode=T._RUN_TIMEOUT_RETURNCODE, stdout="", stderr="timed out")

    cog = _real_impl_cog(_stalled_mgr())
    with patch("c_lord.tmux._run", side_effect=flaky):
        out = await cog._sleep_workspace_impl(
            channel=_thread3(), reason=MagicMock(), idle_label="x"
        )
    assert out is not None, "window_present said True, then a stalled session_exists -> None (gone)"
