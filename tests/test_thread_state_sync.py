"""Tests for c_lord.thread_state_sync (state computation + rename gating)."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord import thread_state_sync
from c_lord.thread_state_sync import (
    ThreadStateSyncLoop,
    _index_by_thread_id,
    _list_all_windows,
    _pane_lamp_state,
)


@dataclass
class _Rec:
    thread_id: int
    topic: str | None = "T"
    state: str | None = "alive"
    tmux_window_id: str | None = None
    auto_topic_locked: int = 0
    # Unused, present for parity with SessionRecord
    session_id: str = "sid"
    working_dir: str | None = None
    model: str | None = None
    origin: str = "discord"
    summary: str | None = None
    created_at: str = ""
    last_used_at: str = ""
    topic_source: str | None = None


def test_index_by_thread_id_keeps_only_digit_tids():
    windows = [
        {
            "session_name": "s",
            "window_id": "@1",
            "window_index": "0",
            "thread_id": "12345",
            "window_name": "work1",
        },
        {
            "session_name": "s",
            "window_id": "@2",
            "window_index": "1",
            "thread_id": "",
            "window_name": "work2",
        },
        {
            "session_name": "s",
            "window_id": "@3",
            "window_index": "2",
            "thread_id": "abc",
            "window_name": "work3",
        },
    ]
    out = _index_by_thread_id(windows)
    assert list(out.keys()) == [12345]


def test_list_all_windows_returns_empty_when_tmux_missing():
    with patch.object(thread_state_sync.subprocess, "run", side_effect=FileNotFoundError):
        assert _list_all_windows() == []


def test_list_all_windows_returns_empty_on_nonzero_exit():
    fake = MagicMock(returncode=1, stdout="")
    with patch.object(thread_state_sync.subprocess, "run", return_value=fake):
        assert _list_all_windows() == []


def test_list_all_windows_parses_pipe_format():
    fake = MagicMock(returncode=0, stdout="clord|@7|3|12345|work1\nother|@1|0||work2\n")
    with patch.object(thread_state_sync.subprocess, "run", return_value=fake):
        out = _list_all_windows()
    assert out[0]["window_id"] == "@7"
    assert out[0]["window_index"] == "3"
    assert out[0]["thread_id"] == "12345"
    assert out[0]["window_name"] == "work1"
    assert out[0]["session_name"] == "clord"


# ── _pane_lamp_state tests ────────────────────────────────────────────────────


def test_pane_lamp_state_waiting_when_bare_prompt():
    pane = "Some content\n❯\n"
    assert _pane_lamp_state(pane) == "waiting"


def test_pane_lamp_state_waiting_gt_prompt():
    pane = "Some content\n>\n"
    assert _pane_lamp_state(pane) == "waiting"


def test_pane_lamp_state_running_when_esc_to_interrupt():
    # Positive signal: "esc to interrupt" line present → running
    pane = "✻ Running bash...\nesc to interrupt\n"
    assert _pane_lamp_state(pane) == "running"


def test_pane_lamp_state_waiting_without_running_signal():
    # No "esc to interrupt" → default waiting even with tool output
    pane = "✻ Running bash...\n● Some response text\n"
    assert _pane_lamp_state(pane) == "waiting"


def test_pane_lamp_state_waiting_with_nbsp_draft_text():
    # Real Claude Code pane: ❯\xa0draft text — NBSP not stripped by .strip()
    pane = "Some content\n❯\xa0PR #122 マージして\n"
    assert _pane_lamp_state(pane) == "waiting"


def test_pane_lamp_state_error_when_api_error():
    pane = "Some content\nAPIError: rate limit exceeded\n❯\n"
    assert _pane_lamp_state(pane) == "error"


def test_pane_lamp_state_error_colon_pattern():
    pane = "Processing...\nError: connection refused\n"
    assert _pane_lamp_state(pane) == "error"


def test_pane_lamp_state_empty_returns_waiting():
    assert _pane_lamp_state("") == "waiting"


def test_pane_lamp_state_error_takes_priority_over_waiting():
    # Error has higher priority than waiting prompt
    pane = "APIError: something\n❯\n"
    assert _pane_lamp_state(pane) == "error"


# ── _sync_one tests ───────────────────────────────────────────────────────────


async def test_sync_one_marks_dead_and_renames():
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    bot = MagicMock()
    # Pretend Discord doesn't have the channel cached — should early-return.
    bot.get_channel.return_value = None

    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    rec = _Rec(thread_id=111, state="alive", topic="トピック", tmux_window_id="@5")

    await loop._sync_one(rec, by_tid={})  # window gone → dead
    repo.set_state.assert_awaited_once_with(111, "dead")


async def test_sync_one_running_when_esc_to_interrupt_in_pane():
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock()

    import discord

    with patch.object(discord, "Thread", fake_thread.__class__):
        bot = MagicMock()
        bot.get_channel.return_value = fake_thread
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
        rec = _Rec(thread_id=222, state="dead", topic="やること", tmux_window_id=None)
        by_tid = {
            222: {
                "window_id": "@9",
                "window_index": "4",
                "thread_id": "222",
                "session_name": "clord",
                "window_name": "work1",
            }
        }

        with (
            patch.object(thread_state_sync, "discord") as discord_mock,
            patch.object(
                thread_state_sync,
                "_capture_pane_text",
                return_value="● response text\nesc to interrupt\n",
            ),
        ):
            discord_mock.Thread = fake_thread.__class__
            discord_mock.HTTPException = Exception
            await loop._sync_one(rec, by_tid)

    repo.set_state.assert_awaited_once_with(222, "running")
    repo.set_tmux_window_id.assert_awaited_once_with(222, "@9")
    fake_thread.edit.assert_awaited_once()
    kwargs = fake_thread.edit.await_args.kwargs
    assert "やること" in kwargs["name"]
    assert "W4" in kwargs["name"]


async def test_sync_one_waiting_when_prompt_visible():
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock()

    import discord

    with patch.object(discord, "Thread", fake_thread.__class__):
        bot = MagicMock()
        bot.get_channel.return_value = fake_thread
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
        rec = _Rec(thread_id=333, state="running", topic="入力待ち", tmux_window_id=None)
        by_tid = {
            333: {
                "window_id": "@5",
                "window_index": "2",
                "thread_id": "333",
                "session_name": "clord",
                "window_name": "work2",
            }
        }

        with (
            patch.object(thread_state_sync, "discord") as discord_mock,
            patch.object(thread_state_sync, "_capture_pane_text", return_value="● done\n❯\n"),
        ):
            discord_mock.Thread = fake_thread.__class__
            discord_mock.HTTPException = Exception
            await loop._sync_one(rec, by_tid)

    repo.set_state.assert_awaited_once_with(333, "waiting")


async def test_sync_one_error_when_error_in_pane():
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock()

    import discord

    with patch.object(discord, "Thread", fake_thread.__class__):
        bot = MagicMock()
        bot.get_channel.return_value = fake_thread
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
        rec = _Rec(thread_id=444, state="running", topic="エラー発生", tmux_window_id=None)
        by_tid = {
            444: {
                "window_id": "@6",
                "window_index": "3",
                "thread_id": "444",
                "session_name": "clord",
                "window_name": "work3",
            }
        }

        with (
            patch.object(thread_state_sync, "discord") as discord_mock,
            patch.object(
                thread_state_sync, "_capture_pane_text", return_value="APIError: rate limit\n"
            ),
        ):
            discord_mock.Thread = fake_thread.__class__
            discord_mock.HTTPException = Exception
            await loop._sync_one(rec, by_tid)

    repo.set_state.assert_awaited_once_with(444, "error")


async def test_sync_one_skips_rename_when_no_topic():
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    bot = MagicMock()
    fake_thread = MagicMock()
    fake_thread.name = "whatever"
    fake_thread.edit = AsyncMock()
    bot.get_channel.return_value = fake_thread

    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    rec = _Rec(thread_id=333, topic=None, state="alive")
    await loop._sync_one(rec, by_tid={})
    fake_thread.edit.assert_not_called()


async def test_loop_start_is_idempotent():
    repo = MagicMock()
    bot = MagicMock()
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    loop.start()
    first = loop._task
    loop.start()
    assert loop._task is first
    await loop.stop()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
