"""Gap-filling tests for SessionManageCog (c_lord/cogs/session_manage.py).

Covers the paths tests/test_session_manage.py does not reach: the model
show/set fallbacks, /tmux-list, workspace stop/start/delete edges,
/clord-status plumbing, /resync and /tmux-screenshot corner cases.

Never touches a real tmux server: every TmuxSessionManager / subprocess is a
mock. Tests marked ``xfail(strict=True)`` assert the behaviour the docs /
comments promise and the code does not deliver (see each ``reason``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import c_lord.cogs.session_manage as sm
from c_lord.database.repository import SessionRecord

# ── helpers ────────────────────────────────────────────────────────────────


def _record(thread_id: int = 100, *, model: str | None = "sonnet", **kw) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id="abc-123",
        working_dir="/home/user",
        model=model,
        origin="discord",
        summary="Fix login bug",
        created_at="2026-02-19 10:00:00",
        last_used_at="2026-02-19 11:00:00",
        **kw,
    )


def _cog(**kwargs):
    from c_lord.cogs.session_manage import SessionManageCog

    bot = MagicMock()
    bot.channel_id = 999
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.list_all = AsyncMock(return_value=[])
    repo.set_closed = AsyncMock()
    repo.set_slept = AsyncMock()
    authorizer = MagicMock()
    authorizer.is_allowed = MagicMock(return_value=True)
    return SessionManageCog(bot=bot, repo=repo, authorizer=authorizer, **kwargs)


def _thread(thread_id: int = 555, parent_id: int | None = 999, name: str = "t") -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.name = name
    thread.edit = AsyncMock()
    thread.send = AsyncMock()
    return thread


def _text_channel(channel_id: int = 999) -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    return ch


def _capture():
    sent: list[dict] = []
    acks: list[dict] = []

    async def respond(content=None, *, embed=None, file=None, ephemeral=False, **kw):
        sent.append(
            {"content": content, "embed": embed, "file": file, "ephemeral": ephemeral, **kw}
        )

    async def ack(*, ephemeral=False):
        acks.append({"ephemeral": ephemeral})

    return respond, ack, sent, acks


def _ctx(channel=None) -> MagicMock:
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.channel = channel if channel is not None else _text_channel()
    return ctx


def _interaction(channel=None) -> MagicMock:
    it = MagicMock(spec=discord.Interaction)
    it.channel = channel if channel is not None else _text_channel()
    it.user = MagicMock()
    it.extras = {}
    it.response = MagicMock()
    it.response.send_message = AsyncMock()
    it.response.defer = AsyncMock()
    it.followup = MagicMock()
    it.followup.send = AsyncMock()
    return it


def _embed_text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    for f in embed.fields:
        parts += [f.name or "", f.value or ""]
    return "\n".join(parts)


# ── _dir_size_bytes ────────────────────────────────────────────────────────


def test_dir_size_uses_du_output(monkeypatch) -> None:
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="4096\t/some/path\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert sm._dir_size_bytes("/some/path") == 4096
    # ``--`` guards a path starting with '-' (never shell=True)
    assert calls == [["du", "-sb", "--", "/some/path"]]


def test_dir_size_falls_back_when_du_is_missing(tmp_path, monkeypatch) -> None:
    (tmp_path / "f").write_bytes(b"z" * 77)

    def missing(args, **kw):
        raise FileNotFoundError("du")

    monkeypatch.setattr(subprocess, "run", missing)
    assert sm._dir_size_bytes(str(tmp_path)) == 77


# ── model show / set (legacy embed path + #918 service path) ─────────────────


class TestModelShow:
    async def test_global_override_is_shown_and_drives_footer(self):
        settings = MagicMock()
        settings.get = AsyncMock(return_value="opus")
        cog = _cog(settings_repo=settings, runner=SimpleNamespace(model="sonnet"))
        cog._model_scopes = MagicMock(return_value=None)
        respond, _ack, sent, _ = _capture()

        await cog._model_show_impl(channel=_text_channel(), respond=respond)

        embed = sent[-1]["embed"]
        assert "Global override" in embed.description and "`opus`" in embed.description
        assert "runner default: `sonnet`" in embed.description
        assert embed.footer.text == "Effective model for new sessions: opus"

    async def test_no_settings_no_runner_falls_back_to_sonnet(self):
        cog = _cog()  # no settings_repo, ClaudeChatCog absent
        cog._model_scopes = MagicMock(return_value=None)
        respond, _ack, sent, _ = _capture()

        await cog._model_show_impl(channel=_text_channel(), respond=respond)

        embed = sent[-1]["embed"]
        assert "Default model:** `sonnet`" in embed.description
        assert embed.footer.text.endswith(": sonnet")

    async def test_runner_resolved_from_chat_cog(self):
        cog = _cog()
        cog.bot.get_cog = MagicMock(
            return_value=SimpleNamespace(runner=SimpleNamespace(model="haiku"))
        )
        cog._model_scopes = MagicMock(return_value=None)
        respond, _ack, sent, _ = _capture()

        await cog._model_show_impl(channel=_text_channel(), respond=respond)

        cog.bot.get_cog.assert_any_call("ClaudeChatCog")
        assert sent[-1]["embed"].footer.text.endswith(": haiku")

    async def test_thread_shows_its_last_session_model(self):
        cog = _cog(runner=SimpleNamespace(model="sonnet"))
        cog._model_scopes = MagicMock(return_value=None)
        cog.repo.get = AsyncMock(return_value=_record(555, model="opus"))
        respond, _ack, sent, _ = _capture()

        await cog._model_show_impl(channel=_thread(555), respond=respond)

        cog.repo.get.assert_awaited_once_with(555)
        fields = {f.name: f.value for f in sent[-1]["embed"].fields}
        assert fields == {"This thread's last session": "`opus`"}

    async def test_service_view_in_thread_uses_parent_channel(self, monkeypatch):
        cog = _cog()
        svc = MagicMock()
        svc.view = AsyncMock(return_value="VIEW")
        cog._model_scopes = MagicMock(return_value=svc)
        monkeypatch.setattr(sm, "render_view", lambda view, s: f"TABLE<{view}>")
        respond, _ack, sent, _ = _capture()

        await cog._model_show_impl(channel=_thread(555, parent_id=42), respond=respond)

        svc.view.assert_awaited_once_with(channel_id=42, thread_id=555)
        assert sent[-1]["embed"].description == "TABLE<VIEW>"

    async def test_service_view_in_channel_has_no_thread(self, monkeypatch):
        cog = _cog()
        svc = MagicMock()
        svc.view = AsyncMock(return_value="V")
        cog._model_scopes = MagicMock(return_value=svc)
        monkeypatch.setattr(sm, "render_view", lambda view, s: "T")
        respond, _ack, _sent, _ = _capture()

        await cog._model_show_impl(channel=_text_channel(77), respond=respond)

        svc.view.assert_awaited_once_with(channel_id=77, thread_id=None)


class TestModelSet:
    async def test_without_settings_repo_refuses_and_says_why(self):
        cog = _cog()
        respond, _ack, sent, _ = _capture()
        await cog._model_set_impl(model="opus", respond=respond)
        assert sent[-1]["ephemeral"] is True
        assert "unavailable" in sent[-1]["content"]


class TestModelCommand:
    async def test_channel_map_cache_then_fetch_then_unknown(self):
        cog = _cog()
        svc = MagicMock()
        svc.legacy_thread_ids = AsyncMock(return_value=[1, 2, 3, 4])
        cached = {1: SimpleNamespace(parent_id=10), 4: SimpleNamespace(parent_id="nope")}
        cog.bot.get_channel = MagicMock(side_effect=lambda tid: cached.get(tid))

        async def fetch(tid):
            if tid == 2:
                return SimpleNamespace(parent_id=20)
            raise discord.NotFound(MagicMock(status=404), "gone")

        cog.bot.fetch_channel = AsyncMock(side_effect=fetch)

        out = await cog._channel_map(svc)

        assert out == {1: 10, 2: 20, 3: None, 4: None}

    async def test_channel_without_an_id_is_refused_before_admit(self):
        cog = _cog()
        admit = AsyncMock(return_value=True)
        respond, _ack, sent, _ = _capture()
        await cog._model_command_impl(
            channel=SimpleNamespace(id=None),
            respond=respond,
            admit=admit,
            model="opus",
            effort=None,
            scope=None,
            include_existing=None,
        )
        admit.assert_not_awaited()
        assert sent[-1]["ephemeral"] is True
        assert "チャンネルかスレッド" in sent[-1]["content"]

    async def test_uppercase_inherit_is_normalised(self, monkeypatch):
        cog = _cog()
        svc = MagicMock()
        svc.change = AsyncMock(return_value="R")
        cog._model_scopes = MagicMock(return_value=svc)
        cog._channel_map = AsyncMock(return_value={})
        monkeypatch.setattr(sm, "render_result", lambda r: f"done:{r}")
        respond, _ack, sent, _ = _capture()

        await cog._model_command_impl(
            channel=_text_channel(77),
            respond=respond,
            admit=AsyncMock(return_value=True),
            model="INHERIT",
            effort=None,
            scope=None,
            include_existing=None,
        )

        kwargs = svc.change.await_args.kwargs
        assert kwargs["model"] == sm.INHERIT
        assert kwargs["scope"] == sm.SCOPE_CHANNEL  # default outside a thread
        assert kwargs["channel_map"] == {}
        assert sent[-1]["content"] == "done:R"

    async def test_text_include_existing_must_be_boolean(self):
        cog = _cog()
        cog._model_command_impl = AsyncMock()
        ctx = _ctx()
        await cog.model_text.callback(cog, ctx, "opus", "include-existing:maybe")
        cog._model_command_impl.assert_not_awaited()
        assert "true か false" in ctx.send.call_args.args[0]

    async def test_text_include_existing_true_is_passed_through(self):
        cog = _cog()
        cog._model_command_impl = AsyncMock()
        ctx = _ctx()
        await cog.model_text.callback(cog, ctx, "opus", "include_existing:TRUE", "scope:global")
        kw = cog._model_command_impl.await_args.kwargs
        assert (kw["model"], kw["include_existing"], kw["scope"]) == ("opus", True, "global")


# ── thread-archive / thread-rename ──────────────────────────────────────────


class TestThreadArchive:
    async def test_show_reports_a_configured_override(self):
        settings = MagicMock()
        settings.get = AsyncMock(return_value="10080")
        cog = _cog(settings_repo=settings)
        respond, _ack, sent, _ = _capture()
        await cog._archive_show_impl(respond=respond)
        assert sent[-1]["embed"].description == "**Configured:** 7 days (`10080` min)"

    async def test_set_without_settings_repo_refuses(self):
        cog = _cog()
        respond, _ack, sent, _ = _capture()
        await cog._archive_set_impl(duration=60, respond=respond)
        assert sent[-1]["ephemeral"] is True
        assert "unavailable" in sent[-1]["content"]

    async def test_slash_set_persists(self):
        settings = MagicMock()
        settings.set = AsyncMock()
        cog = _cog(settings_repo=settings)
        it = _interaction()
        await cog.thread_archive_set.callback(cog, it, 1440)
        settings.set.assert_awaited_once_with(sm.SETTING_THREAD_AUTO_ARCHIVE, "1440")


class TestThreadRename:
    async def test_outside_thread_refuses(self):
        cog = _cog()
        respond, ack, sent, acks = _capture()
        await cog._thread_rename_impl(channel=_text_channel(), respond=respond, ack=ack)
        assert sent[-1]["ephemeral"] is True
        assert acks == []

    async def test_in_thread_acks_then_posts_summary(self, monkeypatch):
        cog = _cog()
        rename = AsyncMock(return_value="🏷️ renamed")
        monkeypatch.setattr(sm, "rename_thread_topic", rename)
        thread = _thread()
        respond, ack, sent, acks = _capture()
        await cog._thread_rename_impl(channel=thread, respond=respond, ack=ack)
        assert acks == [{"ephemeral": False}]
        rename.assert_awaited_once_with(thread, cog.repo)
        assert sent[-1]["content"] == "🏷️ renamed"

    async def test_slash_and_text_route_to_impl(self):
        cog = _cog()
        cog._thread_rename_impl = AsyncMock()
        thread = _thread()
        await cog.thread_rename.callback(cog, _interaction(thread))
        await cog.thread_rename_text.callback(cog, _ctx(thread))
        assert [c.kwargs["channel"] for c in cog._thread_rename_impl.await_args_list] == [
            thread,
            thread,
        ]


# ── manager resolution via ChannelRepoCog ───────────────────────────────────


class TestManagerResolution:
    def _with_channel_cog(self):
        from c_lord.cogs.channel_repo import ChannelRepoCog

        cog = _cog()
        crc = MagicMock(spec=ChannelRepoCog)
        crc.resolve_manager = AsyncMock(return_value="SDM")
        crc.resolve_tmux_manager = AsyncMock(
            side_effect=lambda cid, thread_id=None: None if cid == 2 else f"T{cid}"
        )
        crc._repo = MagicMock()
        crc._repo.list_all = AsyncMock(return_value=[{"channel_id": 1}, {"channel_id": 2}])
        cog.bot.get_cog = MagicMock(return_value=crc)
        return cog, crc

    async def test_resolves_through_channel_repo_cog(self):
        cog, crc = self._with_channel_cog()
        assert cog._get_session_dir_manager() is None
        assert await cog._resolve_session_dir_manager(5) == "SDM"
        assert await cog._resolve_tmux_manager(5, thread_id=6) == "T5"
        # #427: the thread is passed through, not dropped
        crc.resolve_tmux_manager.assert_awaited_with(5, thread_id=6)
        # channel 2 has no manager -> skipped
        assert await cog._resolve_all_tmux_managers() == ["T1"]

    async def test_without_channel_repo_cog_everything_is_empty(self):
        cog = _cog()
        assert await cog._resolve_session_dir_manager(5) is None
        assert await cog._resolve_tmux_manager(5, thread_id=None) is None
        assert await cog._get_all_bindings() == []


# ── /clord-status plumbing ──────────────────────────────────────────────────


class TestClordStatusGaps:
    def test_resume_uuid_needs_a_working_dir(self, monkeypatch):
        from c_lord.cogs.session_manage import SessionManageCog

        assert SessionManageCog._resume_uuid_for(None) == ""
        monkeypatch.setattr(sm, "latest_session_jsonl", lambda d: Path("/p/uuid-1.jsonl"))
        assert SessionManageCog._resume_uuid_for("/w") == "uuid-1"
        monkeypatch.setattr(sm, "latest_session_jsonl", lambda d: None)
        assert SessionManageCog._resume_uuid_for("/w") == ""

    async def test_no_server_channel_is_refused(self):
        cog = _cog()
        respond, ack, sent, acks = _capture()
        await cog._clord_status_impl(
            channel=SimpleNamespace(), show_all=False, respond=respond, ack=ack
        )
        assert sent[-1]["ephemeral"] is True
        assert "server channel" in sent[-1]["content"]
        assert acks == []

    async def test_measured_windows_skips_untagged_and_bad_rows(self, monkeypatch):
        import c_lord.thread_state_sync as tss

        rows = [
            {"session_name": "c-lord", "window_name": "w1", "thread_id": "11"},
            {"session_name": "c-lord", "window_name": "w2", "thread_id": ""},
            {"session_name": "c-lord", "window_name": "", "thread_id": "12"},
            {"session_name": "c-lord", "window_name": "w4", "thread_id": "abc"},
            {"session_name": "", "window_name": "w5", "thread_id": "15"},
        ]
        monkeypatch.setattr(tss, "_list_all_windows", lambda: rows)

        by_thread, attach = await sm.SessionManageCog._measured_windows()

        assert by_thread == {11: "w1", 15: "w5"}
        assert attach == {11: "c-lord:w1", 15: "w5"}

    async def test_slash_in_thread_is_thread_scoped(self):
        cog = _cog()
        cog._clord_status_impl = AsyncMock()
        thread = _thread()
        await cog.clord_status.callback(cog, _interaction(thread), False)
        kw = cog._clord_status_impl.await_args.kwargs
        assert kw["channel"] is thread and kw["thread_scope"] is True


# ── /workspace-cleanup ──────────────────────────────────────────────────────


def _dir(tid: int, clean: bool = True, reason: str = ""):
    from c_lord.session_dir import SessionDirInfo

    return SessionDirInfo(
        path=f"/s/{tid}",
        thread_id=tid,
        source_repo="r",
        commit="c",
        is_clean=clean,
        kept_reason=reason,
    )


class TestWorkspaceCleanupGaps:
    def _cog(self, *, sdm, active=()):
        cog = _cog()
        cog.bot.session_registry.list_active = MagicMock(
            return_value=[SimpleNamespace(thread_id=t) for t in active]
        )
        cog._get_all_bindings = AsyncMock(return_value=[{"channel_id": 1}])
        cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
        return cog

    async def test_bindings_without_managers_says_nothing_is_managed(self):
        cog = self._cog(sdm=None)
        respond, ack, sent, _ = _capture()
        await cog._workspace_cleanup_impl(dry_run=False, respond=respond, ack=ack)
        assert "管理しているチャンネルがありません" in sent[-1]["embed"].description

    async def test_dry_run_skips_active_session(self):
        sdm = MagicMock()
        sdm.find_session_dirs = MagicMock(return_value=[_dir(1), _dir(2)])
        cog = self._cog(sdm=sdm, active=[2])
        respond, ack, sent, _ = _capture()
        await cog._workspace_cleanup_impl(dry_run=True, respond=respond, ack=ack)
        fields = {f.name: f.value for f in sent[-1]["embed"].fields}
        assert fields == {
            "Would remove (1)": "`/s/1`",
            "Would skip (1)": "`/s/2` — session is active",
        }

    async def test_dry_run_with_nothing_found(self):
        sdm = MagicMock()
        sdm.find_session_dirs = MagicMock(return_value=[])
        cog = self._cog(sdm=sdm)
        respond, ack, sent, _ = _capture()
        await cog._workspace_cleanup_impl(dry_run=True, respond=respond, ack=ack)
        assert sent[-1]["embed"].description == "作業ディレクトリが見つかりませんでした。"

    async def test_real_run_reports_removed_dirty_and_other_skips(self):
        from c_lord.session_dir import CleanupResult

        sdm = MagicMock()
        sdm.cleanup_orphaned = MagicMock(
            return_value=[
                CleanupResult("/s/1", 1, True, "removed"),
                CleanupResult("/s/2", 2, False, "1 unpushed commit(s)", kept_work=True),
                CleanupResult("/s/3", 3, False, "session is still active"),
                CleanupResult("/s/4", 4, False, "rm failed: EBUSY"),
            ]
        )
        cog = self._cog(sdm=sdm, active=[3])
        respond, ack, sent, _ = _capture()

        await cog._workspace_cleanup_impl(dry_run=False, respond=respond, ack=ack)

        sdm.cleanup_orphaned.assert_called_once_with({3})
        embed = sent[-1]["embed"]
        fields = {f.name: f.value for f in embed.fields}
        assert fields == {
            "✅ Removed (1)": "`/s/1`",
            "⚠️ Dirty — not removed (1)": "`/s/2` — 1 unpushed commit(s)",
            "ℹ️ Skipped (1)": "`/s/4` — rm failed: EBUSY",
        }
        assert embed.color.value == sm.COLOR_TOOL  # dirty wins the colour

    async def test_slash_and_alias_route_to_impl(self):
        cog = _cog()
        cog._workspace_cleanup_impl = AsyncMock()
        await cog.workspace_cleanup.callback(cog, _interaction(), True)
        await cog.session_cleanup.callback(cog, _interaction(), False)
        assert [c.kwargs["dry_run"] for c in cog._workspace_cleanup_impl.await_args_list] == [
            True,
            False,
        ]


# ── /tmux-list ──────────────────────────────────────────────────────────────


def _tmux(session: str = "c-lord", windows=()):
    mgr = MagicMock()
    mgr.session_name = session
    mgr.list_sessions = MagicMock(return_value=list(windows))
    return mgr


class TestTmuxList:
    async def test_lists_every_window_privately(self):
        cog = _cog()
        cog._get_all_bindings = AsyncMock(return_value=[{"channel_id": 1}, {"channel_id": 2}])
        mgr = _tmux(
            windows=[
                {"window_name": "w1", "working_dir": "/s/11", "thread_id": "11"},
                {"window_name": "w2", "working_dir": "", "thread_id": ""},
            ]
        )
        cog._resolve_tmux_manager = AsyncMock(
            side_effect=lambda cid, thread_id: mgr if cid == 1 else None
        )
        respond, ack, sent, acks = _capture()

        await cog._tmux_list_impl(respond=respond, ack=ack)

        assert acks == [{"ephemeral": True}]
        embed = sent[-1]["embed"]
        assert embed.title == "🖥️ Tmux Windows (2)"
        assert [(f.name, f.value) for f in embed.fields] == [
            ("`w1`", "Dir: `/s/11`\nThread: `11`"),
            ("`w2`", "Dir: `unknown`\nThread: —"),
        ]

    async def test_no_windows(self):
        cog = _cog()
        cog._get_all_bindings = AsyncMock(return_value=[{"channel_id": 1}])
        cog._resolve_tmux_manager = AsyncMock(return_value=_tmux(windows=[]))
        respond, ack, sent, _ = _capture()
        await cog._tmux_list_impl(respond=respond, ack=ack)
        assert sent[-1]["embed"].description == "No tmux windows found."

    async def test_slash_routes_to_impl(self):
        cog = _cog()
        cog._tmux_list_impl = AsyncMock()
        await cog.tmux_list.callback(cog, _interaction())
        cog._tmux_list_impl.assert_awaited_once()

    @pytest.mark.xfail(
        strict=True,
        reason="#1114: /tmux-list adds one embed field per window with no cap; Discord rejects "
        "an embed with more than 25 fields (400/50035), so a busy host gets an error "
        "instead of a list — the crash /clord-status was built to avoid (#363, #824).",
    )
    async def test_more_than_25_windows_still_fits_one_embed(self):
        cog = _cog()
        cog._get_all_bindings = AsyncMock(return_value=[{"channel_id": 1}])
        windows = [
            {"window_name": f"w{i}", "working_dir": f"/s/{i}", "thread_id": str(i)}
            for i in range(30)
        ]
        cog._resolve_tmux_manager = AsyncMock(return_value=_tmux(windows=windows))
        respond, ack, sent, _ = _capture()

        await cog._tmux_list_impl(respond=respond, ack=ack)

        embed = sent[-1]["embed"]
        assert len(embed.fields) <= 25
        assert len(embed) <= 6000


# ── /tmux-screenshot edges ──────────────────────────────────────────────────


class TestScreenshotGaps:
    async def test_empty_pane_says_so_without_a_file(self, monkeypatch):
        cog = _cog()
        mgr = _tmux()
        mgr.window_name = MagicMock(return_value="w1")
        mgr.capture_screen = MagicMock(return_value="   \n ")
        cog._resolve_tmux_manager = AsyncMock(return_value=mgr)
        render = MagicMock()
        monkeypatch.setattr(sm, "render_pane_png", render)
        respond, ack, sent, _ = _capture()

        await cog._screenshot_impl(channel=_thread(), respond=respond, ack=ack)

        render.assert_not_called()
        assert sent[-1] == {
            "content": "ℹ️ The tmux pane is currently empty.",
            "embed": None,
            "file": None,
            "ephemeral": True,
        }

    async def test_wake_that_raises_is_reported_as_failed(self):
        cog = _cog()
        chat = MagicMock()
        chat.wake_workspace = AsyncMock(side_effect=RuntimeError("boom"))
        cog.bot.get_cog = MagicMock(return_value=chat)
        cog.repo.get = AsyncMock(return_value=_record(555))
        mgr = _tmux()
        mgr.window_name = MagicMock(return_value=None)
        cog._resolve_tmux_manager = AsyncMock(return_value=mgr)
        respond, ack, sent, _ = _capture()

        await cog._screenshot_impl(channel=_thread(), respond=respond, ack=ack)

        assert sent[-1]["content"] == sm._WAKE_FAILED
        assert sent[-1]["ephemeral"] is True

    async def test_text_twin_routes_to_impl(self):
        cog = _cog()
        cog._screenshot_impl = AsyncMock()
        thread = _thread()
        await cog.tmux_screenshot_text.callback(cog, _ctx(thread))
        assert cog._screenshot_impl.await_args.kwargs["channel"] is thread


class TestCtxIo:
    async def test_file_is_sent_with_content_and_embed(self):
        cog = _cog()
        ctx = _ctx()
        respond, _ = cog._ctx_io(ctx)
        f = MagicMock(spec=discord.File)
        e = discord.Embed(title="x")
        await respond("hi", file=f, embed=e, ephemeral=True)
        ctx.send.assert_awaited_once_with("hi", embed=e, file=f)

    async def test_file_without_embed_uses_missing(self):
        cog = _cog()
        ctx = _ctx()
        respond, _ = cog._ctx_io(ctx)
        f = MagicMock(spec=discord.File)
        await respond(None, file=f)
        ctx.send.assert_awaited_once_with("", embed=discord.utils.MISSING, file=f)


# ── /resync ─────────────────────────────────────────────────────────────────


class TestResync:
    async def test_snapshot_empty_pane_is_none(self, monkeypatch):
        import c_lord.tmux as tmux_mod

        mgr = _tmux("c-lord")
        mgr.capture_screen = MagicMock(return_value="  ")
        ctor = MagicMock(return_value=mgr)
        monkeypatch.setattr(tmux_mod, "TmuxSessionManager", ctor)

        assert await _cog()._snapshot_pane("c-lord", "w3", 555) is None
        ctor.assert_called_once_with(session_name="c-lord")
        mgr.capture_screen.assert_called_once_with(555)

    async def test_snapshot_renders_with_status_bar(self, monkeypatch):
        import c_lord.tmux as tmux_mod

        mgr = _tmux("c-lord")
        mgr.capture_screen = MagicMock(return_value="pane")
        mgr.list_window_tabs = MagicMock(return_value=[(3, "w3", True)])
        monkeypatch.setattr(tmux_mod, "TmuxSessionManager", MagicMock(return_value=mgr))
        seen = {}

        def render(ansi, bar):
            seen["args"] = (ansi, bar)
            return b"PNG"

        monkeypatch.setattr(sm, "render_pane_png", render)

        assert await _cog()._snapshot_pane("c-lord", "w3", 555) == b"PNG"
        assert seen["args"] == ("pane", ("c-lord", [(3, "w3", True)], "w3"))

    async def test_resync_without_png_still_reports_reconnect(self):
        cog = _cog()
        cog._find_thread_window = AsyncMock(return_value=("c-lord", "w3"))
        cog._rebridge_menu = AsyncMock()
        cog._snapshot_pane = AsyncMock(return_value=None)
        respond, ack, sent, _ = _capture()

        await cog._resync_impl(channel=_thread(555), respond=respond, ack=ack)

        cog._rebridge_menu.assert_awaited_once_with(555, "c-lord", "w3")
        assert sent[-1]["file"] is None
        assert "pane は空" in sent[-1]["content"]

    async def test_slash_and_text_route_to_impl(self):
        cog = _cog()
        cog._resync_impl = AsyncMock()
        thread = _thread()
        await cog.resync.callback(cog, _interaction(thread))
        await cog.resync_text.callback(cog, _ctx(thread))
        assert cog._resync_impl.await_count == 2


# ── /workspace-delete ───────────────────────────────────────────────────────


class TestWorkspaceDeleteGaps:
    def _cog(self, *, killed: bool, cleanup):
        cog = _cog()
        mgr = _tmux()
        mgr.kill_session = MagicMock(return_value=killed)
        sdm = MagicMock()
        sdm.base_dir = "/s"
        sdm.cleanup_for_thread = MagicMock(return_value=cleanup)
        cog._resolve_tmux_manager = AsyncMock(return_value=mgr)
        cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
        return cog, mgr, sdm

    async def test_reports_missing_window_and_kept_work(self, monkeypatch):
        from c_lord.session_dir import CleanupResult

        monkeypatch.setattr(sm, "release_window_number", MagicMock())
        cog, _mgr, sdm = self._cog(
            killed=False,
            cleanup=CleanupResult("/s/555", 555, False, "2 unpushed commit(s)", kept_work=True),
        )
        respond, ack, sent, _ = _capture()

        await cog._workspace_delete_impl(channel=_thread(), respond=respond, ack=ack)

        sdm.cleanup_for_thread.assert_called_once_with(555)
        assert sent[-1]["embed"].description == (
            "ℹ️ No tmux window found\nℹ️ Session directory kept — 2 unpushed commit(s)"
        )

    async def test_missing_directory_reason_uses_colon(self, monkeypatch):
        from c_lord.session_dir import CleanupResult

        monkeypatch.setattr(sm, "release_window_number", MagicMock())
        cog, _mgr, _sdm = self._cog(
            killed=True,
            cleanup=CleanupResult("/s/555", 555, False, "session directory does not exist"),
        )
        respond, ack, sent, _ = _capture()

        await cog._workspace_delete_impl(channel=_thread(), respond=respond, ack=ack)

        assert sent[-1]["embed"].description == (
            "✅ Tmux window deleted\nℹ️ Session directory: session directory does not exist"
        )

    async def test_slash_routes_to_impl(self):
        cog = _cog()
        cog._workspace_delete_impl = AsyncMock()
        await cog.workspace_delete.callback(cog, _interaction(_thread()))
        cog._workspace_delete_impl.assert_awaited_once()

    @pytest.mark.xfail(
        strict=True,
        reason="#1113: docs: 削除 ⊃ 停止 (workspace-vocabulary.md, COMMANDS.md), but "
        "/workspace-delete never discovers or stops the workspace's docker containers "
        "(only /workspace-stop does, #574) — they keep holding ports with no owner.",
    )
    async def test_delete_stops_dev_containers_like_stop_does(self, monkeypatch):
        from c_lord.session_dir import CleanupResult

        monkeypatch.setattr(sm, "release_window_number", MagicMock())
        container = sm.DevContainer("id1", "db", "running", (55322,), "p", "mount")
        found = AsyncMock(return_value=[container])
        stop = AsyncMock(return_value=["db"])
        monkeypatch.setattr(sm, "containers_for_session_dir", found)
        monkeypatch.setattr(sm, "stop_containers", stop)
        cog, _mgr, _sdm = self._cog(
            killed=True, cleanup=CleanupResult("/s/555", 555, True, "removed")
        )
        respond, ack, _sent, _ = _capture()

        await cog._workspace_delete_impl(channel=_thread(), respond=respond, ack=ack)

        stop.assert_awaited_once_with([container])

    @pytest.mark.xfail(
        strict=True,
        reason="#1113: #909 ended the running turn before /workspace-stop kills the window, so the "
        "run does not poll a dead pane until the 5-min backstop and report a crash. "
        "/workspace-delete kills the same window without ending the turn.",
    )
    async def test_delete_ends_the_running_turn_first(self, monkeypatch):
        from c_lord.session_dir import CleanupResult

        monkeypatch.setattr(sm, "release_window_number", MagicMock())
        cog, _mgr, _sdm = self._cog(
            killed=True, cleanup=CleanupResult("/s/555", 555, True, "removed")
        )
        chat = MagicMock()
        chat.end_turn_for_workspace_stop = MagicMock(return_value=True)
        chat.wait_for_turn_end = AsyncMock()
        cog.bot.get_cog = MagicMock(side_effect=lambda n: chat if n == "ClaudeChatCog" else None)
        respond, ack, _sent, _ = _capture()

        await cog._workspace_delete_impl(channel=_thread(), respond=respond, ack=ack)

        chat.end_turn_for_workspace_stop.assert_called_once_with(555)


# ── tool-shell count / sleep ────────────────────────────────────────────────


class TestCountToolShells:
    async def test_not_a_thread_is_zero(self):
        assert await _cog()._count_tool_shells(_text_channel()) == 0

    async def test_no_manager_is_zero(self):
        cog = _cog()
        cog._resolve_tmux_manager = AsyncMock(return_value=None)
        assert await cog._count_tool_shells(_thread()) == 0

    async def test_counts_through_the_threads_manager(self):
        cog = _cog()
        mgr = _tmux()
        mgr.count_tool_shells = MagicMock(return_value=3)
        cog._resolve_tmux_manager = AsyncMock(return_value=mgr)
        assert await cog._count_tool_shells(_thread(555, parent_id=None)) == 3
        # no parent -> the thread id stands in for the channel, thread passed (#427)
        cog._resolve_tmux_manager.assert_awaited_once_with(555, thread_id=555)
        mgr.count_tool_shells.assert_called_once_with(555)


class TestSleepGaps:
    async def test_not_a_thread_sleeps_nothing(self):
        assert await _cog()._sleep_workspace_impl(channel=_text_channel()) is False

    async def test_kill_that_finds_no_window_is_not_counted(self):
        cog = _cog()
        mgr = _tmux()
        mgr.session_exists = MagicMock(return_value=True)
        mgr.kill_session = MagicMock(return_value=False)
        cog._resolve_tmux_manager = AsyncMock(return_value=mgr)

        assert await cog._sleep_workspace_impl(channel=_thread()) is False
        cog.repo.set_slept.assert_not_awaited()


# ── /workspace-stop ─────────────────────────────────────────────────────────


class TestWorkspaceStopGaps:
    def _setup(self, monkeypatch, *, containers=(), stopped=(), tmux=True, devenv=None):
        cog = _cog(devenv_repo=devenv)
        mgr = _tmux()
        mgr.kill_session = MagicMock(return_value=True)
        cog._resolve_tmux_manager = AsyncMock(return_value=mgr if tmux else None)
        sdm = MagicMock()
        sdm.base_dir = "/s"
        cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
        order: list[str] = []

        async def found(_dir):
            order.append("discover")
            return list(containers)

        async def stop(cs):
            order.append("stop")
            return list(stopped)

        monkeypatch.setattr(sm, "containers_for_session_dir", found)
        monkeypatch.setattr(sm, "stop_containers", stop)
        monkeypatch.setattr(sm, "retire_menus_for_stop", AsyncMock())
        monkeypatch.setattr(sm, "apply_closed_name", AsyncMock(return_value="[停止] t"))
        notice = MagicMock(side_effect=lambda *a, **kw: discord.Embed(title="停止"))
        monkeypatch.setattr(sm, "workspace_notice_embed", notice)
        return cog, mgr, notice, order

    async def test_end_turn_failure_does_not_block_the_stop(self, monkeypatch):
        cog, mgr, _notice, _ = self._setup(monkeypatch)
        chat = MagicMock()
        chat.end_turn_for_workspace_stop = MagicMock(side_effect=RuntimeError("x"))
        chat.wait_for_turn_end = AsyncMock()
        cog.bot.get_cog = MagicMock(side_effect=lambda n: chat if n == "ClaudeChatCog" else None)
        respond, ack, sent, _ = _capture()

        await cog._close_workspace_impl(channel=_thread(), respond=respond, ack=ack)

        mgr.kill_session.assert_called_once_with(555)
        cog.repo.set_closed.assert_awaited_once()
        sm.apply_closed_name.assert_awaited_once()
        assert sent[-1]["embed"] is not None

    async def test_unbound_channel_adds_a_note(self, monkeypatch):
        cog, _mgr, _notice, _ = self._setup(monkeypatch, tmux=False)
        respond, ack, sent, _ = _capture()
        await cog._close_workspace_impl(channel=_thread(), respond=respond, ack=ack)
        fields = {f.name: f.value for f in sent[-1]["embed"].fields}
        assert "clord-init" in fields["メモ"]

    async def test_containers_recorded_before_they_are_stopped(self, monkeypatch):
        container = sm.DevContainer("id1", "db", "running", (55322,), "p", "mount")
        devenv = MagicMock()
        cog, _mgr, notice, order = self._setup(
            monkeypatch, containers=[container], stopped=["db"], devenv=devenv
        )

        async def record(*args):
            order.append("record")

        devenv.record = AsyncMock(side_effect=record)
        respond, ack, _sent, _ = _capture()

        await cog._close_workspace_impl(channel=_thread(), respond=respond, ack=ack)

        devenv.record.assert_awaited_once_with(555, "/s/555", [container])
        assert order == ["discover", "record", "stop"]  # #612: written down first
        assert notice.call_args.kwargs["docker"] is sm.DockerOutcome.STOPPED

    async def test_docker_that_refused_is_reported_as_left_running(self, monkeypatch):
        container = sm.DevContainer("id1", "db", "running", (55322,), "p", "mount")
        cog, _mgr, notice, _ = self._setup(monkeypatch, containers=[container], stopped=[])
        respond, ack, _sent, _ = _capture()
        await cog._close_workspace_impl(channel=_thread(), respond=respond, ack=ack)
        # #571: report what happened, not what was attempted
        assert notice.call_args.kwargs["docker"] is sm.DockerOutcome.LEFT_RUNNING

    async def test_failing_devenv_record_still_stops(self, monkeypatch):
        container = sm.DevContainer("id1", "db", "running", (55322,), "p", "mount")
        devenv = MagicMock()
        devenv.record = AsyncMock(side_effect=RuntimeError("db locked"))
        cog, _mgr, _notice, order = self._setup(
            monkeypatch, containers=[container], stopped=["db"], devenv=devenv
        )
        respond, ack, _sent, _ = _capture()
        await cog._close_workspace_impl(channel=_thread(), respond=respond, ack=ack)
        assert order == ["discover", "stop"]

    async def test_slash_text_and_alias_route_to_impl(self):
        cog = _cog()
        cog._close_workspace_impl = AsyncMock()
        thread = _thread()
        await cog.workspace_stop.callback(cog, _interaction(thread))
        await cog.workspace_stop_text.callback(cog, _ctx(thread))
        await cog.close_workspace.callback(cog, _interaction(thread))
        assert cog._close_workspace_impl.await_count == 3


# ── /workspace-start ────────────────────────────────────────────────────────


class TestWorkspaceStartGaps:
    async def test_outside_thread_refuses(self):
        cog = _cog()
        respond, ack, sent, acks = _capture()
        await cog._reopen_workspace_impl(channel=_text_channel(), respond=respond, ack=ack)
        assert sent[-1]["ephemeral"] is True
        assert "thread" in sent[-1]["content"]
        assert acks == []

    async def test_mark_reopened_failure_still_reopens(self, monkeypatch):
        cog = _cog()
        cog.repo.get = AsyncMock(return_value=_record(555, closed_at="2026-10-01 10:00:00"))
        chat = MagicMock()
        chat.mark_reopened = MagicMock(side_effect=RuntimeError("x"))
        cog.bot.get_cog = MagicMock(side_effect=lambda n: chat if n == "ClaudeChatCog" else None)
        cog._resolve_session_dir_manager = AsyncMock(return_value=None)
        monkeypatch.setattr(sm, "apply_open_name", AsyncMock(return_value="t"))
        respond, ack, sent, _ = _capture()

        await cog._reopen_workspace_impl(
            channel=_thread(name="[停止] W3 │ t"), respond=respond, ack=ack
        )

        cog.repo.set_closed.assert_awaited_once_with(555, False)
        chat.mark_reopened.assert_called_once_with(555)
        assert sent[-1]["embed"].title == "▶️ このスレッドのワークスペースを再開しました"

    async def test_slash_text_and_alias_route_to_impl(self):
        cog = _cog()
        cog._reopen_workspace_impl = AsyncMock()
        thread = _thread()
        await cog.workspace_start.callback(cog, _interaction(thread))
        await cog.workspace_start_text.callback(cog, _ctx(thread))
        await cog.reopen_workspace.callback(cog, _interaction(thread))
        assert cog._reopen_workspace_impl.await_count == 3
