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
        {"session_name": "s", "window_id": "@1", "window_index": "0", "thread_id": "12345"},
        {"session_name": "s", "window_id": "@2", "window_index": "1", "thread_id": ""},
        {"session_name": "s", "window_id": "@3", "window_index": "2", "thread_id": "abc"},
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
    fake = MagicMock(returncode=0, stdout="clord|@7|3|12345\nother|@1|0|\n")
    with patch.object(thread_state_sync.subprocess, "run", return_value=fake):
        out = _list_all_windows()
    assert out[0]["window_id"] == "@7"
    assert out[0]["window_index"] == "3"
    assert out[0]["thread_id"] == "12345"


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


async def test_sync_one_alive_with_index_renames_when_name_differs():
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
        # Force isinstance check to pass
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
        rec = _Rec(thread_id=222, state="dead", topic="やること", tmux_window_id=None)
        by_tid = {222: {"window_id": "@9", "window_index": "4", "thread_id": "222"}}

        with patch.object(thread_state_sync, "discord") as discord_mock:
            discord_mock.Thread = fake_thread.__class__
            discord_mock.HTTPException = Exception
            await loop._sync_one(rec, by_tid)

    repo.set_state.assert_awaited_once_with(222, "alive")
    repo.set_tmux_window_id.assert_awaited_once_with(222, "@9")
    fake_thread.edit.assert_awaited_once()
    kwargs = fake_thread.edit.await_args.kwargs
    assert "やること" in kwargs["name"]
    assert "#4" in kwargs["name"]


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
