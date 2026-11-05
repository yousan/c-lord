"""#1213 — a thread whose window sits in another tmux session read as "window gone".

The resident cap protects the host's memory. It counts residents host-wide
(``list-panes -a``), but the sleep only looks inside the session of the thread's
*resolved* manager. Right after a binding change (``/clord-thread-init``) the
window has not moved yet, so that session has no window, the sleep answered
"nothing to kill" (``None``), the cap counted the thread as gone and did not make
up for it with the next candidate: the host stayed above its limit. A window that
is still there in another session must not count as gone. No real tmux — ``_run``
is mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import c_lord.tmux as T
from c_lord.resident_cap import ResidentCapLoop
from c_lord.tmux import PaneTurnStates
from tests.test_resident_cap_failure_paths import OK, _bot, _rec, _repo
from tests.test_resident_cap_stalled_lookup_1206 import _real_impl_cog, _thread3

NONE = PaneTurnStates(frozenset(), frozenset())


def _elsewhere(argv: list[str]) -> MagicMock:
    """The new session has no window for thread 3; the old session still runs its claude."""
    if argv[:2] == ["tmux", "-V"]:
        return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
    if argv[:2] == ["tmux", "list-windows"] and "-a" not in argv:
        return MagicMock(returncode=0, stdout="", stderr="")  # the resolved session: empty
    if argv[:3] == ["tmux", "list-panes", "-a"]:
        return MagicMock(returncode=0, stdout="3\tclaude\t%7\n", stderr="")
    if argv[:2] == ["tmux", "kill-window"]:
        raise AssertionError(f"killed a window: {argv}")
    return MagicMock(returncode=1, stdout="", stderr="unexpected")


def _no_window_anywhere(argv: list[str]) -> MagicMock:
    if argv[:2] == ["tmux", "-V"]:
        return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
    if argv[:3] == ["tmux", "list-panes", "-a"]:
        return MagicMock(returncode=0, stdout="4\tclaude\t%8\n", stderr="")  # another thread
    return MagicMock(returncode=0, stdout="", stderr="")


def _new_session_mgr() -> T.TmuxSessionManager:
    return T.TmuxSessionManager(session_name="new-repo", mapping_path="")


async def test_window_in_another_session_is_not_gone() -> None:
    cog = _real_impl_cog(_new_session_mgr())
    loop = ResidentCapLoop(
        _bot(cog=cog, channels={3: _thread3()}),
        _repo([]),
        limit=1,
        resident_fn=set,
        in_flight_fn=set,
        mem_fn=lambda: OK,
    )
    with patch("c_lord.tmux._run", side_effect=_elsewhere):
        outcome = await loop._sleep_one(3, MagicMock(), "1本")
    # expected: False (still there -> made up for by the next candidate)
    assert outcome is not None, "a window in another tmux session was counted as 'window gone'"


async def test_cap_reaches_its_limit_when_a_window_is_in_another_session() -> None:
    resident = {1, 2, 3}
    real = _real_impl_cog(_new_session_mgr())

    async def impl(*, channel: object, reason: object, idle_label: object) -> bool | None:
        if channel.id == 3:  # type: ignore[attr-defined]
            with patch("c_lord.tmux._run", side_effect=_elsewhere):
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


async def test_no_window_anywhere_is_still_gone() -> None:
    """#1094 holds: no window in its session and none elsewhere is gone (``None``)."""
    cog = _real_impl_cog(_new_session_mgr())
    with patch("c_lord.tmux._run", side_effect=_no_window_anywhere):
        out = await cog._sleep_workspace_impl(
            channel=_thread3(), reason=MagicMock(), idle_label="x"
        )
    assert out is None


async def test_host_wide_lookup_that_stalls_is_not_gone() -> None:
    """The session said "no window" but the host-wide listing timed out: could not tell."""

    def stalled_listing(argv: list[str]) -> MagicMock:
        if argv[:3] == ["tmux", "list-panes", "-a"]:
            return MagicMock(returncode=T._RUN_TIMEOUT_RETURNCODE, stdout="", stderr="timed out")
        return _no_window_anywhere(argv)

    cog = _real_impl_cog(_new_session_mgr())
    with patch("c_lord.tmux._run", side_effect=stalled_listing):
        out = await cog._sleep_workspace_impl(
            channel=_thread3(), reason=MagicMock(), idle_label="x"
        )
    assert out is False


# --- independent review of the #1213 fix: another c-lord's window is not ours ---
#
# prod and staging share one tmux server. A pane of *another* instance
# (``@clord_owner`` differs) that carries one of our thread ids is not a window
# we could ever sleep. Counting it as "still there" made the cap sleep one of our
# live workspaces in its place (on main it read as gone and nothing extra slept).


def _foreign_elsewhere(argv: list[str]) -> MagicMock:
    if argv[:2] == ["tmux", "-V"]:
        return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
    if argv[:2] == ["tmux", "list-windows"] and "-a" not in argv:
        return MagicMock(returncode=0, stdout="", stderr="")
    if argv[:3] == ["tmux", "list-panes", "-a"]:
        if any(T.OWNER_OPTION in a for a in argv):
            return MagicMock(returncode=0, stdout="3\tclaude\t%7\tother-bot\n", stderr="")
        return MagicMock(returncode=0, stdout="3\tclaude\t%7\n", stderr="")
    if argv[:2] == ["tmux", "kill-window"]:
        raise AssertionError(f"killed a window: {argv}")
    return MagicMock(returncode=1, stdout="", stderr="unexpected")


async def test_another_instances_window_does_not_make_us_sleep_ours() -> None:
    resident = {1, 2, 3}
    slept: list[int] = []
    real = _real_impl_cog(_new_session_mgr())

    async def impl(*, channel: object, reason: object, idle_label: object) -> bool | None:
        if channel.id == 3:  # type: ignore[attr-defined]
            with (
                patch("c_lord.tmux._run", side_effect=_foreign_elsewhere),
                patch("c_lord.tmux.instance_owner", return_value="this-bot"),
            ):
                return await real._sleep_workspace_impl(
                    channel=channel, reason=reason, idle_label=idle_label
                )
        slept.append(channel.id)  # type: ignore[attr-defined]
        resident.discard(channel.id)  # type: ignore[attr-defined]
        return True

    cog = MagicMock()
    cog._sleep_workspace_impl = AsyncMock(side_effect=impl)
    ch = {i: MagicMock(id=i, parent_id=10) for i in (1, 2)}
    ch[3] = _thread3()
    loop = ResidentCapLoop(
        _bot(cog=cog, channels=ch),
        _repo([_rec(i, hours_ago=i) for i in range(1, 4)]),
        limit=2,
        resident_fn=lambda: set(resident),
        in_flight_fn=set,
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )
    with patch("c_lord.tmux.pane_turn_states", return_value=NONE):
        await loop.tick()
    assert slept == [], f"slept our live workspace(s) {slept} for another bot's window"


def test_is_resident_anywhere_ignores_another_instances_pane() -> None:
    with (
        patch("c_lord.tmux._run", side_effect=_foreign_elsewhere),
        patch("c_lord.tmux.instance_owner", return_value="this-bot"),
    ):
        assert T.is_resident_anywhere(3) is False


# --- whole-branch review of 79fc532: the count and the sleep must agree on owners ---
#
# The sleep's host-wide check counts only this instance's panes, but the count
# (``resident_thread_ids``) counted every pane. Another bot's pane carrying one of
# our thread ids was counted, every attempt on it answered "gone", and the cap sat
# at limit+1 for good with no "still over" line.


def _listing_with_a_foreign_pane(argv: list[str]) -> MagicMock:
    if argv[:2] == ["tmux", "-V"]:
        return MagicMock(returncode=0, stdout="tmux 3.4", stderr="")
    if argv[:3] == ["tmux", "list-panes", "-a"]:
        if any(T.OWNER_OPTION in a for a in argv):
            out = "1\tclaude\t%1\tthis-bot\n2\tclaude\t%2\t\n3\tclaude\t%3\tother-bot\n"
        else:
            out = "1\tclaude\t%1\n2\tclaude\t%2\n3\tclaude\t%3\n"
        return MagicMock(returncode=0, stdout=out, stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")  # our session: no window for 3


async def test_foreign_window_elsewhere_holds_cap_above_limit_forever() -> None:
    """Limit 2, our residents 1 and 2, and 3's only pane belongs to another bot.
    Adapted from the review: the count is the real reader (no ``resident_fn``),
    since the bug is in what it counts. We are at the limit: nothing is tried."""
    real = _real_impl_cog(_new_session_mgr())
    calls: list[int] = []

    async def impl(*, channel: object, reason: object, idle_label: object) -> bool | None:
        calls.append(channel.id)  # type: ignore[attr-defined]
        return await real._sleep_workspace_impl(
            channel=channel, reason=reason, idle_label=idle_label
        )

    cog = MagicMock()
    cog._sleep_workspace_impl = AsyncMock(side_effect=impl)
    ch = {i: MagicMock(id=i, parent_id=10) for i in (1, 2)}
    ch[3] = _thread3()
    loop = ResidentCapLoop(
        _bot(cog=cog, channels=ch),
        _repo([_rec(i, hours_ago=i) for i in range(1, 4)]),
        limit=2,
        in_flight_fn=set,
        mem_fn=lambda: OK,
        activity_fn=lambda r: None,
    )
    with (
        patch("c_lord.tmux._run", side_effect=_listing_with_a_foreign_pane),
        patch("c_lord.tmux.instance_owner", return_value="this-bot"),
        patch("c_lord.tmux.pane_turn_states", return_value=NONE),
    ):
        for _ in range(10):
            await loop.tick()
        assert T.resident_thread_ids() == {1, 2}
    assert calls == [], f"another bot's pane was counted as ours: tried {calls}"
