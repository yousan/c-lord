"""Tests for /resync — reconnect the Discord mirror to tmux (#439).

/resync is a user-facing safety valve: when the tmux→Discord mirror feels out
of sync (a menu's buttons never showed, an embed looks stale), the user runs it
to (a) re-bridge any stranded TUI menu and (b) post a fresh pane snapshot —
without waiting for the 60s watchdog sweep or a bot restart.

Thread-scoped only — the channel-wide twin was removed in #619.

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
        await cog._resync_impl(channel=thread, respond=respond, ack=ack)

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
        await cog._resync_impl(channel=thread, respond=respond, ack=ack)

        watchdog._maybe_bridge_open_menu.assert_not_awaited()
        cog._snapshot_pane.assert_not_awaited()
        assert respond.await_count >= 1

    @pytest.mark.asyncio
    async def test_thread_no_window_gives_actionable_recovery_hint(self, monkeypatch):
        """#464 ②-2: /resync on a stopped session must guide the user to restore
        it (send a message) instead of dead-ending with a bare 'No tmux window
        found for this thread.' — that bare message is what left the user stuck
        during the 2026-06-25 incident."""
        cog, bot, watchdog = _make_cog()
        thread = _make_thread(12345)
        monkeypatch.setattr(tss, "_list_all_windows", lambda: [])
        cog._snapshot_pane = AsyncMock(return_value=b"PNGDATA")

        respond, ack = _io()
        await cog._resync_impl(channel=thread, respond=respond, ack=ack)

        texts = []
        for a, k in respond.await_args_list:
            if a:
                texts.append(a[0])
            if k.get("content"):
                texts.append(k["content"])
        joined = " ".join(t for t in texts if isinstance(t, str))
        assert "復元" in joined or "メッセージを送" in joined, joined
        assert joined != "ℹ️ No tmux window found for this thread."

    @pytest.mark.asyncio
    async def test_thread_scope_outside_thread_is_rejected(self):
        cog, bot, watchdog = _make_cog()
        channel = MagicMock(spec=discord.TextChannel)

        respond, ack = _io()
        await cog._resync_impl(channel=channel, respond=respond, ack=ack)

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
        await cog._resync_impl(channel=thread, respond=respond, ack=ack)
        assert any(kwargs.get("file") is not None for _args, kwargs in respond.await_args_list)


class TestResyncChannelRemoved:
    """``/resync-channel`` was removed in #619.

    It claimed to reconnect "every thread in this channel" but only ever swept
    **one** tmux session, so threads bound to another repo were silently left
    out — and the 60s menu watchdog already does the same job across *all*
    sessions, automatically. A manual command that is a narrower version of an
    automatic one is worse than no command.
    """

    def test_slash_command_is_gone(self) -> None:
        cog, _bot, _watchdog = _make_cog()
        names = {c.name for c in cog.get_app_commands()}
        assert "resync-channel" not in names
        assert "resync" in names, "the thread-scoped command must survive"

    def test_text_twin_is_gone(self) -> None:
        cog, _bot, _watchdog = _make_cog()
        names = {c.name for c in cog.get_commands()}
        assert "resync-channel" not in names
        assert "resync" in names, "the thread-scoped text twin must survive"

    def test_channel_scope_helper_is_gone(self) -> None:
        """The dead branch and its wrong-premise helper must not linger (AC4)."""
        from c_lord.cogs.session_manage import SessionManageCog

        assert not hasattr(SessionManageCog, "_resolve_channel_session")
        assert not hasattr(SessionManageCog, "resync_channel")
