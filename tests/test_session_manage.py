"""Tests for SessionManageCog: /sessions, /resume-info, /sync-sessions commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from c_lord.database.repository import SessionRecord


def _make_record(
    thread_id: int = 100,
    session_id: str = "abc-123",
    origin: str = "discord",
    summary: str | None = "Fix login bug",
    working_dir: str | None = "/home/user",
    model: str | None = "sonnet",
) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id=session_id,
        working_dir=working_dir,
        model=model,
        origin=origin,
        summary=summary,
        created_at="2026-02-19 10:00:00",
        last_used_at="2026-02-19 11:00:00",
    )


def _make_thread_interaction(thread_id: int = 12345) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    interaction.channel = thread
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_channel_interaction() -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_cog():
    from c_lord.cogs.session_manage import SessionManageCog

    bot = MagicMock()
    bot.channel_id = 999
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.list_all = AsyncMock(return_value=[])
    return SessionManageCog(bot=bot, repo=repo)


def _make_ctx(channel: MagicMock | None = None) -> MagicMock:
    """Return a mocked commands.Context for the !text twins."""
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.id = 1
    if channel is not None:
        ctx.channel = channel
    else:
        ctx.channel = MagicMock(spec=discord.TextChannel)
    return ctx


def _make_thread_ctx(thread_id: int = 12345) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    return _make_ctx(channel=thread)


class TestReadonlyTextTwins:
    """!text twins of the read-only/info slash commands (E2E-invokable, #209)."""

    async def test_model_show_text(self):
        cog = _make_cog()
        ctx = _make_ctx()
        await cog.model_show_text.callback(cog, ctx)
        ctx.send.assert_called_once()
        assert ctx.send.call_args.kwargs.get("embed") is not None

    async def test_tmux_list_text_no_bindings(self):
        cog = _make_cog()
        ctx = _make_ctx()
        await cog.tmux_list_text.callback(cog, ctx)
        ctx.send.assert_called_once()
        assert "clord-init" in ctx.send.call_args.args[0]


def _sdir(thread_id: int, path: str = "/x"):
    from types import SimpleNamespace

    return SimpleNamespace(
        path=f"{path}/{thread_id}",
        thread_id=thread_id,
        source_repo="https://github.com/yousan/c-lord.git",
        commit="abc1234",
        is_clean=True,
    )


def _status_cog(*, dirs, windows, records):
    """A cog wired with mocked per-channel managers for /clord-status (#363)."""
    cog = _make_cog()
    sdm = MagicMock()
    sdm.find_session_dirs = MagicMock(return_value=dirs)
    tmux = MagicMock()
    tmux.session_name = "c-lord"
    tmux.list_sessions = MagicMock(return_value=windows)
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
    cog.repo.get = AsyncMock(side_effect=lambda tid: records.get(tid))
    cog.repo.list_all = AsyncMock(return_value=list(records.values()))
    return cog


class TestClordStatus:
    """/clord-status — per-channel session view that supersedes the old 3 (#363)."""

    async def test_no_binding_guides_to_init_without_hanging(self):
        cog = _make_cog()
        cog._resolve_session_dir_manager = AsyncMock(return_value=None)
        cog._resolve_tmux_manager = AsyncMock(return_value=None)
        ctx = _make_ctx()
        ctx.channel.id = 777
        await cog.clord_status_text.callback(cog, ctx, None)
        ctx.send.assert_called_once()
        assert "clord-init" in ctx.send.call_args.args[0]

    async def test_lists_live_session(self, monkeypatch):
        import c_lord.cogs.session_manage as sm

        monkeypatch.setattr(sm, "_dir_size_bytes", lambda _p: 412_000_000)
        cog = _status_cog(
            dirs=[_sdir(100)],
            windows=[{"window_name": "work1", "working_dir": "/x/100", "thread_id": "100"}],
            records={100: _make_record(thread_id=100, session_id="tmux-x", summary="auth-bug")},
        )
        ctx = _make_ctx()
        ctx.channel.id = 777
        ctx.channel.name = "dev-claude"
        await cog.clord_status_text.callback(cog, ctx, None)
        sent = ctx.send.call_args.args[0]
        assert "c-lord status" in sent
        assert "auth-bug" in sent
        assert "1 active" in sent
        assert "tmux attach -t c-lord:work<#>" in sent

    async def test_closed_session_hidden_by_default_shown_in_all(self, monkeypatch):
        import c_lord.cogs.session_manage as sm

        monkeypatch.setattr(sm, "_dir_size_bytes", lambda _p: 96_000_000)
        # dir exists but no tmux window -> closed
        records = {200: _make_record(thread_id=200, session_id="tmux-y", summary="old-refactor")}

        cog = _status_cog(dirs=[_sdir(200)], windows=[], records=records)
        ctx = _make_ctx()
        ctx.channel.id = 777
        await cog.clord_status_text.callback(cog, ctx, None)  # default
        default_out = ctx.send.call_args.args[0]
        assert "old-refactor" not in default_out  # hidden by default
        assert "1 closed" in default_out  # but surfaced in the footer

        cog2 = _status_cog(dirs=[_sdir(200)], windows=[], records=records)
        ctx2 = _make_ctx()
        ctx2.channel.id = 777
        await cog2.clord_status_text.callback(cog2, ctx2, "all")  # !clord-status all
        all_out = ctx2.send.call_args.args[0]
        assert "old-refactor" in all_out
        assert "closed" in all_out


class TestModelSetTextTwin:
    """!model-set mirrors /model set (E2E-invokable, #209)."""

    async def test_missing_model_shows_usage(self):
        cog = _make_cog()
        ctx = _make_ctx()
        await cog.model_set_text.callback(cog, ctx, model=None)
        ctx.send.assert_called_once()
        assert "Usage" in ctx.send.call_args.args[0]

    async def test_invalid_model(self):
        cog = _make_cog()
        ctx = _make_ctx()
        await cog.model_set_text.callback(cog, ctx, model="no pe")  # space → malformed
        assert "Invalid" in ctx.send.call_args.args[0]

    async def test_valid_model_persisted(self):
        cog = _make_cog()
        cog.settings_repo = MagicMock()
        cog.settings_repo.set = AsyncMock()
        ctx = _make_ctx()
        await cog.model_set_text.callback(cog, ctx, model="opus")
        cog.settings_repo.set.assert_called_once()
        assert cog.settings_repo.set.call_args.args[1] == "opus"
        assert ctx.send.call_args.kwargs.get("embed") is not None


class TestOpsTextTwins:
    """!session-cleanup / !workspace-delete (destructive, E2E-invokable, #209)."""

    async def test_session_cleanup_text_no_bindings(self):
        cog = _make_cog()  # bot.get_cog → non-ChannelRepoCog ⇒ no bindings
        ctx = _make_ctx()
        await cog.session_cleanup_text.callback(cog, ctx, arg=None)
        ctx.send.assert_called_once()
        assert "clord-init" in ctx.send.call_args.args[0]

    async def test_workspace_delete_text_outside_thread(self):
        cog = _make_cog()
        ctx = _make_ctx()  # not a thread
        await cog.workspace_delete_text.callback(cog, ctx)
        ctx.send.assert_called_once()
        assert "thread" in ctx.send.call_args.args[0].lower()

    async def test_workspace_delete_text_in_thread(self):
        cog = _make_cog()
        cog._resolve_tmux_manager = AsyncMock(return_value=None)
        cog._resolve_session_dir_manager = AsyncMock(return_value=None)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 999
        ctx = _make_ctx(channel=thread)
        await cog.workspace_delete_text.callback(cog, ctx)
        # Unbound → an embed explaining nothing to delete / clord-init hint
        assert ctx.send.call_args.kwargs.get("embed") is not None


class TestCloseWorkspace:
    """#271: /close-workspace — non-destructive twin of /workspace-delete.

    Kills the tmux window and archives the thread to declutter, but KEEPS the
    session directory + transcript + DB record so the next message resumes the
    conversation via --continue (#270).
    """

    async def test_close_text_outside_thread(self):
        cog = _make_cog()
        ctx = _make_ctx()  # not a thread
        await cog.close_workspace_text.callback(cog, ctx)
        ctx.send.assert_called_once()
        assert "thread" in ctx.send.call_args.args[0].lower()

    async def test_close_kills_window_but_keeps_session_dir(self):
        """Core (#271): kill_session IS called, cleanup_for_thread is NOT (dir kept)."""
        cog = _make_cog()
        tmux_mgr = MagicMock()
        tmux_mgr.kill_session = MagicMock(return_value=True)
        sdm = MagicMock()
        sdm.cleanup_for_thread = MagicMock()
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)
        cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 999
        thread.edit = AsyncMock()
        ctx = _make_ctx(channel=thread)

        await cog.close_workspace_text.callback(cog, ctx)

        tmux_mgr.kill_session.assert_called_once_with(555)
        sdm.cleanup_for_thread.assert_not_called()  # ← dir/transcript MUST survive

    async def test_close_archives_thread(self):
        """#271: the thread is archived to declutter the sidebar (the 'tidy up' goal)."""
        cog = _make_cog()
        tmux_mgr = MagicMock()
        tmux_mgr.kill_session = MagicMock(return_value=True)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)
        cog._resolve_session_dir_manager = AsyncMock(return_value=None)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 999
        thread.edit = AsyncMock()
        ctx = _make_ctx(channel=thread)

        await cog.close_workspace_text.callback(cog, ctx)

        # Two edits since #512: the "[終了]" rename, then the archive.
        assert any(c.kwargs.get("archived") is True for c in thread.edit.call_args_list)

    async def test_close_stops_mirror_before_archiving(self):
        """#379: close-workspace MUST stop the TranscriptMirror before archiving.

        Otherwise the mirror keeps tailing the JSONL and posts a ``👤``
        echo (e.g. the ``<task-notification>`` emitted when close-workspace's
        own ``kill_session`` stops a background task) *after* the archive,
        which makes Discord auto-unarchive the thread — so it never closes.
        """
        cog = _make_cog()
        tmux_mgr = MagicMock()
        tmux_mgr.kill_session = MagicMock(return_value=True)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)
        cog._resolve_session_dir_manager = AsyncMock(return_value=None)

        # TranscriptMirrorCog stub with an async stop_for.
        mirror_cog = MagicMock()
        mirror_cog.stop_for = AsyncMock()
        cog.bot.get_cog = MagicMock(return_value=mirror_cog)

        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 999
        thread.edit = AsyncMock()

        # Record global call order across stop_for and the thread edits.  Since
        # #512 there are two edits (the "[終了]" rename, then the archive), so the
        # invariant under test is the *position* of stop_for, not the edit count.
        order: list[str] = []
        mirror_cog.stop_for.side_effect = lambda *a, **k: order.append("stop_for")
        thread.edit.side_effect = lambda *a, **k: order.append(
            "archive" if k.get("archived") else "rename"
        )

        ctx = _make_ctx(channel=thread)
        await cog.close_workspace_text.callback(cog, ctx)

        # Looked up by the canonical cog name.
        cog.bot.get_cog.assert_any_call("TranscriptMirrorCog")
        # Stopped for this exact thread.
        mirror_cog.stop_for.assert_awaited_once_with(555)
        # And stopped BEFORE the archive, so no echo can land post-archive.
        assert order.index("stop_for") < order.index("archive")

    async def test_close_works_when_mirror_cog_absent(self):
        """#379 zero-config: no TranscriptMirrorCog (bridge OFF) → no crash, still archives."""
        cog = _make_cog()
        tmux_mgr = MagicMock()
        tmux_mgr.kill_session = MagicMock(return_value=True)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)
        cog._resolve_session_dir_manager = AsyncMock(return_value=None)
        cog.bot.get_cog = MagicMock(return_value=None)  # cog not registered

        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 999
        thread.edit = AsyncMock()
        ctx = _make_ctx(channel=thread)

        await cog.close_workspace_text.callback(cog, ctx)

        assert any(c.kwargs.get("archived") is True for c in thread.edit.call_args_list)

    async def test_workspace_delete_stops_mirror(self):
        """#379: workspace-delete also tears the mirror down so it doesn't tail a removed JSONL."""
        cog = _make_cog()
        tmux_mgr = MagicMock()
        tmux_mgr.kill_session = MagicMock(return_value=True)
        sdm = MagicMock()
        sdm.cleanup_for_thread = MagicMock(
            return_value=MagicMock(removed=True, path="/tmp/x", reason="")
        )
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)
        cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)

        mirror_cog = MagicMock()
        mirror_cog.stop_for = AsyncMock()
        cog.bot.get_cog = MagicMock(return_value=mirror_cog)

        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 999
        ctx = _make_ctx(channel=thread)

        await cog.workspace_delete_text.callback(cog, ctx)

        mirror_cog.stop_for.assert_awaited_once_with(555)


class TestThreadArchiveCommand:
    """/thread-archive show + set and their !text twins."""

    async def test_show_text_default(self):
        cog = _make_cog()
        cog.settings_repo = MagicMock()
        cog.settings_repo.get = AsyncMock(return_value=None)
        ctx = _make_ctx()
        await cog.thread_archive_show_text.callback(cog, ctx)
        embed = ctx.send.call_args.kwargs.get("embed")
        assert embed is not None
        # Default is 3 days (4320 min) when nothing is stored.
        assert "4320" in embed.description or "3" in embed.description

    async def test_set_text_invalid_duration(self):
        cog = _make_cog()
        cog.settings_repo = MagicMock()
        cog.settings_repo.set = AsyncMock()
        ctx = _make_ctx()
        await cog.thread_archive_set_text.callback(cog, ctx, duration="999")
        cog.settings_repo.set.assert_not_called()
        assert "999" in ctx.send.call_args.args[0] or "Invalid" in ctx.send.call_args.args[0]

    async def test_set_text_non_numeric(self):
        cog = _make_cog()
        cog.settings_repo = MagicMock()
        cog.settings_repo.set = AsyncMock()
        ctx = _make_ctx()
        await cog.thread_archive_set_text.callback(cog, ctx, duration="garbage")
        cog.settings_repo.set.assert_not_called()

    async def test_set_text_missing_shows_usage(self):
        cog = _make_cog()
        ctx = _make_ctx()
        await cog.thread_archive_set_text.callback(cog, ctx, duration=None)
        ctx.send.assert_called_once()
        assert "Usage" in ctx.send.call_args.args[0]

    async def test_set_text_valid_persisted(self):
        cog = _make_cog()
        cog.settings_repo = MagicMock()
        cog.settings_repo.set = AsyncMock()
        ctx = _make_ctx()
        await cog.thread_archive_set_text.callback(cog, ctx, duration="10080")
        cog.settings_repo.set.assert_called_once()
        assert cog.settings_repo.set.call_args.args[1] == "10080"
        assert ctx.send.call_args.kwargs.get("embed") is not None


def _capture_responder():
    """A (respond, ack, sent) triple that records every respond() call."""
    sent: list[dict] = []

    async def respond(content=None, *, embed=None, file=None, ephemeral=False):
        sent.append({"content": content, "embed": embed, "file": file, "ephemeral": ephemeral})

    async def ack(*, ephemeral=False):
        return None

    return respond, ack, sent


class TestTmuxScreenshot:
    async def test_outside_thread_sends_ephemeral(self):
        cog = _make_cog()
        interaction = _make_channel_interaction()
        await cog.tmux_screenshot.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        assert call_args.kwargs.get("ephemeral") is True

    async def test_impl_sends_png_file(self, monkeypatch):
        import c_lord.cogs.session_manage as sm

        cog = _make_cog()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 123
        thread.parent_id = 456

        tmux_mgr = MagicMock()
        tmux_mgr.session_name = "c-lord"
        tmux_mgr._find_window_for_thread = MagicMock(return_value="work1")
        tmux_mgr.capture_screen = MagicMock(return_value="\x1b[31mhi\x1b[0m")
        tmux_mgr.list_window_tabs = MagicMock(return_value=[(1, "work1", True)])
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)

        captured = {}

        def fake_render(text, status_bar=None):
            captured["status_bar"] = status_bar
            return b"\x89PNG\r\n\x1a\nDATA"

        monkeypatch.setattr(sm, "render_pane_png", fake_render)

        respond, ack, sent = _capture_responder()
        await cog._screenshot_impl(channel=thread, respond=respond, ack=ack)

        files = [s["file"] for s in sent if s["file"] is not None]
        assert len(files) == 1
        assert isinstance(files[0], discord.File)
        assert files[0].filename == "tmux-c-lord-work1.png"
        tmux_mgr.capture_screen.assert_called_once_with(123)
        # Status bar (session, tabs, current window) is passed to the renderer.
        assert captured["status_bar"] is not None
        assert captured["status_bar"][0] == "c-lord"
        assert captured["status_bar"][2] == "work1"

    async def test_impl_no_window_sends_ephemeral(self, monkeypatch):
        cog = _make_cog()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 123
        thread.parent_id = 456

        tmux_mgr = MagicMock()
        tmux_mgr._find_window_for_thread = MagicMock(return_value=None)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)

        respond, ack, sent = _capture_responder()
        await cog._screenshot_impl(channel=thread, respond=respond, ack=ack)

        assert sent and sent[-1]["ephemeral"] is True
        assert all(s["file"] is None for s in sent)

    async def test_impl_no_window_gives_actionable_recovery_hint(self):
        """#464 ②-2: a stopped session must not dead-end with a bare 'No tmux
        window found for this thread.' — tell the user that sending a message
        auto-restores it (the #270/#465 dead-pane recovery)."""
        cog = _make_cog()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 123
        thread.parent_id = 456

        tmux_mgr = MagicMock()
        tmux_mgr._find_window_for_thread = MagicMock(return_value=None)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)

        respond, ack, sent = _capture_responder()
        await cog._screenshot_impl(channel=thread, respond=respond, ack=ack)

        msg = sent[-1]["content"] or ""
        assert "復元" in msg or "メッセージを送" in msg, msg
        assert msg != "ℹ️ No tmux window found for this thread."

    async def test_impl_render_unavailable_sends_ephemeral(self, monkeypatch):
        import c_lord.cogs.session_manage as sm

        cog = _make_cog()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 123
        thread.parent_id = 456

        tmux_mgr = MagicMock()
        tmux_mgr.session_name = "c-lord"
        tmux_mgr._find_window_for_thread = MagicMock(return_value="work1")
        tmux_mgr.capture_screen = MagicMock(return_value="hi")
        tmux_mgr.list_window_tabs = MagicMock(return_value=[(1, "work1", True)])
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)
        monkeypatch.setattr(sm, "render_pane_png", lambda text, status_bar=None: None)

        respond, ack, sent = _capture_responder()
        await cog._screenshot_impl(channel=thread, respond=respond, ack=ack)

        assert sent and sent[-1]["ephemeral"] is True
        assert all(s["file"] is None for s in sent)
        # Hint at the optional extra so consumers know how to enable it.
        assert "c-lord[table]" in (sent[-1]["content"] or "")
