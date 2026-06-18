"""Tests for /resync — reconnect the Discord mirror to tmux (#439).

/resync is a user-facing safety valve: when the tmux→Discord mirror feels out
of sync (a menu's buttons never showed, an embed looks stale), the user runs it
to (a) re-bridge any stranded TUI menu and (b) post a fresh pane snapshot —
without waiting for the 60s watchdog sweep or a bot restart.

Two scopes:
- ``thread``  — just the invoking thread.
- ``channel`` — every thread window in this channel's tmux session.

These tests drive ``SessionManageCog._resync_impl`` directly with mocks (the
slash/text commands are thin wrappers over it, same pattern as the other
session-manage commands).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import c_lord.thread_state_sync as tss


def _make_cog():
    from c_lord.cogs.session_manage import SessionManageCog

    bot = MagicMock()
    # Wired by setup.py (bot.menu_watchdog = MenuWatchdogLoop(...)).
    watchdog = MagicMock()
    watchdog._maybe_bridge_open_menu = AsyncMock()
    bot.menu_watchdog = watchdog
    repo = MagicMock()
    cog = SessionManageCog(bot=bot, repo=repo)
    return cog, bot, watchdog


def _make_thread(thread_id: int = 12345, parent_id: int = 999):
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    return thread


def _io():
    """Return (respond, ack) AsyncMocks plus the respond mock for assertions."""
    respond = AsyncMock()
    ack = AsyncMock()
    return respond, ack


class TestResyncThread:
    @pytest.mark.asyncio
    async def test_thread_rebridges_menu_and_posts_snapshot(self, monkeypatch):
        cog, bot, watchdog = _make_cog()
        thread = _make_thread(12345)

        monkeypatch.setattr(
            tss,
            "_list_all_windows",
            lambda: [
                {"thread_id": "12345", "session_name": "c-lord", "window_name": "work1"},
            ],
        )
        monkeypatch.setattr(tss, "_capture_pane_text", lambda *a, **k: "pane body")
        # Snapshot rendering is exercised separately; stub it to a fake PNG here.
        cog._snapshot_pane = AsyncMock(return_value=b"PNGDATA")

        respond, ack = _io()
        await cog._resync_impl(channel=thread, scope="thread", respond=respond, ack=ack)

        # Re-bridged the stranded menu for this exact thread/session/window.
        watchdog._maybe_bridge_open_menu.assert_awaited_once_with(
            12345, "c-lord", "work1", "pane body"
        )
        # Posted a snapshot file back to the thread.
        assert respond.await_count >= 1
        assert any(kwargs.get("file") is not None for _args, kwargs in respond.await_args_list), (
            "expected a snapshot file in the reply"
        )

    @pytest.mark.asyncio
    async def test_thread_no_window_gives_up_without_bridging(self, monkeypatch):
        cog, bot, watchdog = _make_cog()
        thread = _make_thread(12345)
        monkeypatch.setattr(tss, "_list_all_windows", lambda: [])
        cog._snapshot_pane = AsyncMock(return_value=b"PNGDATA")

        respond, ack = _io()
        await cog._resync_impl(channel=thread, scope="thread", respond=respond, ack=ack)

        watchdog._maybe_bridge_open_menu.assert_not_awaited()
        cog._snapshot_pane.assert_not_awaited()
        assert respond.await_count >= 1

    @pytest.mark.asyncio
    async def test_thread_scope_outside_thread_is_rejected(self):
        cog, bot, watchdog = _make_cog()
        channel = MagicMock(spec=discord.TextChannel)

        respond, ack = _io()
        await cog._resync_impl(channel=channel, scope="thread", respond=respond, ack=ack)

        watchdog._maybe_bridge_open_menu.assert_not_awaited()
        # Rejected with an ephemeral hint.
        assert respond.await_count == 1
        _args, kwargs = respond.await_args_list[0]
        assert kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_thread_survives_missing_watchdog(self, monkeypatch):
        cog, bot, _watchdog = _make_cog()
        bot.menu_watchdog = None  # not wired
        thread = _make_thread(12345)
        monkeypatch.setattr(
            tss,
            "_list_all_windows",
            lambda: [{"thread_id": "12345", "session_name": "c-lord", "window_name": "work1"}],
        )
        monkeypatch.setattr(tss, "_capture_pane_text", lambda *a, **k: "pane body")
        cog._snapshot_pane = AsyncMock(return_value=b"PNGDATA")

        respond, ack = _io()
        # Must not raise even with no watchdog; snapshot still posts.
        await cog._resync_impl(channel=thread, scope="thread", respond=respond, ack=ack)
        assert any(kwargs.get("file") is not None for _args, kwargs in respond.await_args_list)


class TestResyncChannel:
    @pytest.mark.asyncio
    async def test_channel_rebridges_only_this_channels_session(self, monkeypatch):
        cog, bot, watchdog = _make_cog()
        thread = _make_thread(12345, parent_id=999)

        # This channel resolves to session "c-lord"; windows in "other" must be skipped.
        mgr = MagicMock()
        mgr.session_name = "c-lord"
        cog._resolve_tmux_manager = AsyncMock(return_value=mgr)

        monkeypatch.setattr(
            tss,
            "_list_all_windows",
            lambda: [
                {"thread_id": "111", "session_name": "c-lord", "window_name": "work1"},
                {"thread_id": "222", "session_name": "c-lord", "window_name": "work2"},
                {"thread_id": "333", "session_name": "other", "window_name": "work1"},
                {"thread_id": "", "session_name": "c-lord", "window_name": "work3"},
            ],
        )
        monkeypatch.setattr(tss, "_capture_pane_text", lambda *a, **k: "pane body")

        respond, ack = _io()
        await cog._resync_impl(channel=thread, scope="channel", respond=respond, ack=ack)

        bridged_ids = {call.args[0] for call in watchdog._maybe_bridge_open_menu.await_args_list}
        assert bridged_ids == {111, 222}  # not 333 (other session), not "" (no thread)
        # Summary mentions the count.
        joined = " ".join(str(a) for c in respond.await_args_list for a in c.args)
        assert "2" in joined

    @pytest.mark.asyncio
    async def test_channel_no_session_reports_and_skips(self, monkeypatch):
        cog, bot, watchdog = _make_cog()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 999
        channel.parent_id = None
        cog._resolve_tmux_manager = AsyncMock(return_value=None)
        monkeypatch.setattr(tss, "_list_all_windows", lambda: [])

        respond, ack = _io()
        await cog._resync_impl(channel=channel, scope="channel", respond=respond, ack=ack)

        watchdog._maybe_bridge_open_menu.assert_not_awaited()
        assert respond.await_count >= 1
