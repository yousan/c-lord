"""Tests for c_lord.thread_state_sync (state computation + rename gating)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
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
    # #281: persisted rename rate-limit deadline (wall-clock "YYYY-MM-DD HH:MM:SS")
    rename_backoff_until: str | None = None
    # #414: Issue/PR number shown in the thread name
    issue_ref: str | None = None
    # #512: set when the session was intentionally closed (/close-workspace)
    closed_at: str | None = None


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


def test_pane_lamp_state_running_when_tool_executing():
    # ✻ in the pane means a tool (Bash/Read/etc.) is actively running
    pane = "✻ Running bash...\n"
    assert _pane_lamp_state(pane) == "running"


def test_pane_lamp_state_running_when_actualizing():
    # ✢ appears during Claude response generation (Actualizing/Synthesizing)
    pane = "✢ Actualizing… (1m 13s · ↓ 3.6k tokens)\n\n❯\n"
    assert _pane_lamp_state(pane) == "running"


def test_pane_lamp_state_running_when_synthesizing():
    pane = "✢ Synthesizing… (1m 43s · ↓ 5.5k tokens)\n\n❯\n"
    assert _pane_lamp_state(pane) == "running"


def test_pane_lamp_state_waiting_without_running_chars():
    # No ✢ or ✻ → default waiting
    pane = "● Some completed tool output\n❯\n"
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


# ── #190: lamp stuck yellow — real-capture regression fixtures ─────────────────

_PANES_DIR = Path(__file__).parent / "fixtures" / "panes"


def _load_pane(name: str) -> str:
    return (_PANES_DIR / name).read_text()


def test_pane_lamp_state_running_when_spinner_above_footer():
    """#190: live spinner sits ~15 lines above the bottom (input box + status
    footer + a tool-result preview push it up). Real captured pane where the
    old bottom-6 probe returned ``waiting`` despite ``✢ Swirling… (2m 29s · …)``
    being live. Must be ``running``."""
    pane = _load_pane("running_spinner_above_footer.txt")
    assert _pane_lamp_state(pane) == "running"


def test_pane_lamp_state_waiting_with_completed_spinner_no_timer():
    """#190 inverse: an idle pane whose last turn collapsed to
    ``✻ Brewed for 1m 20s`` (a spinner char, but no live ``(Ns · …)`` timer).
    A char-only detector widened to clear the footer would false-positive here
    and get stuck green — must stay ``waiting``."""
    pane = _load_pane("waiting_completed_spinner_no_timer.txt")
    assert _pane_lamp_state(pane) == "waiting"


def test_pane_lamp_state_running_with_alternate_spinner_chars():
    """#190: real spinners also use ✶ and · (not just ✢/✻). Detection must be
    spinner-charset-independent via the live ``(Ns · …)`` timer."""
    assert _pane_lamp_state("✶ Creating PR… (11m 57s · ↑ 36.5k tokens)\n\n❯\n") == "running"
    assert _pane_lamp_state("· Precipitating… (3m 4s · ↑ 10.6k tokens)\n\n❯\n") == "running"


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


async def test_sync_one_running_when_tool_executing_in_pane():
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
        # Divergence case: tmux window_index (4) != work-name number (1).
        # The W<N> label must follow the stable work{N} name, not the volatile index.
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
                return_value="✻ Running bash...\n● response text\n",
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
    # W1 from window_name "work1", NOT W4 from the volatile window_index.
    assert "W1" in kwargs["name"]
    assert "W4" not in kwargs["name"]


async def test_sync_one_keeps_issue_ref_in_name():
    """#414: the lamp-sync rename must keep the #<issue> number in the name."""
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock()

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    rec = _Rec(
        thread_id=222, state="running", topic="やること", tmux_window_id="@9", issue_ref="404"
    )
    by_tid = {
        222: {
            "window_id": "@9",
            "window_index": "1",
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
            return_value="✻ Running bash...\n● response text\n",
        ),
    ):
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = Exception
        await loop._sync_one(rec, by_tid)

    fake_thread.edit.assert_awaited_once()
    assert "#404" in fake_thread.edit.await_args.kwargs["name"]


async def test_sync_one_keeps_closed_marker_in_name():
    """#512 AC5: the 60s sidebar repaint must not strip ``[終了]`` off a closed thread.

    The lamp-sync loop rebuilds the name from the DB row every tick.  Without the
    ``closed`` flag it would rebuild ``#404 やること`` and quietly undo the marker
    ``/close-workspace`` just applied.
    """
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "[終了] #404 やること"
    fake_thread.edit = AsyncMock()

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    rec = _Rec(
        thread_id=222,
        state="dead",
        topic="やること",
        issue_ref="404",
        closed_at="2026-08-18 12:00:00",
    )

    with patch.object(thread_state_sync, "discord") as discord_mock:
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = Exception
        await loop._sync_one(rec, {})  # no tmux window → dead

    # Name already matches the closed form, so no rename is sent at all.
    fake_thread.edit.assert_not_awaited()


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


# ── #236: is_processing guard (event-driven lamp must not be rolled back) ──────


async def test_sync_one_keeps_running_when_processing_despite_waiting_pane():
    """A thread the cog reports as actively processing must stay ``running`` even
    when the poll lands in a brief no-spinner window (startup/tool gap) (#236)."""
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
        # Thread 555 is actively being processed by the cog.
        loop = ThreadStateSyncLoop(
            bot, repo, interval_seconds=999, is_processing=lambda tid: tid == 555
        )
        rec = _Rec(thread_id=555, state="waiting", topic="処理中", tmux_window_id=None)
        by_tid = {
            555: {
                "window_id": "@7",
                "window_index": "1",
                "thread_id": "555",
                "session_name": "clord",
                "window_name": "work1",
            }
        }

        with (
            patch.object(thread_state_sync, "discord") as discord_mock,
            # Pane shows the idle prompt — no spinner yet (startup race).
            patch.object(thread_state_sync, "_capture_pane_text", return_value="● done\n❯\n"),
        ):
            discord_mock.Thread = fake_thread.__class__
            discord_mock.HTTPException = Exception
            await loop._sync_one(rec, by_tid)

    repo.set_state.assert_awaited_once_with(555, "running")


async def test_sync_one_waiting_when_not_processing():
    """Without the is_processing guard (or when it returns False), a no-spinner
    pane still resolves to ``waiting`` — existing behavior preserved (#236)."""
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
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999, is_processing=lambda tid: False)
        rec = _Rec(thread_id=556, state="running", topic="入力待ち", tmux_window_id=None)
        by_tid = {
            556: {
                "window_id": "@8",
                "window_index": "2",
                "thread_id": "556",
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

    repo.set_state.assert_awaited_once_with(556, "waiting")


async def test_sync_one_error_overrides_processing_guard():
    """The is_processing guard only promotes waiting→running; an error pane must
    still win (#236)."""
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
        loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999, is_processing=lambda tid: True)
        rec = _Rec(thread_id=557, state="running", topic="エラー", tmux_window_id=None)
        by_tid = {
            557: {
                "window_id": "@9",
                "window_index": "3",
                "thread_id": "557",
                "session_name": "clord",
                "window_name": "work3",
            }
        }

        with (
            patch.object(thread_state_sync, "discord") as discord_mock,
            patch.object(thread_state_sync, "_capture_pane_text", return_value="APIError: boom\n"),
        ):
            discord_mock.Thread = fake_thread.__class__
            discord_mock.HTTPException = Exception
            await loop._sync_one(rec, by_tid)

    repo.set_state.assert_awaited_once_with(557, "error")


class _FakeHTTPException(Exception):
    """Minimal discord.HTTPException stand-in for rate-limit tests."""

    def __init__(self, status: int, retry_after: float = 300.0) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}")


# ── per-thread 429 backoff tests ─────────────────────────────────────────────


async def test_sync_one_429_sets_per_thread_backoff():
    """429 HTTPException should set per-thread backoff and log a warning."""
    import asyncio

    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock(side_effect=_FakeHTTPException(429, retry_after=300.0))

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    rec = _Rec(thread_id=777, state="waiting", topic="rate-limit test")

    with (
        patch.object(thread_state_sync, "discord") as discord_mock,
        patch.object(thread_state_sync, "_capture_pane_text", return_value="❯\n"),
    ):
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = _FakeHTTPException
        await loop._sync_one(rec, by_tid={})

    assert 777 in loop._rename_backoff
    now = asyncio.get_event_loop().time()
    assert loop._rename_backoff[777] > now + 299.0


async def test_sync_one_skips_rename_during_backoff():
    """While thread is in backoff window, rename PATCH must not be sent."""
    import asyncio

    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock()

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)

    # Pre-set backoff for thread 888 far into the future.
    loop._rename_backoff[888] = asyncio.get_event_loop().time() + 600.0

    rec = _Rec(thread_id=888, state="waiting", topic="backoff test")
    with (
        patch.object(thread_state_sync, "discord") as discord_mock,
        patch.object(thread_state_sync, "_capture_pane_text", return_value="❯\n"),
    ):
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = _FakeHTTPException
        await loop._sync_one(rec, by_tid={})

    fake_thread.edit.assert_not_called()


async def test_sync_one_timeout_sets_conservative_backoff():
    """TimeoutError during rename should set a conservative per-thread backoff."""
    import asyncio

    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock(side_effect=asyncio.TimeoutError())

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    rec = _Rec(thread_id=999, state="waiting", topic="timeout test")

    with (
        patch.object(thread_state_sync, "discord") as discord_mock,
        patch.object(thread_state_sync, "_capture_pane_text", return_value="❯\n"),
    ):
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = _FakeHTTPException
        await loop._sync_one(rec, by_tid={})

    assert 999 in loop._rename_backoff
    now = asyncio.get_event_loop().time()
    assert loop._rename_backoff[999] > now + 59.0


async def test_sync_one_429_not_immediately_retried():
    """After a 429, a second tick within backoff window must not call edit again."""
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock(side_effect=_FakeHTTPException(429, retry_after=300.0))

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    rec = _Rec(thread_id=1001, state="waiting", topic="no retry test")

    with (
        patch.object(thread_state_sync, "discord") as discord_mock,
        patch.object(thread_state_sync, "_capture_pane_text", return_value="❯\n"),
    ):
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = _FakeHTTPException
        # First tick — hits 429.
        await loop._sync_one(rec, by_tid={})
        assert fake_thread.edit.call_count == 1

        # Simulate same-loop second tick (backoff already set).
        fake_thread.edit.reset_mock()
        await loop._sync_one(rec, by_tid={})
        fake_thread.edit.assert_not_called()


async def test_sync_one_backoff_cleared_on_success():
    """Successful rename should clear the backoff entry for that thread."""
    import asyncio

    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock()

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)

    # Pre-set an expired backoff.
    loop._rename_backoff[1002] = asyncio.get_event_loop().time() - 1.0  # expired

    rec = _Rec(thread_id=1002, state="waiting", topic="backoff clear test")
    with (
        patch.object(thread_state_sync, "discord") as discord_mock,
        patch.object(thread_state_sync, "_capture_pane_text", return_value="❯\n"),
    ):
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = _FakeHTTPException
        await loop._sync_one(rec, by_tid={})

    # edit was called (backoff expired), and backoff entry cleared.
    fake_thread.edit.assert_awaited_once()
    assert 1002 not in loop._rename_backoff


# ── #277: initial-pass must not burst-rename on startup ───────────────────────


async def test_initial_tick_does_not_rename(monkeypatch):
    """#277: the FIRST tick after startup must not call channel.edit for any
    thread, even when the Discord name diverges from the computed name. It only
    syncs DB state so the next tick has a baseline. This prevents the startup
    rename burst that saturates Discord's per-channel rename rate-limit (429)."""
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()
    # Three dead threads whose names all diverge → would all be renamed pre-fix.
    recs = [
        _Rec(thread_id=tid, state="alive", topic=f"topic{tid}", tmux_window_id="@1")
        for tid in (101, 102, 103)
    ]
    repo.list_all = AsyncMock(return_value=recs)

    fake_thread = MagicMock()
    fake_thread.name = "old name"  # diverges from build_name → pre-fix would edit
    fake_thread.edit = AsyncMock()

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread

    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)

    monkeypatch.setattr(thread_state_sync, "_list_all_windows", lambda: [])

    with patch.object(thread_state_sync, "discord") as discord_mock:
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = _FakeHTTPException
        await loop.tick()

    # Initial pass: NO renames at all …
    fake_thread.edit.assert_not_called()
    # … but DB state IS still synced (baseline for the next tick).
    assert repo.set_state.await_count == 3


async def test_second_tick_renames_normally(monkeypatch):
    """#277: after the initial pass, subsequent ticks rename diverging threads
    as before. Guards against the fix accidentally disabling all renames."""
    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()
    rec = _Rec(thread_id=201, state="alive", topic="やること", tmux_window_id="@1")
    repo.list_all = AsyncMock(return_value=[rec])

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock()

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread

    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    monkeypatch.setattr(thread_state_sync, "_list_all_windows", lambda: [])

    with patch.object(thread_state_sync, "discord") as discord_mock:
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = _FakeHTTPException
        # First tick: initial pass — no rename.
        await loop.tick()
        fake_thread.edit.assert_not_called()
        # Second tick: normal behaviour — diverging name is renamed.
        await loop.tick()
        fake_thread.edit.assert_awaited_once()


# ── #281: rename backoff persists across restarts ─────────────────────────────


_FIXED_NOW = "2026-06-02 12:00:00"


def _ts(offset_seconds: int) -> str:
    """Return a wall-clock timestamp `offset_seconds` from _FIXED_NOW."""
    import datetime as _dt

    base = _dt.datetime.strptime(_FIXED_NOW, "%Y-%m-%d %H:%M:%S")
    return (base + _dt.timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


async def test_persisted_backoff_in_future_skips_rename(monkeypatch):
    """#281: a FRESH loop (simulating a restart — _rename_backoff empty) must
    still honour a backoff deadline persisted in the DB. Without persistence the
    restart would forget the rate-limit window and re-PATCH within it (429)."""
    import datetime as _dt

    monkeypatch.setattr(
        thread_state_sync,
        "_now",
        lambda: _dt.datetime.strptime(_FIXED_NOW, "%Y-%m-%d %H:%M:%S"),
    )

    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()
    repo.set_rename_backoff_until = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock()

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    # In-memory backoff is empty (fresh process), but DB says "still backed off".
    rec = _Rec(thread_id=1201, state="waiting", topic="persisted backoff", tmux_window_id="@1")
    rec.rename_backoff_until = _ts(300)  # 5 min in the future

    with (
        patch.object(thread_state_sync, "discord") as discord_mock,
        patch.object(thread_state_sync, "_capture_pane_text", return_value="❯\n"),
    ):
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = _FakeHTTPException
        await loop._sync_one(rec, by_tid={})

    fake_thread.edit.assert_not_called()


async def test_persisted_backoff_in_past_allows_rename(monkeypatch):
    """#281: once the persisted deadline has passed, rename proceeds normally."""
    import datetime as _dt

    monkeypatch.setattr(
        thread_state_sync,
        "_now",
        lambda: _dt.datetime.strptime(_FIXED_NOW, "%Y-%m-%d %H:%M:%S"),
    )

    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()
    repo.set_rename_backoff_until = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock()

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    rec = _Rec(thread_id=1202, state="waiting", topic="expired backoff", tmux_window_id="@1")
    rec.rename_backoff_until = _ts(-1)  # already expired

    with (
        patch.object(thread_state_sync, "discord") as discord_mock,
        patch.object(thread_state_sync, "_capture_pane_text", return_value="❯\n"),
    ):
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = _FakeHTTPException
        await loop._sync_one(rec, by_tid={})

    fake_thread.edit.assert_awaited_once()


async def test_429_persists_backoff_to_db(monkeypatch):
    """#281: a 429 must persist the backoff deadline to the DB (not just memory)
    so a restart before the window elapses still honours it."""
    import datetime as _dt

    monkeypatch.setattr(
        thread_state_sync,
        "_now",
        lambda: _dt.datetime.strptime(_FIXED_NOW, "%Y-%m-%d %H:%M:%S"),
    )

    repo = MagicMock()
    repo.set_state = AsyncMock()
    repo.set_tmux_window_id = AsyncMock()
    repo.set_rename_backoff_until = AsyncMock()

    fake_thread = MagicMock()
    fake_thread.name = "old name"
    fake_thread.edit = AsyncMock(side_effect=_FakeHTTPException(429, retry_after=300.0))

    bot = MagicMock()
    bot.get_channel.return_value = fake_thread
    loop = ThreadStateSyncLoop(bot, repo, interval_seconds=999)
    rec = _Rec(thread_id=1203, state="waiting", topic="429 persist")

    with (
        patch.object(thread_state_sync, "discord") as discord_mock,
        patch.object(thread_state_sync, "_capture_pane_text", return_value="❯\n"),
    ):
        discord_mock.Thread = fake_thread.__class__
        discord_mock.HTTPException = _FakeHTTPException
        await loop._sync_one(rec, by_tid={})

    # Deadline persisted = now + retry_after (300s).
    repo.set_rename_backoff_until.assert_awaited_once_with(1203, _ts(300))


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


# -- #359 menu watchdog -------------------------------------------------------
# The 60s pane sweep must bridge an unresolved AskUserQuestion/plan menu that
# no run_claude turn is watching (turn finalized early / mirror blind), so the
# user gets Discord buttons within one tick instead of never.


def _fixture(name: str) -> str:
    from pathlib import Path

    return (Path(__file__).parent / "fixtures" / "panes" / name).read_text()


def _make_loop(is_processing=lambda _tid: False):
    bot = MagicMock()
    bot.get_cog.return_value = None
    bot.tmux_manager = MagicMock()
    bot.ask_repo = None
    thread = MagicMock(spec=thread_state_sync.discord.Thread)
    bot.get_channel.return_value = thread
    loop = thread_state_sync.MenuWatchdogLoop(bot, interval_seconds=60, is_processing=is_processing)
    return loop, bot, thread


@pytest.mark.asyncio
async def test_watchdog_bridges_unwatched_menu():
    """An open menu in the pane with no active turn gets bridged (RED for #359)."""
    loop, bot, thread = _make_loop()
    pane = _fixture("ask_rich_descriptions.txt")
    with (
        patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
        patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge,
    ):
        await loop._maybe_bridge_open_menu(111, "sess", "w1", pane)
        # the bridge runs as a background task — let it start
        await asyncio.sleep(0)
        task = loop._ask_bridges.get(111)
        assert task is not None
        await task
    bridge.assert_awaited_once()
    q = bridge.await_args.args[1]
    assert [o.label for o in q.options] == [
        "カナリアリリース",
        "ブルーグリーン",
        "ローリング更新",
        "一斉切り替え",
    ]


@pytest.mark.asyncio
async def test_watchdog_skips_while_turn_active():
    """While a run_claude turn is processing, its poll loop owns menus."""
    loop, bot, thread = _make_loop(is_processing=lambda _tid: True)
    pane = _fixture("ask_rich_descriptions.txt")
    with (
        patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
        patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge,
    ):
        await loop._maybe_bridge_open_menu(112, "sess", "w1", pane)
        await asyncio.sleep(0)
    bridge.assert_not_awaited()
    assert 112 not in loop._ask_bridges


@pytest.mark.asyncio
async def test_watchdog_dedups_active_bridge_and_busy_bus():
    """No double-bridge: pending watchdog task or an ask_bus waiter blocks re-entry."""
    from c_lord.discord_ui.ask_bus import ask_bus

    loop, bot, thread = _make_loop()
    pane = _fixture("ask_rich_descriptions.txt")
    never = asyncio.get_event_loop().create_future()

    async def _hang(*a, **k):
        await never

    with (
        patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
        patch(
            "c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock(side_effect=_hang)
        ) as bridge,
    ):
        await loop._maybe_bridge_open_menu(113, "sess", "w1", pane)
        await asyncio.sleep(0)
        await loop._maybe_bridge_open_menu(113, "sess", "w1", pane)  # pending task → skip
        assert bridge.await_count <= 1
        # separate thread: a foreign ask_bus waiter (e.g. live poll bridge) blocks too
        ask_bus.register(114)
        try:
            await loop._maybe_bridge_open_menu(114, "sess", "w1", pane)
            await asyncio.sleep(0)
            assert 114 not in loop._ask_bridges
        finally:
            ask_bus.unregister(114)
        never.set_result(None)
        t = loop._ask_bridges.get(113)
        if t is not None:
            await t


@pytest.mark.asyncio
async def test_watchdog_ignores_pane_without_menu():
    loop, bot, thread = _make_loop()
    with (
        patch.object(thread_state_sync, "_capture_pane_text", return_value="❯ \n-- INSERT --"),
        patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge,
    ):
        await loop._maybe_bridge_open_menu(115, "sess", "w1", "❯ \n-- INSERT --")
        await asyncio.sleep(0)
    bridge.assert_not_awaited()


# -- #420: stranded menu when no manager resolves -----------------------------
# A channel without a /clord-init binding resolves no TmuxSessionManager
# (resolve_tmux_manager → None) and bot.tmux_manager is unwired (main.py never
# passes one), so the watchdog used to log "no tmux manager" and give up every
# tick — the open menu never reached Discord and the tmux→Discord mirror "cut
# off". But the sweep ALREADY knows the session_name the window lives in (it
# captured the pane from it), so the bridge must still happen.


@pytest.mark.asyncio
async def test_watchdog_bridges_using_swept_session_when_no_manager_resolves():
    """#420: resolve_tmux_manager=None AND bot.tmux_manager=None must NOT strand
    the menu — the watchdog builds a manager from the swept session_name and
    bridges. RED before the fix: it logged 'no tmux manager' and never bridged."""
    from c_lord.cogs.channel_repo import ChannelRepoCog
    from c_lord.tmux import TmuxSessionManager

    bot = MagicMock()
    bot.tmux_manager = None  # global default not wired (main.py:226)
    bot.ask_repo = None
    # Parent channel has no /clord-init binding → resolve returns None.
    cog = MagicMock(spec=ChannelRepoCog)
    cog.resolve_tmux_manager = AsyncMock(return_value=None)
    bot.get_cog.return_value = cog
    thread = MagicMock(spec=thread_state_sync.discord.Thread)
    thread.parent_id = 999
    bot.get_channel.return_value = thread

    loop = thread_state_sync.MenuWatchdogLoop(bot, interval_seconds=60)
    pane = _fixture("ask_rich_descriptions.txt")
    with (
        patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
        patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge,
    ):
        await loop._maybe_bridge_open_menu(222, "clord", "w94", pane)
        await asyncio.sleep(0)
        task = loop._ask_bridges.get(222)
        assert task is not None, "menu was stranded — watchdog gave up instead of bridging"
        await task

    bridge.assert_awaited_once()
    # The runner must target the session the sweep located, not a re-resolved one.
    assert bridge.await_args is not None
    runner = bridge.await_args.args[2]
    assert isinstance(runner._tmux, TmuxSessionManager)
    assert runner._tmux.session_name == "clord"


@pytest.mark.asyncio
async def test_watchdog_still_gives_up_when_session_name_empty():
    """The empty-session_name guard stays: with nothing to target, the watchdog
    must not fabricate a default session — it logs and returns (safety preserved)."""
    bot = MagicMock()
    bot.tmux_manager = None
    bot.ask_repo = None
    bot.get_cog.return_value = None
    thread = MagicMock(spec=thread_state_sync.discord.Thread)
    thread.parent_id = 999
    bot.get_channel.return_value = thread

    loop = thread_state_sync.MenuWatchdogLoop(bot, interval_seconds=60)
    pane = _fixture("ask_rich_descriptions.txt")
    with (
        patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
        patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge,
    ):
        await loop._maybe_bridge_open_menu(223, "", "w94", pane)
        await asyncio.sleep(0)
    bridge.assert_not_awaited()
    assert 223 not in loop._ask_bridges


# ── #438: menu watchdog must only act on windows THIS bot owns ────────────────
# `tmux list-windows -a` returns every session on a shared tmux server, including
# other bots'. The watchdog must ignore foreign windows (else bot B bridges bot
# A's menu). Ownership = thread_id ∈ my sessions.db (AC2) AND session ∈ my
# managed tmux sessions (AC1).


def _make_owned_watchdog(known_thread_ids, managed_sessions):
    """Build a watchdog whose repo knows `known_thread_ids` and whose
    ChannelRepoCog manages `managed_sessions`."""
    from c_lord.cogs.channel_repo import ChannelRepoCog

    bot = MagicMock()
    cog = MagicMock(spec=ChannelRepoCog)
    cog.managed_session_names = AsyncMock(return_value=set(managed_sessions))
    bot.get_cog.return_value = cog

    repo = MagicMock()
    known = set(known_thread_ids)
    repo.get = AsyncMock(side_effect=lambda tid: object() if tid in known else None)

    loop = thread_state_sync.MenuWatchdogLoop(bot, interval_seconds=60, repo=repo)
    return loop, bot, repo


@pytest.mark.asyncio
async def test_watchdog_skips_window_for_thread_not_in_db():
    """AC2: a window whose @thread_id is not in this bot's sessions.db (another
    bot's session on the shared tmux server) is never bridged."""
    loop, _bot, repo = _make_owned_watchdog(known_thread_ids={100}, managed_sessions={"clord"})
    windows = [{"thread_id": "999", "session_name": "other-bot", "window_name": "w1"}]
    with (
        patch.object(thread_state_sync, "_list_all_windows", return_value=windows),
        patch.object(thread_state_sync, "_capture_pane_text", return_value="menu") as cap,
        patch.object(loop, "_maybe_bridge_open_menu", new=AsyncMock()) as bridge,
    ):
        await loop.tick()
    bridge.assert_not_awaited()
    cap.assert_not_called()  # foreign window: don't even capture its pane
    repo.get.assert_awaited_with(999)


@pytest.mark.asyncio
async def test_watchdog_processes_window_for_own_thread():
    """An owned thread (in my DB) in a managed session is processed normally."""
    loop, _bot, _repo = _make_owned_watchdog(
        known_thread_ids={100}, managed_sessions={"clord", "myrepo"}
    )
    windows = [{"thread_id": "100", "session_name": "myrepo", "window_name": "w1"}]
    with (
        patch.object(thread_state_sync, "_list_all_windows", return_value=windows),
        patch.object(thread_state_sync, "_capture_pane_text", return_value="menu"),
        patch.object(loop, "_maybe_bridge_open_menu", new=AsyncMock()) as bridge,
    ):
        await loop.tick()
    bridge.assert_awaited_once()
    assert bridge.await_args is not None
    assert bridge.await_args.args[0] == 100


@pytest.mark.asyncio
async def test_watchdog_skips_foreign_session_even_if_thread_id_collides():
    """AC1: a window in a tmux session this bot does NOT manage is skipped, even
    if its @thread_id happens to match one of ours (二重ガード)."""
    loop, _bot, _repo = _make_owned_watchdog(known_thread_ids={100}, managed_sessions={"clord"})
    windows = [{"thread_id": "100", "session_name": "someone-elses-session", "window_name": "w1"}]
    with (
        patch.object(thread_state_sync, "_list_all_windows", return_value=windows),
        patch.object(thread_state_sync, "_capture_pane_text", return_value="menu"),
        patch.object(loop, "_maybe_bridge_open_menu", new=AsyncMock()) as bridge,
    ):
        await loop.tick()
    bridge.assert_not_awaited()


@pytest.mark.asyncio
async def test_watchdog_processes_owned_thread_in_default_clord_session():
    """#420 regression: an owned thread whose window is in the default `clord`
    session (unbound channel) is still bridged — the managed set includes the
    default session, so the ownership filter does not strand it."""
    loop, _bot, _repo = _make_owned_watchdog(known_thread_ids={100}, managed_sessions={"clord"})
    windows = [{"thread_id": "100", "session_name": "clord", "window_name": "w94"}]
    with (
        patch.object(thread_state_sync, "_list_all_windows", return_value=windows),
        patch.object(thread_state_sync, "_capture_pane_text", return_value="menu"),
        patch.object(loop, "_maybe_bridge_open_menu", new=AsyncMock()) as bridge,
    ):
        await loop.tick()
    bridge.assert_awaited_once()


@pytest.mark.asyncio
async def test_watchdog_without_repo_is_backward_compatible():
    """Zero-config / legacy: a loop built without a repo (and no ChannelRepoCog)
    keeps the old behaviour — it does not filter and still processes windows."""
    bot = MagicMock()
    bot.get_cog.return_value = None
    loop = thread_state_sync.MenuWatchdogLoop(bot, interval_seconds=60)  # no repo
    windows = [{"thread_id": "100", "session_name": "clord", "window_name": "w1"}]
    with (
        patch.object(thread_state_sync, "_list_all_windows", return_value=windows),
        patch.object(thread_state_sync, "_capture_pane_text", return_value="menu"),
        patch.object(loop, "_maybe_bridge_open_menu", new=AsyncMock()) as bridge,
    ):
        await loop.tick()
    bridge.assert_awaited_once()


# -- #510: a dead pane's leftover menu is not a live question -----------------
# After a reboot, tmux-resurrect restores the SHELL plus the saved screen
# contents ("cat <pane_contents>; exec zsh") — claude itself is not restored.
# The corpse's screen still parses as an open AskUserQuestion menu, so the
# watchdog bridged it, waited out the 24h ASK_ANSWER_TIMEOUT, sent Esc into
# zsh (a no-op), then re-bridged the same corpse — one @mention per day, for
# a question that had actually been answered two weeks earlier.


def _ghost_pane() -> str:
    """Real capture of the w122 corpse pane (menu text above a zsh prompt)."""
    return _fixture("ghost_menu_dead_pane.txt")


def test_ghost_pane_still_parses_as_a_menu():
    """The detector is NOT wrong — the corpse is textually indistinguishable.

    Pins why the fix is a liveness check and not a parser change: this pane
    yields the very menu Discord kept re-posting.
    """
    from c_lord.claude.tmux_runner import _normalize_capture, _parse_ask_from_pane

    question = _parse_ask_from_pane(_normalize_capture(_ghost_pane()))
    assert question is not None
    assert question.header == "次のアクション"
    assert [o.label for o in question.options] == [
        "Issue化して再現条件を記録する",
        "対策の設計に進む",
        "まず babeln さんに共有するだけ",
    ]


def _tmux_stub(pane_text: str, foreground: str):
    """subprocess.run stub: capture-pane → *pane_text*, pane command → *foreground*."""

    def _run(argv, *_a, **_kw):
        joined = " ".join(argv)
        stdout = pane_text if "capture-pane" in joined else f"{foreground}\n"
        return MagicMock(returncode=0, stdout=stdout, stderr="")

    return _run


@pytest.mark.asyncio
async def test_watchdog_skips_menu_in_pane_where_claude_is_dead():
    """#510 RED: zsh in the foreground → the menu is a leftover screen, not a question."""
    loop, bot, thread = _make_loop()
    pane = _ghost_pane()
    with (
        patch.object(thread_state_sync.subprocess, "run", _tmux_stub(pane, "zsh")),
        patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge,
    ):
        await loop._maybe_bridge_open_menu(510, "sess", "w122", pane)
        await asyncio.sleep(0)
    bridge.assert_not_awaited()
    assert 510 not in loop._ask_bridges


@pytest.mark.asyncio
async def test_watchdog_still_bridges_when_claude_is_running():
    """Guard against over-suppression: a live claude pane must still bridge."""
    loop, bot, thread = _make_loop()
    pane = _fixture("ask_rich_descriptions.txt")
    with (
        patch.object(thread_state_sync.subprocess, "run", _tmux_stub(pane, "claude")),
        patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge,
    ):
        await loop._maybe_bridge_open_menu(511, "sess", "w1", pane)
        await asyncio.sleep(0)
        task = loop._ask_bridges.get(511)
        assert task is not None
        await task
    bridge.assert_awaited_once()


@pytest.mark.asyncio
async def test_watchdog_bridges_when_foreground_command_is_unknown():
    """An unreadable pane command is UNKNOWN, never 'dead' (#485 philosophy).

    tmux missing / window mapping momentarily unresolved must not silence a
    real question — only a positively-read non-claude command suppresses.
    """
    loop, bot, thread = _make_loop()
    pane = _fixture("ask_rich_descriptions.txt")

    def _run(argv, *_a, **_kw):
        joined = " ".join(argv)
        if "capture-pane" in joined:
            return MagicMock(returncode=0, stdout=pane, stderr="")
        return MagicMock(returncode=1, stdout="", stderr="no such window")

    with (
        patch.object(thread_state_sync.subprocess, "run", _run),
        patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=AsyncMock()) as bridge,
    ):
        await loop._maybe_bridge_open_menu(512, "sess", "w1", pane)
        await asyncio.sleep(0)
        task = loop._ask_bridges.get(512)
        assert task is not None
        await task
    bridge.assert_awaited_once()


# ----------------------------------------------------------------------
# #579: a menu the bridge cannot post must not become an infinite retry.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_logs_a_failed_bridge_instead_of_swallowing_it(caplog):
    """AC4: the failure has to be readable.

    The bridge runs as a bare ``create_task`` nobody awaits, so a raise surfaced
    only as asyncio's ``Task exception was never retrieved`` — which is why 116
    failed posts in one day went unnoticed.
    """
    loop, bot, thread = _make_loop()
    pane = _fixture("ask_rich_descriptions.txt")
    boom = AsyncMock(side_effect=RuntimeError("400 Bad Request: label required"))
    with (
        patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
        patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=boom),
        caplog.at_level(logging.WARNING, logger="c_lord.thread_state_sync"),
    ):
        await loop._maybe_bridge_open_menu(579_101, "sess", "w1", pane)
        await asyncio.sleep(0)
        task = loop._ask_bridges.get(579_101)
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert any("400 Bad Request" in r.getMessage() or "bridge failed" in r.getMessage()
               for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_watchdog_gives_up_after_repeated_failures_and_says_so():
    """AC5: stop retrying a menu that cannot be posted, and say so in the thread.

    Without a cap the watchdog retried the same unpostable menu every sweep
    forever: the post failed, so the menu stayed unbridged, so the condition
    that triggers the sweep never cleared.
    """
    loop, bot, thread = _make_loop()
    thread.send = AsyncMock()
    pane = _fixture("ask_rich_descriptions.txt")
    boom = AsyncMock(side_effect=RuntimeError("400 Bad Request: label required"))

    with (
        patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
        patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=boom),
    ):
        for _ in range(thread_state_sync._ASK_BRIDGE_MAX_FAILURES + 3):
            await loop._maybe_bridge_open_menu(579_102, "sess", "w1", pane)
            await asyncio.sleep(0)
            task = loop._ask_bridges.get(579_102)
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
                loop._ask_bridges.pop(579_102, None)

    assert boom.await_count == thread_state_sync._ASK_BRIDGE_MAX_FAILURES, (
        f"retried {boom.await_count} times — the cap did not hold"
    )
    sent = " ".join(str(c.args) + str(c.kwargs) for c in thread.send.await_args_list)
    assert "選択肢" in sent, f"gave up silently: {thread.send.await_args_list}"


@pytest.mark.asyncio
async def test_a_successful_bridge_clears_the_failure_count():
    """A menu that posts fine must not inherit an earlier menu's failures."""
    loop, bot, thread = _make_loop()
    pane = _fixture("ask_rich_descriptions.txt")
    boom = AsyncMock(side_effect=RuntimeError("nope"))
    ok = AsyncMock()

    with patch.object(thread_state_sync, "_capture_pane_text", return_value=pane):
        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=boom):
            await loop._maybe_bridge_open_menu(579_103, "sess", "w1", pane)
            await asyncio.sleep(0)
            await asyncio.gather(loop._ask_bridges.pop(579_103), return_exceptions=True)
        with patch("c_lord.discord_ui.ask_handler.bridge_pane_ask", new=ok):
            await loop._maybe_bridge_open_menu(579_103, "sess", "w1", pane)
            await asyncio.sleep(0)
            await asyncio.gather(loop._ask_bridges.pop(579_103), return_exceptions=True)

    assert loop._ask_bridge_failures.get(579_103, 0) == 0
