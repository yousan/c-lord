"""Session management Cog.

Provides slash commands for viewing and managing Claude Code sessions:
- /clord-status: Per-channel session status (size, attach, resume) — supersedes
  the removed /sessions, /session-dirs, /resume-info (#363)
- /session-cleanup, /tmux-list, /tmux-screenshot, /workspace-delete, ...
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..database.repository import SessionRepository
from ..database.settings_repo import SettingsRepository
from ..discord_ui.embeds import COLOR_INFO, COLOR_SUCCESS, COLOR_TOOL
from ..discord_ui.pane_renderer import render_pane_png
from ..session_dir import SessionDirManager
from ..status_view import StatusRow, classify_status, render_status
from ..thread_settings import (
    SETTING_THREAD_AUTO_ARCHIVE,
    VALID_DURATIONS,
    resolve_auto_archive_duration,
)
from ..tmux import parse_work_number
from ..transcript.resolver import derive_project_dir, latest_session_jsonl

if TYPE_CHECKING:
    from ..bot import ClaudeDiscordBot
    from ..tmux import TmuxSessionManager

logger = logging.getLogger(__name__)

# Lets each command's core run from either a slash interaction or a !text twin
# without duplicating the body (#209 follow-up).
_Responder = Callable[..., Awaitable[None]]
_Acknowledger = Callable[..., Awaitable[None]]


# Model management
SETTING_CLAUDE_MODEL = "claude_model"
_VALID_MODELS = {"haiku", "sonnet", "opus"}
_MODEL_CHOICES = [
    app_commands.Choice(name="Haiku 4.5 (fast, cost-effective)", value="haiku"),
    app_commands.Choice(name="Sonnet 4.6 (balanced, default)", value="sonnet"),
    app_commands.Choice(name="Opus 4.7 (powerful, deep reasoning)", value="opus"),
]

# Thread auto-archive duration management. Discord only accepts the four values
# in VALID_DURATIONS (see c_lord/thread_settings.py).
_ARCHIVE_CHOICES = [
    app_commands.Choice(name="1 hour", value=60),
    app_commands.Choice(name="1 day", value=1440),
    app_commands.Choice(name="3 days (default)", value=4320),
    app_commands.Choice(name="7 days", value=10080),
]


def _format_duration(minutes: int) -> str:
    """Human-readable label for a Discord auto-archive duration in minutes."""
    return {
        60: "1 hour",
        1440: "1 day",
        4320: "3 days",
        10080: "7 days",
    }.get(minutes, f"{minutes} min")


def _dir_size_bytes(path: str) -> int:
    """Disk usage of ``path`` in bytes via ``du -sb`` (never shell=True).

    Falls back to an ``os.walk`` sum if ``du`` is unavailable or errors. Runs in
    a worker thread (see callers) so the event loop never blocks on the syscall.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["du", "-sb", "--", path],  # -- guards a path that could start with '-'
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0 and result.stdout:
            return int(result.stdout.split("\t", 1)[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    total = 0
    import os

    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def _short_repo(source_repo: str | None) -> str:
    """``https://github.com/yousan/c-lord.git`` → ``yousan/c-lord``."""
    if not source_repo:
        return "-"
    s = source_repo.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    parts = [p for p in s.replace(":", "/").split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else s


class SessionManageCog(commands.Cog):
    """Cog for session listing, resume info, and CLI sync commands."""

    def __init__(
        self,
        bot: ClaudeDiscordBot,
        repo: SessionRepository,
        settings_repo: SettingsRepository | None = None,
        runner: object | None = None,
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.settings_repo = settings_repo
        # Optional ClaudeConfig reference for reading the default model.
        # Resolved lazily from ClaudeChatCog if not provided directly.
        self._runner = runner

    def _get_runner(self) -> object | None:
        """Return the runner, resolving it from ClaudeChatCog if not set directly."""
        if self._runner is not None:
            return self._runner
        chat_cog = self.bot.get_cog("ClaudeChatCog")
        if chat_cog is not None:
            return getattr(chat_cog, "runner", None)
        return None

    async def _get_effective_model(self) -> str:
        """Return the effective model: settings_repo override or runner default."""
        if self.settings_repo is not None:
            stored = await self.settings_repo.get(SETTING_CLAUDE_MODEL)
            if stored:
                return stored
        runner = self._get_runner()
        if runner is not None and hasattr(runner, "model"):
            return runner.model  # type: ignore[return-value]
        return "sonnet"

    # ── Slash/text I/O plumbing (#209 follow-up) ───────────────────────────────
    # Each read-only command's core takes a (respond, ack) pair so the same body
    # serves both the slash command and its !text twin.

    def _slash_io(self, interaction: discord.Interaction) -> tuple[_Responder, _Acknowledger]:
        state = {"acked": False}

        async def ack(*, ephemeral: bool = False) -> None:
            state["acked"] = True
            await interaction.response.defer(ephemeral=ephemeral)

        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            file: discord.File | None = None,
            ephemeral: bool = False,
        ) -> None:
            if file is not None:
                embed_arg = embed if embed is not None else discord.utils.MISSING
                if state["acked"]:
                    await interaction.followup.send(
                        content or "", embed=embed_arg, file=file, ephemeral=ephemeral
                    )
                else:
                    await interaction.response.send_message(
                        content, embed=embed_arg, file=file, ephemeral=ephemeral
                    )
                return
            if state["acked"]:
                if embed is not None:
                    await interaction.followup.send(embed=embed, ephemeral=ephemeral)
                else:
                    await interaction.followup.send(content or "", ephemeral=ephemeral)
            elif embed is not None:
                await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)

        return respond, ack

    def _ctx_io(self, ctx: commands.Context) -> tuple[_Responder, _Acknowledger]:
        async def ack(*, ephemeral: bool = False) -> None:
            return None

        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            file: discord.File | None = None,
            ephemeral: bool = False,
        ) -> None:
            # Text channels can't be ephemeral — ``ephemeral`` is ignored.
            if file is not None:
                embed_arg = embed if embed is not None else discord.utils.MISSING
                await ctx.send(content or "", embed=embed_arg, file=file)
            elif embed is not None:
                await ctx.send(embed=embed)
            else:
                await ctx.send(content or "")

        return respond, ack

    # ── Model commands ────────────────────────────────────────────────────────

    model_group = app_commands.Group(
        name="model",
        description="View and change the Claude model used for new sessions",
    )

    async def _model_show_impl(self, *, channel: object, respond: _Responder) -> None:
        """Shared core for /model show and !model-show (#209 follow-up)."""
        effective_model = await self._get_effective_model()

        embed = discord.Embed(
            title="🤖 Current Claude Model",
            color=COLOR_INFO,
        )

        # Global / default model field
        stored = await self.settings_repo.get(SETTING_CLAUDE_MODEL) if self.settings_repo else None
        runner = self._get_runner()
        runner_model = getattr(runner, "model", "sonnet") if runner else "sonnet"
        if stored:
            embed.description = (
                f"**Global override:** `{stored}`\n*(runner default: `{runner_model}`)*"
            )
        else:
            embed.description = (
                f"**Default model:** `{runner_model}`\n"
                "*(no override set — use `/model set` to change)*"
            )

        # Per-thread session model (if inside a thread)
        if isinstance(channel, discord.Thread):
            record = await self.repo.get(channel.id)
            if record and record.model:
                embed.add_field(
                    name="This thread's last session",
                    value=f"`{record.model}`",
                    inline=False,
                )

        embed.set_footer(text=f"Effective model for new sessions: {effective_model}")
        await respond(embed=embed)

    @model_group.command(name="show", description="Show the current Claude model")
    async def model_show(self, interaction: discord.Interaction) -> None:
        """Display the current global model and, if in a thread, the per-session model."""
        respond, _ = self._slash_io(interaction)
        await self._model_show_impl(channel=interaction.channel, respond=respond)

    @commands.command(name="model-show")
    async def model_show_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /model show — webhook-invokable for E2E (#209)."""
        respond, _ = self._ctx_io(ctx)
        await self._model_show_impl(channel=ctx.channel, respond=respond)

    async def _model_set_impl(self, *, model: str, respond: _Responder) -> None:
        """Shared core for /model set and !model-set (#209 follow-up)."""
        if model not in _VALID_MODELS:
            await respond(
                f"❌ Unknown model `{model}`. Valid choices: {', '.join(sorted(_VALID_MODELS))}",
                ephemeral=True,
            )
            return

        if self.settings_repo is None:
            await respond(
                "❌ Settings repository is unavailable — model cannot be persisted.",
                ephemeral=True,
            )
            return

        await self.settings_repo.set(SETTING_CLAUDE_MODEL, model)

        embed = discord.Embed(
            title="✅ Model Updated",
            description=f"Global model set to **`{model}`**.\nAll new sessions will use this model.",  # noqa: E501
            color=COLOR_SUCCESS,
        )
        await respond(embed=embed)

    @model_group.command(name="set", description="Change the global Claude model for new sessions")
    @app_commands.describe(model="Model to use for all new Claude sessions")
    @app_commands.choices(model=_MODEL_CHOICES)
    async def model_set(self, interaction: discord.Interaction, model: str) -> None:
        """Set the global default model stored in settings_repo."""
        respond, _ = self._slash_io(interaction)
        await self._model_set_impl(model=model, respond=respond)

    @commands.command(name="model-set")
    async def model_set_text(self, ctx: commands.Context, model: str | None = None) -> None:
        """Text/mention twin of /model set — webhook-invokable for E2E (#209)."""
        if not model:
            await ctx.send(f"Usage: `!model-set <{'/'.join(sorted(_VALID_MODELS))}>`")
            return
        respond, _ = self._ctx_io(ctx)
        await self._model_set_impl(model=model, respond=respond)

    # ── Thread auto-archive duration commands ──────────────────────────────────

    thread_archive_group = app_commands.Group(
        name="thread-archive",
        description="View and change how long c-lord threads stay active before archiving",
    )

    async def _archive_show_impl(self, *, respond: _Responder) -> None:
        """Shared core for /thread-archive show and !thread-archive-show."""
        effective = await resolve_auto_archive_duration(self.settings_repo)
        stored = (
            await self.settings_repo.get(SETTING_THREAD_AUTO_ARCHIVE)
            if self.settings_repo
            else None
        )

        embed = discord.Embed(title="🗂️ Thread Auto-Archive Duration", color=COLOR_INFO)
        if stored:
            embed.description = f"**Configured:** {_format_duration(effective)} (`{effective}` min)"
        else:
            embed.description = (
                f"**Default:** {_format_duration(effective)} (`{effective}` min)\n"
                "*(no override set — use `/thread-archive set` to change)*"
            )
        embed.set_footer(text="Applies to threads c-lord creates from now on.")
        await respond(embed=embed)

    @thread_archive_group.command(name="show", description="Show the thread auto-archive duration")
    async def thread_archive_show(self, interaction: discord.Interaction) -> None:
        """Display the current thread auto-archive duration."""
        respond, _ = self._slash_io(interaction)
        await self._archive_show_impl(respond=respond)

    @commands.command(name="thread-archive-show")
    async def thread_archive_show_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /thread-archive show — webhook-invokable for E2E."""
        respond, _ = self._ctx_io(ctx)
        await self._archive_show_impl(respond=respond)

    async def _archive_set_impl(self, *, duration: int, respond: _Responder) -> None:
        """Shared core for /thread-archive set and !thread-archive-set."""
        if duration not in VALID_DURATIONS:
            valid = ", ".join(str(d) for d in VALID_DURATIONS)
            await respond(
                f"❌ Invalid duration `{duration}`. Discord only accepts: {valid} (minutes).",
                ephemeral=True,
            )
            return

        if self.settings_repo is None:
            await respond(
                "❌ Settings repository is unavailable — duration cannot be persisted.",
                ephemeral=True,
            )
            return

        await self.settings_repo.set(SETTING_THREAD_AUTO_ARCHIVE, str(duration))

        embed = discord.Embed(
            title="✅ Thread Auto-Archive Updated",
            description=(
                f"New threads will archive after **{_format_duration(duration)}** "
                f"(`{duration}` min) of inactivity."
            ),
            color=COLOR_SUCCESS,
        )
        await respond(embed=embed)

    @thread_archive_group.command(
        name="set", description="Change how long new threads stay active before archiving"
    )
    @app_commands.describe(duration="Inactivity period before a thread auto-archives")
    @app_commands.choices(duration=_ARCHIVE_CHOICES)
    async def thread_archive_set(self, interaction: discord.Interaction, duration: int) -> None:
        """Set the global thread auto-archive duration stored in settings_repo."""
        respond, _ = self._slash_io(interaction)
        await self._archive_set_impl(duration=duration, respond=respond)

    @commands.command(name="thread-archive-set")
    async def thread_archive_set_text(
        self, ctx: commands.Context, duration: str | None = None
    ) -> None:
        """Text/mention twin of /thread-archive set — webhook-invokable for E2E."""
        if not duration:
            valid = "/".join(str(d) for d in VALID_DURATIONS)
            await ctx.send(f"Usage: `!thread-archive-set <{valid}>` (minutes)")
            return
        respond, _ = self._ctx_io(ctx)
        try:
            minutes = int(duration)
        except (TypeError, ValueError):
            await respond(f"❌ Invalid duration `{duration}` — must be a number.", ephemeral=True)
            return
        await self._archive_set_impl(duration=minutes, respond=respond)

    # ------------------------------------------------------------------
    # Session directory commands
    # ------------------------------------------------------------------

    def _get_session_dir_manager(self) -> SessionDirManager | None:
        """Return the SessionDirManager from the bot, if configured.

        .. deprecated::
            This returns None now that global managers are removed.
            Use :meth:`_resolve_session_dir_manager` for per-channel lookup.
        """
        return None

    async def _resolve_session_dir_manager(self, channel_id: int) -> SessionDirManager | None:
        """Resolve a SessionDirManager for the given channel via ChannelRepoCog."""
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_manager(channel_id)
        return None

    async def _resolve_tmux_manager(self, channel_id: int) -> TmuxSessionManager | None:
        """Resolve a TmuxSessionManager for the given channel via ChannelRepoCog."""
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_tmux_manager(channel_id)
        return None

    async def _resolve_all_tmux_managers(self) -> list[TmuxSessionManager]:
        """Return all TmuxSessionManager instances across channel bindings."""
        bindings = await self._get_all_bindings()
        managers: list[TmuxSessionManager] = []
        for binding in bindings:
            mgr = await self._resolve_tmux_manager(binding["channel_id"])
            if mgr is not None:
                managers.append(mgr)
        return managers

    async def _get_all_bindings(self) -> list[dict]:
        """Return all channel-repo bindings from ChannelRepoCog."""
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog._repo.list_all()
        return []

    # ── /clord-status (#363) ────────────────────────────────────────────────
    # One per-channel view that supersedes /sessions, /session-dirs, /resume-info.
    # docker ps model: default = live only, `all` = live + closed; deleted (no
    # working dir) is a footer count, never a row. See c_lord/status_view.py.

    @staticmethod
    def _resume_uuid_for(working_dir: str | None) -> str:
        """Real ``claude --resume`` id for a session = its transcript jsonl stem.

        The DB ``session_id`` is ``tmux-…`` for tmux-managed sessions (not a
        resumable id), so the actual Claude session uuid is recovered from
        ``~/.claude/projects/<slug>/<uuid>.jsonl`` (#363).
        """
        if not working_dir:
            return ""
        jsonl = latest_session_jsonl(derive_project_dir(working_dir))
        return jsonl.stem if jsonl is not None else ""

    async def _clord_status_impl(
        self, *, channel: object, show_all: bool, respond: _Responder, ack: _Acknowledger
    ) -> None:
        """Shared core for /clord-status and !clord-status (#363).

        Lists only the sessions of the *invoking channel* (per-channel — this is
        what structurally avoids the all-channels 25-field crash that froze the
        old /session-dirs). Live + closed get table rows; deleted is a count.
        """
        import asyncio
        from datetime import datetime

        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None) or channel_id
        if parent_id is None:
            await respond("This command must be used in a server channel.", ephemeral=True)
            return

        sdm = await self._resolve_session_dir_manager(parent_id)
        tmux_mgr = await self._resolve_tmux_manager(parent_id)
        if sdm is None or tmux_mgr is None:
            await respond(
                "ℹ️ このチャンネルにはリポジトリが紐づけられていません。"
                " `/clord-init` で設定してください。",
                ephemeral=True,
            )
            return

        await ack()

        dirs = await asyncio.to_thread(sdm.find_session_dirs)
        windows = await asyncio.to_thread(tmux_mgr.list_sessions)
        window_by_thread: dict[int, str] = {}
        for w in windows:
            tid = w.get("thread_id")
            if tid:
                with contextlib.suppress(ValueError, TypeError):
                    window_by_thread[int(tid)] = w["window_name"]

        rows: list[StatusRow] = []
        for d in dirs:
            rec = await self.repo.get(d.thread_id)
            window_name = window_by_thread.get(d.thread_id)
            has_window = window_name is not None
            status = classify_status(
                has_window=has_window,
                db_state=rec.state if rec else None,
                dir_exists=True,
            )
            if status is None:  # dir exists -> never None, but stay defensive
                continue
            size = await asyncio.to_thread(_dir_size_bytes, d.path)
            topic = (rec.topic or rec.summary if rec else None) or "(no topic)"
            rows.append(
                StatusRow(
                    window_number=parse_work_number(window_name) if window_name else None,
                    status=status,
                    topic=topic,
                    size_bytes=size,
                    last_used=rec.last_used_at if rec else "",
                    session_id=self._resume_uuid_for((rec.working_dir if rec else None) or d.path),
                )
            )

        # deleted = this channel's DB sessions whose working dir is gone and which
        # have no live window (workspace-delete removed the clone). Count only.
        present = {d.thread_id for d in dirs}
        all_records = await self.repo.list_all(limit=1000)
        deleted_count = sum(
            1
            for r in all_records
            if r.working_dir
            and f"/{parent_id}/" in r.working_dir
            and r.thread_id not in present
            and r.thread_id not in window_by_thread
        )

        repo_label = _short_repo(dirs[0].source_repo if dirs else None)
        channel_name = getattr(getattr(channel, "parent", None), "name", None) or getattr(
            channel, "name", str(parent_id)
        )
        content = render_status(
            rows=rows,
            show_all=show_all,
            channel_name=channel_name,
            repo=repo_label,
            session_name=tmux_mgr.session_name,
            deleted_count=deleted_count,
            now=datetime.now(),
        )
        await respond(content)

    @app_commands.command(
        name="clord-status",
        description="List this channel's Claude sessions (size, attach, resume)",
    )
    @app_commands.describe(show_all="Include closed sessions too (like `docker ps -a`)")
    async def clord_status(self, interaction: discord.Interaction, show_all: bool = False) -> None:
        """Per-channel session status. ``show_all`` adds closed sessions (#363)."""
        respond, ack = self._slash_io(interaction)
        await self._clord_status_impl(
            channel=interaction.channel, show_all=show_all, respond=respond, ack=ack
        )

    @commands.command(name="clord-status")
    async def clord_status_text(self, ctx: commands.Context, arg: str | None = None) -> None:
        """Text/mention twin of /clord-status. ``!clord-status all`` shows closed."""
        show_all = (arg or "").lower() in {"all", "-a", "a"}
        respond, ack = self._ctx_io(ctx)
        await self._clord_status_impl(
            channel=ctx.channel, show_all=show_all, respond=respond, ack=ack
        )

    async def _session_cleanup_impl(
        self, *, dry_run: bool, respond: _Responder, ack: _Acknowledger
    ) -> None:
        """Shared core for /session-cleanup and !session-cleanup (#209 follow-up)."""
        bindings = await self._get_all_bindings()
        if not bindings:
            await respond(
                "❌ No channel-repo bindings configured. Use `/clord-init` first.",
                ephemeral=True,
            )
            return

        await ack()

        import asyncio

        # Determine active thread IDs from the session registry
        active_ids: set[int] = set()
        if hasattr(self.bot, "session_registry"):
            active_ids = {s.thread_id for s in self.bot.session_registry.list_active()}

        # Collect managers from all bindings
        managers: list[SessionDirManager] = []
        for binding in bindings:
            sdm = await self._resolve_session_dir_manager(binding["channel_id"])
            if sdm is not None:
                managers.append(sdm)

        if not managers:
            await respond(
                embed=discord.Embed(
                    title="📁 Session Cleanup",
                    description="No session directory managers found.",
                    color=COLOR_INFO,
                )
            )
            return

        if dry_run:
            all_dirs = []
            for sdm in managers:
                dirs = await asyncio.to_thread(sdm.find_session_dirs)
                all_dirs.extend(dirs)

            candidates = []
            skipped = []
            for d in all_dirs:
                if d.thread_id in active_ids:
                    skipped.append((d, "session is active"))
                    continue
                if d.is_clean:
                    candidates.append(d)
                else:
                    skipped.append((d, "dirty"))

            embed = discord.Embed(
                title="📁 Session Cleanup — Dry Run",
                color=COLOR_INFO,
            )
            if candidates:
                embed.add_field(
                    name=f"Would remove ({len(candidates)})",
                    value="\n".join(f"`{d.path}`" for d in candidates) or "—",
                    inline=False,
                )
            if skipped:
                embed.add_field(
                    name=f"Would skip ({len(skipped)})",
                    value="\n".join(f"`{d.path}` — {reason}" for d, reason in skipped) or "—",
                    inline=False,
                )
            if not candidates and not skipped:
                embed.description = "No session directories found."
            embed.set_footer(text="Re-run without dry_run=True to actually remove.")
            await respond(embed=embed)
            return

        all_results = []
        for sdm in managers:
            results = await asyncio.to_thread(sdm.cleanup_orphaned, active_ids)
            all_results.extend(results)

        removed = [r for r in all_results if r.removed]
        dirty = [r for r in all_results if not r.removed and "uncommitted changes" in r.reason]
        other_skipped = [
            r
            for r in all_results
            if not r.removed
            and "uncommitted changes" not in r.reason
            and r.reason != "session is still active"
        ]

        color = COLOR_SUCCESS if removed else COLOR_INFO
        if dirty:
            color = COLOR_TOOL

        embed = discord.Embed(
            title="📁 Session Cleanup Complete",
            color=color,
        )
        embed.add_field(
            name=f"✅ Removed ({len(removed)})",
            value="\n".join(f"`{r.path}`" for r in removed) or "—",
            inline=False,
        )
        if dirty:
            embed.add_field(
                name=f"⚠️ Dirty — not removed ({len(dirty)})",
                value="\n".join(f"`{r.path}`" for r in dirty) or "—",
                inline=False,
            )
        if other_skipped:
            embed.add_field(
                name=f"ℹ️ Skipped ({len(other_skipped)})",
                value="\n".join(f"`{r.path}` — {r.reason}" for r in other_skipped) or "—",
                inline=False,
            )

        await respond(embed=embed)

    @app_commands.command(
        name="session-cleanup",
        description="Remove clean orphaned session directories",
    )
    @app_commands.describe(
        dry_run="Preview what would be removed without actually removing anything",
    )
    async def session_cleanup(
        self,
        interaction: discord.Interaction,
        dry_run: bool = False,
    ) -> None:
        """Remove session directories that have no active session and are clean."""
        respond, ack = self._slash_io(interaction)
        await self._session_cleanup_impl(dry_run=dry_run, respond=respond, ack=ack)

    @commands.command(name="session-cleanup")
    async def session_cleanup_text(self, ctx: commands.Context, arg: str | None = None) -> None:
        """Text/mention twin of /session-cleanup — webhook-invokable for E2E (#209).

        Usage: ``!session-cleanup`` (remove) / ``!session-cleanup dry`` (preview).
        """
        dry_run = (arg or "").lower() in {"dry", "dry_run", "dry-run", "true", "1"}
        respond, ack = self._ctx_io(ctx)
        await self._session_cleanup_impl(dry_run=dry_run, respond=respond, ack=ack)

    async def _tmux_list_impl(self, *, respond: _Responder, ack: _Acknowledger) -> None:
        """Shared core for /tmux-list and !tmux-list (#209 follow-up)."""
        bindings = await self._get_all_bindings()
        if not bindings:
            await respond(
                "❌ No channel-repo bindings configured. Use `/clord-init` first.",
                ephemeral=True,
            )
            return

        await ack(ephemeral=True)

        import asyncio

        all_windows: list[dict] = []
        for binding in bindings:
            tmux_mgr = await self._resolve_tmux_manager(binding["channel_id"])
            if tmux_mgr is not None:
                windows = await asyncio.to_thread(tmux_mgr.list_sessions)
                all_windows.extend(windows)

        if not all_windows:
            await respond(
                embed=discord.Embed(
                    title="🖥️ Tmux Windows",
                    description="No tmux windows found.",
                    color=COLOR_INFO,
                )
            )
            return

        embed = discord.Embed(
            title=f"🖥️ Tmux Windows ({len(all_windows)})",
            color=COLOR_INFO,
        )
        for w in all_windows:
            tid = w.get("thread_id", "")
            tid_display = f"Thread: `{tid}`" if tid else "Thread: —"
            embed.add_field(
                name=f"`{w['window_name']}`",
                value=f"Dir: `{w['working_dir'] or 'unknown'}`\n{tid_display}",
                inline=False,
            )

        await respond(embed=embed)

    @app_commands.command(
        name="tmux-list",
        description="List all active tmux windows for Claude Code",
    )
    async def tmux_list(self, interaction: discord.Interaction) -> None:
        """Show all windows across all channel tmux sessions."""
        respond, ack = self._slash_io(interaction)
        await self._tmux_list_impl(respond=respond, ack=ack)

    @commands.command(name="tmux-list")
    async def tmux_list_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /tmux-list — webhook-invokable for E2E (#209)."""
        respond, ack = self._ctx_io(ctx)
        await self._tmux_list_impl(respond=respond, ack=ack)

    async def _screenshot_impl(
        self, *, channel: object, respond: _Responder, ack: _Acknowledger
    ) -> None:
        """Shared core for /tmux-screenshot and !tmux-screenshot (#285).

        Captures the thread's *visible* tmux pane and posts it as a PNG so the
        colors / layout / Claude TUI status lamps survive — a plain text
        capture loses them. PNG rendering needs the optional ``c-lord[table]``
        extra (Pillow); without it we reply with an actionable hint.
        """
        if not isinstance(channel, discord.Thread):
            await respond(
                "This command can only be used in a Claude chat thread.",
                ephemeral=True,
            )
            return

        thread_id = channel.id
        parent_channel_id = channel.parent_id or thread_id
        await ack()  # capture + render can exceed Discord's 3s ack window

        import asyncio

        tmux_mgr = await self._resolve_tmux_manager(parent_channel_id)
        if tmux_mgr is None:
            await respond(
                "ℹ️ このチャンネルにはリポジトリが紐づけられていません。"
                " `/clord-init` で設定してください。",
                ephemeral=True,
            )
            return

        window = await asyncio.to_thread(tmux_mgr._find_window_for_thread, thread_id)
        if window is None:
            await respond("ℹ️ No tmux window found for this thread.", ephemeral=True)
            return

        ansi = await asyncio.to_thread(tmux_mgr.capture_screen, thread_id)
        if not ansi.strip():
            await respond("ℹ️ The tmux pane is currently empty.", ephemeral=True)
            return

        # Synthesize a tmux-style status bar (session + window tabs, this
        # window highlighted) so the screenshot shows which pane it is (#285).
        tabs = await asyncio.to_thread(tmux_mgr.list_window_tabs)
        status_bar = (tmux_mgr.session_name, tabs, window) if tabs else None
        png = await asyncio.to_thread(render_pane_png, ansi, status_bar)
        if png is None:
            await respond(
                "⚠️ スクリーンショットのレンダリングに必要な依存が見つかりません。"
                " `pip install c-lord[table]` (Pillow) を導入してください。",
                ephemeral=True,
            )
            return

        filename = f"tmux-{tmux_mgr.session_name}-{window}.png"
        file = discord.File(BytesIO(png), filename=filename)
        await respond(file=file)

    @app_commands.command(
        name="tmux-screenshot",
        description="Post a PNG screenshot of this thread's current tmux pane",
    )
    async def tmux_screenshot(self, interaction: discord.Interaction) -> None:
        """Screenshot the current tmux pane and post it as a PNG (#285)."""
        respond, ack = self._slash_io(interaction)
        await self._screenshot_impl(channel=interaction.channel, respond=respond, ack=ack)

    @commands.command(name="tmux-screenshot")
    async def tmux_screenshot_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /tmux-screenshot — webhook-invokable for E2E (#285)."""
        respond, ack = self._ctx_io(ctx)
        await self._screenshot_impl(channel=ctx.channel, respond=respond, ack=ack)

    async def _workspace_delete_impl(
        self, *, channel: object, respond: _Responder, ack: _Acknowledger
    ) -> None:
        """Shared core for /workspace-delete and !workspace-delete (#209 follow-up)."""
        if not isinstance(channel, discord.Thread):
            await respond(
                "This command can only be used in a Claude chat thread.",
                ephemeral=True,
            )
            return

        thread_id = channel.id
        parent_channel_id = channel.parent_id or thread_id
        await ack()

        import asyncio

        results: list[str] = []

        # Kill tmux window
        tmux_mgr = await self._resolve_tmux_manager(parent_channel_id)
        if tmux_mgr is not None:
            killed = await asyncio.to_thread(tmux_mgr.kill_session, thread_id)
            if killed:
                results.append("✅ Tmux window deleted")
            else:
                results.append("ℹ️ No tmux window found")

        # Remove session directory
        sdm = await self._resolve_session_dir_manager(parent_channel_id)
        if sdm is not None:
            cleanup = await asyncio.to_thread(sdm.cleanup_for_thread, thread_id)
            if cleanup.removed:
                results.append(f"✅ Session directory removed: `{cleanup.path}`")
            else:
                results.append(f"ℹ️ Session directory: {cleanup.reason}")

        if not results:
            results.append(
                "ℹ️ このチャンネルにはリポジトリが紐づけられていません。"
                " `/clord-init` で設定してください。"
            )

        embed = discord.Embed(
            title="🗑️ Workspace Deleted",
            description="\n".join(results),
            color=COLOR_SUCCESS,
        )
        await respond(embed=embed)

    @app_commands.command(
        name="workspace-delete",
        description="Delete the tmux window and session directory for this thread",
    )
    async def workspace_delete(self, interaction: discord.Interaction) -> None:
        """Delete the tmux window and session directory for the current thread."""
        respond, ack = self._slash_io(interaction)
        await self._workspace_delete_impl(channel=interaction.channel, respond=respond, ack=ack)

    @commands.command(name="workspace-delete")
    async def workspace_delete_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /workspace-delete — webhook-invokable for E2E (#209)."""
        respond, ack = self._ctx_io(ctx)
        await self._workspace_delete_impl(channel=ctx.channel, respond=respond, ack=ack)

    async def _close_workspace_impl(
        self, *, channel: object, respond: _Responder, ack: _Acknowledger
    ) -> None:
        """Shared core for /close-workspace and !close-workspace (#271).

        Non-destructive counterpart to ``/workspace-delete``: kills the tmux
        window and archives the thread to declutter, but **keeps** the session
        directory, transcript, and DB session record.  The next message resumes
        the conversation via ``--continue`` (#270) — that is the whole point of
        "close" vs "delete".  Note this never resolves the session-dir manager,
        so the directory-removal path is structurally unreachable here.
        """
        if not isinstance(channel, discord.Thread):
            await respond(
                "This command can only be used in a Claude chat thread.",
                ephemeral=True,
            )
            return

        thread_id = channel.id
        parent_channel_id = channel.parent_id or thread_id
        await ack()

        import asyncio

        results: list[str] = []

        # Kill the tmux window to free the work<N> slot.
        tmux_mgr = await self._resolve_tmux_manager(parent_channel_id)
        if tmux_mgr is not None:
            killed = await asyncio.to_thread(tmux_mgr.kill_session, thread_id)
            if killed:
                results.append("✅ Tmux window closed")
            else:
                results.append("ℹ️ No tmux window found")
            results.append("📂 Session directory kept — send a message to resume.")
        else:
            results.append(
                "ℹ️ このチャンネルにはリポジトリが紐づけられていません。"
                " `/clord-init` で設定してください。"
            )

        embed = discord.Embed(
            title="🧹 Workspace Closed",
            description="\n".join(results),
            color=COLOR_SUCCESS,
        )
        await respond(embed=embed)

        # Archive the thread to declutter the sidebar (best-effort).
        with contextlib.suppress(discord.HTTPException):
            await channel.edit(archived=True)

    @app_commands.command(
        name="close-workspace",
        description="Close the tmux window but keep the session (resumes on next message)",
    )
    async def close_workspace(self, interaction: discord.Interaction) -> None:
        """Close the tmux window + archive the thread, keeping the session dir (#271)."""
        respond, ack = self._slash_io(interaction)
        await self._close_workspace_impl(channel=interaction.channel, respond=respond, ack=ack)

    @commands.command(name="close-workspace")
    async def close_workspace_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /close-workspace — webhook-invokable for E2E (#271)."""
        respond, ack = self._ctx_io(ctx)
        await self._close_workspace_impl(channel=ctx.channel, respond=respond, ack=ack)
