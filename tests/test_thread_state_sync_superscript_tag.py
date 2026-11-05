"""#1255: one tmux window tagged ``@thread_id=²`` must not stop the sweeps.

The tmux server is shared, so anyone can set a window option. ``"²".isdigit()``
is True but ``int("²")`` raises, so a single such window made the thread-state
sync (and with it the DB sync) and the menu watchdog raise on every pass: the
AskUserQuestion / plan menus of every window after it never became Discord
buttons. #1239 fixed the same shape in ``c_lord/tmux.py``; this is the rest.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord import thread_state_sync as tss


def test_state_sync_index_survives_superscript_tag() -> None:
    """ThreadStateSyncLoop.tick -> _index_by_thread_id on `tmux list-windows -a`."""
    windows = [
        {"session_name": "x", "window_id": "@1", "window_index": "1", "thread_id": "²"},
        {"session_name": "c", "window_id": "@2", "window_index": "2", "thread_id": "5"},
    ]
    by_tid = tss._index_by_thread_id(windows)  # must not raise
    assert 5 in by_tid


async def test_menu_watchdog_survives_superscript_tag() -> None:
    """MenuWatchdogLoop.tick: a window tagged ``²`` must not stop the sweep for the
    windows after it (the per-window try only wraps _maybe_bridge_open_menu)."""
    windows = [
        {
            "session_name": "x",
            "window_id": "@1",
            "window_index": "1",
            "thread_id": "²",
            "window_name": "w1",
        },
        {
            "session_name": "c",
            "window_id": "@2",
            "window_index": "2",
            "thread_id": "5",
            "window_name": "w2",
        },
    ]
    bot = MagicMock()
    bot.get_cog.return_value = None
    loop = tss.MenuWatchdogLoop(bot)
    seen: list[int] = []

    async def bridge(thread_id, *_a):  # noqa: ANN001, ANN202
        seen.append(thread_id)

    with (
        patch.object(tss, "_list_all_windows", return_value=windows),
        patch.object(tss, "_capture_pane_text", return_value=""),
        patch.object(loop, "_maybe_bridge_open_menu", side_effect=bridge),
    ):
        await loop.tick()  # must not raise
    assert seen == [5]


# #1287: ASCII digits, but more than Python's 4300-digit ``int()`` limit. Someone on
# the shared tmux server can set a 5000-digit ``@thread_id``; ``int()`` raised
# ValueError and stopped the same two sweeps. A Discord snowflake is at most 20
# digits, so a longer value is not a c-lord window.

HUGE = "9" * 5000


def test_state_sync_index_survives_huge_tag() -> None:
    windows = [
        {"session_name": "x", "window_id": "@1", "window_index": "1", "thread_id": HUGE},
        {"session_name": "c", "window_id": "@2", "window_index": "2", "thread_id": "5"},
    ]
    assert 5 in tss._index_by_thread_id(windows)


def test_state_sync_index_skips_21_digit_tag() -> None:
    windows = [{"session_name": "x", "window_id": "@1", "window_index": "1", "thread_id": "1" * 21}]
    assert tss._index_by_thread_id(windows) == {}


async def test_menu_watchdog_survives_huge_tag() -> None:
    windows = [
        {
            "session_name": "x",
            "window_id": "@1",
            "window_index": "1",
            "thread_id": HUGE,
            "window_name": "w1",
        },
        {
            "session_name": "c",
            "window_id": "@2",
            "window_index": "2",
            "thread_id": "5",
            "window_name": "w2",
        },
    ]
    bot = MagicMock()
    bot.get_cog.return_value = None
    loop = tss.MenuWatchdogLoop(bot)
    seen: list[int] = []

    async def bridge(thread_id, *_a):  # noqa: ANN001, ANN202
        seen.append(thread_id)

    with (
        patch.object(tss, "_list_all_windows", return_value=windows),
        patch.object(tss, "_capture_pane_text", return_value=""),
        patch.object(loop, "_maybe_bridge_open_menu", side_effect=bridge),
    ):
        await loop.tick()
    assert seen == [5]
