"""#1214 / #1219 — the real sleep behind ``POST /api/threads/{id}/sleep`` (external
review W57, round 8).

``staging.sh release`` and the resident cap read the sleep's answer. Eleven ways
the sleep can end, through the real ``_sleep_workspace_impl`` and the API; a kill
that reported failure but was carried out (tmux then answers "gone") is a sleep
like any other — recorded with ``set_slept``, so the next message resumes
silently (#572) and not with the "workspace was down" notice (#464); and an API
sleep racing a cap tick.

A fake tmux world (a dict of windows) stands in for tmux; nothing touches tmux.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from c_lord.cogs.session_manage import SessionManageCog
from c_lord.database.notification_repo import NotificationRepository
from c_lord.ext.api_server import ApiServer
from c_lord.resident_cap import ResidentCapLoop
from c_lord.tmux import PaneTurnStates
from tests.test_resident_cap_failure_paths import OK, _rec, _repo

T = 4242
NONE = PaneTurnStates(frozenset(), frozenset())


def _thread(tid: int = T) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = tid
    t.parent_id = 10
    return t


def _cog(mgr: object | None) -> SessionManageCog:
    cog = SessionManageCog.__new__(SessionManageCog)
    cog._resolve_tmux_manager = AsyncMock(return_value=mgr)
    cog._stop_transcript_mirror = AsyncMock()
    cog._resolve_session_dir_manager = AsyncMock(return_value=None)
    cog.repo = MagicMock()
    cog.repo.set_slept = AsyncMock()
    return cog


def _mgr(present, kill, again=None) -> MagicMock:  # noqa: ANN001
    m = MagicMock()
    m.window_present = MagicMock(side_effect=[present, again])
    m.kill_session = MagicMock(return_value=kill)
    return m


async def _api_status(cog, channel) -> tuple[int, str]:  # noqa: ANN001
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = NotificationRepository(path)
    await repo.init_db()
    bot = MagicMock()
    bot.cogs = {"SessionManageCog": cog}
    bot.get_channel.return_value = channel
    api = ApiServer(repo=repo, bot=bot, default_channel_id=1, host="127.0.0.1", port=0)
    client = TestClient(TestServer(api.app))
    await client.start_server()
    try:
        resp = await client.post(f"/api/threads/{T}/sleep")
        return resp.status, (await resp.json()).get("status")
    finally:
        await client.close()
        os.unlink(path)


@pytest.mark.parametrize(
    ("label", "channel_is_thread", "mgr", "anywhere", "expect"),
    [
        ("not a thread", False, _mgr(True, True), False, (200, "not_resident")),
        ("no manager", True, None, False, (200, "not_resident")),
        ("lookup stalled", True, _mgr(None, True), False, (503, "failed")),
        ("no window anywhere", True, _mgr(False, True), False, (200, "not_resident")),
        ("window in another session", True, _mgr(False, True), True, (200, "not_resident")),
        ("no window here, host-wide stalled", True, _mgr(False, True), None, (503, "failed")),
        ("killed", True, _mgr(True, True), False, (200, "slept")),
        ("kill failed, still there", True, _mgr(True, False, True), True, (503, "failed")),
        ("kill failed, re-look stalled", True, _mgr(True, False, None), True, (503, "failed")),
        (
            "kill failed, gone here, host stalled",
            True,
            _mgr(True, False, False),
            None,
            (503, "failed"),
        ),
        # Adapted: a kill that reported failure but was carried out is a sleep (#1219).
        ("kill failed, gone everywhere", True, _mgr(True, False, False), False, (200, "slept")),
    ],
)
async def test_api_answers(label, channel_is_thread, mgr, anywhere, expect) -> None:  # noqa: ANN001
    cog = _cog(mgr)
    channel = _thread() if channel_is_thread else MagicMock(id=T)
    with patch("c_lord.tmux.is_resident_anywhere", return_value=anywhere):
        assert await _api_status(cog, channel) == expect, label


async def test_kill_reported_failed_but_gone_is_recorded_as_slept() -> None:
    """The kill timed out but tmux carried it out: the window is gone because *we*
    killed it, and the mirror was already stopped. Not recorded as slept -> the next
    message resumes with the crash notice (#464) instead of silently (#572)."""
    cog = _cog(_mgr(True, False, False))
    with patch("c_lord.tmux.is_resident_anywhere", return_value=False):
        out = await cog._sleep_workspace_impl(channel=_thread(), reason=MagicMock())
    assert out is True  # adapted: the review asserted None; decided 2026-11-05 it is a sleep
    cog._stop_transcript_mirror.assert_awaited_once()
    cog.repo.set_slept.assert_awaited_once_with(T, True)


class _World:
    """One window per thread; kill_session is slow and thread-safe like tmux."""

    def __init__(self, threads: set[int]) -> None:
        self.windows = set(threads)
        self.lock = threading.Lock()

    def present(self, tid: int) -> bool:
        with self.lock:
            return tid in self.windows

    def kill(self, tid: int) -> bool:
        time.sleep(0.05)
        with self.lock:
            if tid in self.windows:
                self.windows.discard(tid)
                return True
            return False  # "can't find window"


def _world_mgr(w: _World) -> MagicMock:
    m = MagicMock()
    m.window_present = MagicMock(side_effect=w.present)
    m.kill_session = MagicMock(side_effect=w.kill)
    return m


async def test_api_sleep_racing_a_cap_tick_on_the_same_thread() -> None:
    """The cap's victim is slept through the API at the same moment. Neither side
    may report a failure, and the cap must not go on to sleep a second thread."""
    for _ in range(10):
        w = _World({1, 2, 3})
        cog = _cog(_world_mgr(w))
        bot = MagicMock()
        bot.get_cog = MagicMock(return_value=cog)
        bot.get_channel = MagicMock(side_effect=lambda t: _thread(t))
        loop = ResidentCapLoop(
            bot,
            _repo([_rec(1, hours_ago=3), _rec(2, hours_ago=2), _rec(3, hours_ago=1)]),
            limit=2,
            resident_fn=lambda w=w: set(w.windows),
            in_flight_fn=set,
            mem_fn=lambda: OK,
            activity_fn=lambda r: None,
        )
        with (
            patch("c_lord.tmux.pane_turn_states", return_value=NONE),
            patch("c_lord.tmux.is_resident_anywhere", side_effect=w.present),
        ):
            api_out, _ = await asyncio.gather(
                cog._sleep_workspace_impl(
                    channel=_thread(1), reason=MagicMock(), unkillable_is_absent=True
                ),
                loop.tick(),
            )
            assert api_out in (True, None), api_out
            assert w.windows == {2, 3}, w.windows
            # next tick: nothing more to do
            await loop.tick()
            assert w.windows == {2, 3}, w.windows


async def test_two_ticks_at_once_do_not_sleep_twice_the_excess() -> None:
    """tick() is only called from _run, but nothing stops a second caller; check
    that two concurrent ticks over a slow kill do not sleep 2 x over."""
    w = _World({1, 2, 3, 4})
    cog = _cog(_world_mgr(w))
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    bot.get_channel = MagicMock(side_effect=lambda t: _thread(t))
    loop = ResidentCapLoop(
        bot,
        _repo([_rec(i, hours_ago=i) for i in (1, 2, 3, 4)]),
        limit=3,
        resident_fn=lambda: set(w.windows),
        in_flight_fn=set,
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )
    with (
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
        patch("c_lord.tmux.is_resident_anywhere", side_effect=w.present),
    ):
        await asyncio.gather(loop.tick(), loop.tick())
    assert len(w.windows) >= 3, w.windows
