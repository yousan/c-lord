"""#1228: a stalled tmux during the 4h idle sleep is "could not tell" — it must be retried.

``_sleep_workspace_impl`` answers ``False`` when it could not tell or could not
kill (a stalled tmux), and ``None`` when there was no window to kill (#1192 /
#1206). IdleSleepLoop squashed both into "not slept" and marked the thread
visited, so a single stalled tick kept that idle window awake — holding its
memory — until a human spoke in the thread again. ``False`` is now treated like
the busy skip of #922 / #991: the next tick looks again. ``None`` is still
"visited".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord

import c_lord.tmux as T
from c_lord.idle_sleep import IdleSleepLoop
from tests.test_idle_sleep import NOW, _rec
from tests.test_resident_cap_stalled_lookup_1206 import _real_impl_cog, _stalled


def _healthy(argv: list[str]) -> MagicMock:
    if argv[:2] == ["tmux", "-V"]:
        return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
    if argv[1] == "show-option":
        return MagicMock(returncode=0, stdout="3\n", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")  # kill-window etc.


def _loop() -> IdleSleepLoop:
    mgr = T.TmuxSessionManager(session_name="clord", mapping_path="")
    mgr._thread_to_window[3] = "@3"
    cog = _real_impl_cog(mgr)
    cog._count_tool_shells = AsyncMock(return_value=0)  # type: ignore[method-assign]
    thread = MagicMock(spec=discord.Thread)
    thread.id = 3
    thread.parent_id = 10
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    bot.get_channel = MagicMock(return_value=thread)
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[_rec(3, hours_ago=9)])
    return IdleSleepLoop(bot, repo, threshold_hours=4, now_fn=lambda: NOW)


async def test_idle_sleep_retries_after_a_stalled_lookup() -> None:
    loop = _loop()
    no_containers = AsyncMock(return_value=[])
    with (
        patch("c_lord.tmux._run", side_effect=_stalled),
        patch("c_lord.cogs.session_manage.containers_for_session_dir", no_containers),
    ):
        assert await loop.tick() == 0  # stalled: nothing slept
    with (
        patch("c_lord.tmux._run", side_effect=_healthy),
        patch("c_lord.cogs.session_manage.containers_for_session_dir", no_containers),
    ):
        slept = await loop.tick()
    assert slept == 1, "tmux recovered, but the idle window was marked visited and never retried"


async def test_sanity_healthy_first_tick_sleeps() -> None:
    loop = _loop()
    with (
        patch("c_lord.tmux._run", side_effect=_healthy),
        patch("c_lord.cogs.session_manage.containers_for_session_dir", AsyncMock(return_value=[])),
    ):
        assert await loop.tick() == 1


async def test_nothing_to_kill_is_still_visited_once() -> None:
    """``None`` (no window) stays "visited": the next tick does not ask again."""
    loop = _loop()
    impl = AsyncMock(return_value=None)
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl  # type: ignore[attr-defined]
    assert await loop.tick() == 0
    assert await loop.tick() == 0
    assert impl.await_count == 1


async def test_could_not_sleep_is_asked_again_next_tick() -> None:
    loop = _loop()
    impl = AsyncMock(return_value=False)
    loop._bot.get_cog.return_value._sleep_workspace_impl = impl  # type: ignore[attr-defined]
    assert await loop.tick() == 0
    assert await loop.tick() == 0
    assert impl.await_count == 2
