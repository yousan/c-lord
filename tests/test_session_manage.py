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


class TestResumeInfo:
    async def test_outside_thread_sends_ephemeral(self):
        cog = _make_cog()
        interaction = _make_channel_interaction()
        await cog.resume_info.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        assert call_args.kwargs.get("ephemeral") is True

    async def test_no_session_sends_ephemeral(self):
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=None)
        interaction = _make_thread_interaction(thread_id=555)
        await cog.resume_info.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        assert call_args.kwargs.get("ephemeral") is True

    async def test_shows_resume_command(self):
        cog = _make_cog()
        record = _make_record(thread_id=555, session_id="def-456")
        cog.repo.get = AsyncMock(return_value=record)
        interaction = _make_thread_interaction(thread_id=555)
        await cog.resume_info.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        # Should contain an embed with the resume command
        embed = call_args.kwargs.get("embed")
        assert embed is not None
        assert "def-456" in embed.description


class TestSessionsList:
    async def test_empty_sessions(self):
        cog = _make_cog()
        cog.repo.list_all = AsyncMock(return_value=[])
        interaction = _make_channel_interaction()
        await cog.sessions_list.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        # Should send something indicating no sessions
        embed = call_args.kwargs.get("embed")
        assert embed is not None

    async def test_shows_sessions(self):
        cog = _make_cog()
        records = [
            _make_record(thread_id=100, session_id="aaa", origin="discord", summary="First task"),
            _make_record(thread_id=101, session_id="bbb", origin="cli", summary="CLI task"),
        ]
        cog.repo.list_all = AsyncMock(return_value=records)
        interaction = _make_channel_interaction()
        await cog.sessions_list.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        embed = call_args.kwargs.get("embed")
        assert embed is not None
        assert len(embed.fields) == 2

    async def test_session_origin_icons(self):
        cog = _make_cog()
        records = [
            _make_record(session_id="d1", origin="discord", summary="Discord session"),
            _make_record(session_id="c1", origin="cli", summary="CLI session", thread_id=101),
        ]
        cog.repo.list_all = AsyncMock(return_value=records)
        interaction = _make_channel_interaction()
        await cog.sessions_list.callback(cog, interaction)
        embed = interaction.response.send_message.call_args.kwargs["embed"]
        # Discord sessions show 💬, CLI sessions show 🖥️
        assert "\U0001f4ac" in embed.fields[0].name  # 💬
        assert "\U0001f5a5" in embed.fields[1].name  # 🖥️

    async def test_tmux_session_shows_window_name(self):
        """tmux sessions should display window name (e.g. work3) instead of tmux-147..."""
        cog = _make_cog()
        records = [
            _make_record(
                thread_id=200,
                session_id="tmux-200",
                origin="discord",
                summary="Tmux task",
            ),
        ]
        cog.repo.list_all = AsyncMock(return_value=records)

        # Mock tmux manager to return window name mapping
        tmux_mgr = MagicMock()
        tmux_mgr._thread_to_window = {200: "work3"}
        tmux_mgr._rebuild_mapping = MagicMock()
        cog._resolve_all_tmux_managers = AsyncMock(return_value=[tmux_mgr])

        interaction = _make_channel_interaction()
        await cog.sessions_list.callback(cog, interaction)
        embed = interaction.response.send_message.call_args.kwargs["embed"]
        assert len(embed.fields) == 1
        # Should show window name, not tmux-200
        assert "work3" in embed.fields[0].value
        assert "tmux-200" not in embed.fields[0].value

    async def test_tmux_session_no_window_shows_tmux(self):
        """tmux sessions with no live window should show 'tmux'."""
        cog = _make_cog()
        records = [
            _make_record(
                thread_id=200,
                session_id="tmux-200",
                origin="discord",
                summary="Tmux task",
            ),
        ]
        cog.repo.list_all = AsyncMock(return_value=records)

        # No tmux managers available
        cog._resolve_all_tmux_managers = AsyncMock(return_value=[])

        interaction = _make_channel_interaction()
        await cog.sessions_list.callback(cog, interaction)
        embed = interaction.response.send_message.call_args.kwargs["embed"]
        assert "`tmux`" in embed.fields[0].value

    async def test_cli_session_shows_short_id(self):
        """Non-tmux sessions should display truncated session ID as before."""
        cog = _make_cog()
        records = [
            _make_record(session_id="abcdef12-3456-7890", origin="discord", summary="CLI task"),
        ]
        cog.repo.list_all = AsyncMock(return_value=records)
        interaction = _make_channel_interaction()
        await cog.sessions_list.callback(cog, interaction)
        embed = interaction.response.send_message.call_args.kwargs["embed"]
        assert "`abcdef12...`" in embed.fields[0].value


class TestResumeInfoTmux:
    """Tests for /resume-info with tmux sessions."""

    async def test_tmux_session_shows_tmux_attach(self):
        """tmux sessions should show tmux attach command, not claude --resume."""
        cog = _make_cog()
        record = _make_record(
            thread_id=555,
            session_id="tmux-1477909096400294011",
        )
        cog.repo.get = AsyncMock(return_value=record)
        interaction = _make_thread_interaction(thread_id=555)
        await cog.resume_info.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        embed = call_args.kwargs.get("embed")
        assert embed is not None
        # Should NOT contain claude --resume for tmux sessions
        assert "claude --resume" not in embed.description
        # Should contain tmux attach guidance
        assert "tmux" in embed.description

    async def test_cli_session_shows_claude_resume(self):
        """Non-tmux sessions should still show claude --resume."""
        cog = _make_cog()
        record = _make_record(thread_id=555, session_id="def-456")
        cog.repo.get = AsyncMock(return_value=record)
        interaction = _make_thread_interaction(thread_id=555)
        await cog.resume_info.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        embed = call_args.kwargs.get("embed")
        assert embed is not None
        assert "claude --resume def-456" in embed.description


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

    async def test_resume_info_text_outside_thread(self):
        cog = _make_cog()
        ctx = _make_ctx()  # not a thread
        await cog.resume_info_text.callback(cog, ctx)
        ctx.send.assert_called_once()
        assert "thread" in ctx.send.call_args.args[0].lower()

    async def test_resume_info_text_shows_command(self):
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=_make_record(thread_id=555, session_id="def-456"))
        ctx = _make_thread_ctx(thread_id=555)
        await cog.resume_info_text.callback(cog, ctx)
        embed = ctx.send.call_args.kwargs.get("embed")
        assert embed is not None
        assert "def-456" in embed.description

    async def test_sessions_text_empty(self):
        cog = _make_cog()
        cog.repo.list_all = AsyncMock(return_value=[])
        ctx = _make_ctx()
        await cog.sessions_list_text.callback(cog, ctx)
        assert ctx.send.call_args.kwargs.get("embed") is not None

    async def test_session_dirs_text_no_bindings(self):
        cog = _make_cog()  # bot.get_cog returns a non-ChannelRepoCog mock → no bindings
        ctx = _make_ctx()
        await cog.session_dirs_list_text.callback(cog, ctx)
        ctx.send.assert_called_once()
        assert "clord-init" in ctx.send.call_args.args[0]

    async def test_tmux_list_text_no_bindings(self):
        cog = _make_cog()
        ctx = _make_ctx()
        await cog.tmux_list_text.callback(cog, ctx)
        ctx.send.assert_called_once()
        assert "clord-init" in ctx.send.call_args.args[0]


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
        await cog.model_set_text.callback(cog, ctx, model="nope")
        assert "Unknown model" in ctx.send.call_args.args[0]

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

        thread.edit.assert_called_once()
        assert thread.edit.call_args.kwargs.get("archived") is True

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

        # Record global call order across stop_for and the archive edit.
        order: list[str] = []
        mirror_cog.stop_for.side_effect = lambda *a, **k: order.append("stop_for")
        thread.edit.side_effect = lambda *a, **k: order.append("edit")

        ctx = _make_ctx(channel=thread)
        await cog.close_workspace_text.callback(cog, ctx)

        # Looked up by the canonical cog name.
        cog.bot.get_cog.assert_any_call("TranscriptMirrorCog")
        # Stopped for this exact thread.
        mirror_cog.stop_for.assert_awaited_once_with(555)
        # And stopped BEFORE the archive, so no echo can land post-archive.
        assert order == ["stop_for", "edit"]

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

        thread.edit.assert_called_once()
        assert thread.edit.call_args.kwargs.get("archived") is True

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


class TestHelperFunctions:
    """Tests for _is_tmux_session and _format_session_short."""

    def test_is_tmux_session_true(self):
        from c_lord.cogs.session_manage import _is_tmux_session

        assert _is_tmux_session("tmux-1477909096400294011") is True

    def test_is_tmux_session_false(self):
        from c_lord.cogs.session_manage import _is_tmux_session

        assert _is_tmux_session("abcdef12-3456-7890") is False

    def test_is_tmux_session_empty(self):
        from c_lord.cogs.session_manage import _is_tmux_session

        assert _is_tmux_session("") is False

    def test_format_session_short_tmux_without_window(self):
        from c_lord.cogs.session_manage import _format_session_short

        assert _format_session_short("tmux-1477909096400294011") == "tmux"

    def test_format_session_short_tmux_with_window(self):
        from c_lord.cogs.session_manage import _format_session_short

        assert _format_session_short("tmux-1477909096400294011", window_name="work3") == "work3"

    def test_format_session_short_cli(self):
        from c_lord.cogs.session_manage import _format_session_short

        assert _format_session_short("abcdef12-3456-7890") == "abcdef12"


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
