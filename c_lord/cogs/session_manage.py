"""Session management Cog.

Provides slash commands for viewing and managing Claude Code sessions:
- /clord-status: Per-channel session status (size, attach, resume) — supersedes
  the removed /sessions, /session-dirs, /resume-info (#363)
- /session-cleanup, /tmux-list, /tmux-screenshot, /workspace-delete, ...
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..database.repository import SessionRepository
from ..database.settings_repo import SettingsRepository
from ..devenv import DevContainer, containers_for_session_dir, stop_containers
from ..discord_ui.embeds import COLOR_INFO, COLOR_SUCCESS, COLOR_TOOL
from ..discord_ui.pane_renderer import render_pane_png
from ..session_close import (
    apply_closed_name,
    apply_open_name,
    is_closed,
    reopen_rename_notice,
)
from ..session_dir import SessionDirManager
from ..session_resume import ThreadResume, classify, hint_for_thread, stopped_hint
from ..status_view import StatusRow, classify_status, render_status
from ..thread_settings import (
    SETTING_THREAD_AUTO_ARCHIVE,
    VALID_DURATIONS,
    resolve_auto_archive_duration,
)
from ..tmux import parse_work_number
from ..transcript.resolver import derive_project_dir, latest_session_jsonl
from ..utils.logger import log_ctx
from ..workspace_notice import (
    DockerOutcome,
    WorkspaceAction,
    WorkspaceReason,
    workspace_notice_embed,
)

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

# Tier aliases the Claude CLI resolves to the *latest* model of that tier
# (``claude --help``: "Provide an alias for the latest model"). Labels are kept
# version-agnostic on purpose — hardcoding "4.7"/"5" would go stale and mislead,
# since the alias already tracks the latest (#478).
_VALID_MODELS = {"haiku", "sonnet", "opus"}
_MODEL_CHOICES = [
    app_commands.Choice(name="Sonnet — latest balanced (default)", value="sonnet"),
    app_commands.Choice(name="Opus — most capable, deep reasoning", value="opus"),
    app_commands.Choice(name="Haiku — fastest, low cost", value="haiku"),
]

# A model string is either a tier alias or a full model ID passed straight to the
# CLI (e.g. ``claude-fable-5``), so tier-external / brand-new models can be
# selected without a c-lord release (#478). It is interpolated into a tmux
# ``send-keys`` command (``--model {model}``) — a shell context — so it MUST match
# this strict allowlist before use: a leading letter then letters/digits/``.-_``,
# max 64 chars. This blocks spaces, shell metacharacters, and a leading ``-``
# (flag injection). Whether the model actually *exists* is left to the CLI to
# decide; c-lord keeps no model registry (security-audit, #478).
_MODEL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")

#: Posted with the PNG when the capture had to restore the workspace first (#642).
#: Small text: it explains the wait, it is not the answer the user asked for.
_RESTORED_NOTICE = "-# 🔄 停止していたワークスペースを復元してから撮影しました。"

#: The restore itself failed. Says so plainly and points at the path that has the
#: rest of the recovery machinery behind it (announcements, reopen buttons, the
#: untracked-thread notice) rather than leaving the user with a dead command.
_WAKE_FAILED = (
    "⚠️ 停止していたワークスペースの復元に失敗したため、スクリーンショットを撮れませんでした。\n"
    "**このスレッドにメッセージを送れば、通常の経路で復元を試みます。**"
)


def _is_valid_model_id(model: str) -> bool:
    """True if ``model`` is a safe, well-formed model string (alias or full ID)."""
    return bool(_MODEL_ID_RE.fullmatch(model))


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
        devenv_repo: object | None = None,
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.settings_repo = settings_repo
        # #612: where the docker↔workspace link is written down. Optional so
        # consumers constructing the cog themselves keep working, but the
        # standard setup always passes it — without a call site, #573's
        # persistence layer was dead code and its table was never created.
        self._devenv_repo = devenv_repo
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
        model = (model or "").strip()
        if not _is_valid_model_id(model):
            await respond(
                f"❌ Invalid model `{model}`. Use an alias "
                f"({'/'.join(sorted(_VALID_MODELS))}) or a model ID like "
                "`claude-fable-5` (letters, digits, `.` `-` `_`; no spaces).",
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

    async def _model_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for /model set: suggest the tier aliases, and — so any
        well-formed model ID (e.g. ``claude-fable-5``) can be entered without a
        c-lord release — surface a "use custom" entry for a typed, well-formed,
        non-alias value (#478). Fixed ``choices`` would forbid free-form input;
        autocomplete only *suggests*, so the typed value is still submittable."""
        typed = (current or "").strip()
        low = typed.lower()
        choices = [c for c in _MODEL_CHOICES if not low or low in c.value or low in c.name.lower()]
        if typed and typed not in _VALID_MODELS and _is_valid_model_id(typed):
            choices.append(
                app_commands.Choice(name=f'Use "{typed}" (custom model ID)', value=typed)
            )
        return choices[:25]

    @model_group.command(name="set", description="Change the global Claude model for new sessions")
    @app_commands.describe(model="Alias (sonnet/opus/haiku) or a model ID like claude-fable-5")
    @app_commands.autocomplete(model=_model_autocomplete)
    async def model_set(self, interaction: discord.Interaction, model: str) -> None:
        """Set the global default model stored in settings_repo."""
        respond, _ = self._slash_io(interaction)
        await self._model_set_impl(model=model, respond=respond)

    @commands.command(name="model-set")
    async def model_set_text(self, ctx: commands.Context, model: str | None = None) -> None:
        """Text/mention twin of /model set — webhook-invokable for E2E (#209)."""
        if not model:
            await ctx.send(
                f"Usage: `!model-set <{'/'.join(sorted(_VALID_MODELS))}|MODEL_ID>` "
                "(e.g. `claude-fable-5`)"
            )
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

    async def _resolve_tmux_manager(
        self, channel_id: int, *, thread_id: int | None
    ) -> TmuxSessionManager | None:
        """Resolve a TmuxSessionManager for the given channel via ChannelRepoCog.

        #427: pass ``thread_id`` whenever a thread is in scope, so a thread
        bound to its own repo is looked up in that repo's tmux session.
        """
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_tmux_manager(channel_id, thread_id=thread_id)
        return None

    async def _resolve_all_tmux_managers(self) -> list[TmuxSessionManager]:
        """Return all TmuxSessionManager instances across channel bindings."""
        bindings = await self._get_all_bindings()
        managers: list[TmuxSessionManager] = []
        for binding in bindings:
            # Walks channel bindings, not threads (#600 audit).
            mgr = await self._resolve_tmux_manager(binding["channel_id"], thread_id=None)
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
        # /clord-status lists the *channel's* sessions by design (#600 audit).
        tmux_mgr = await self._resolve_tmux_manager(parent_id, thread_id=None)
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
            # Walks channel bindings, not threads (#600 audit).
            tmux_mgr = await self._resolve_tmux_manager(binding["channel_id"], thread_id=None)
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

        tmux_mgr = await self._resolve_tmux_manager(parent_channel_id, thread_id=thread_id)
        if tmux_mgr is None:
            await respond(
                "ℹ️ このチャンネルにはリポジトリが紐づけられていません。"
                " `/clord-init` で設定してください。",
                ephemeral=True,
            )
            return

        window = await asyncio.to_thread(tmux_mgr._find_window_for_thread, thread_id)
        restored = False
        if window is None:
            # #642: a stopped workspace used to end here with a sentence telling
            # the user to send a message. #572 then made "stopped" the ordinary
            # state of anything untouched for four hours, so that sentence became
            # the usual answer and the command stopped returning pictures. Bring
            # the workspace back and photograph it — but only where a message
            # would have restored it too: 終了 is a state the user chose, and an
            # untracked thread has no conversation to restore (#538).
            verdict = classify(await self.repo.get(thread_id))
            if verdict is not ThreadResume.RESUMES:
                await respond(stopped_hint(verdict), ephemeral=True)
                return
            if not await self._wake_workspace(channel):
                await respond(_WAKE_FAILED, ephemeral=True)
                return
            window = await asyncio.to_thread(tmux_mgr._find_window_for_thread, thread_id)
            if window is None:
                logger.warning(
                    "%s wake reported success but no tmux window exists",
                    log_ctx(thread_id=thread_id),
                )
                await respond(_WAKE_FAILED, ephemeral=True)
                return
            restored = True

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
        # Say when the picture cost a restore: the user waited several seconds
        # for something they did not ask for, and the pane they are looking at
        # is a Claude that was not running a moment ago.
        await respond(_RESTORED_NOTICE if restored else None, file=file)

    async def _wake_workspace(self, channel: discord.Thread) -> bool:
        """Restore this thread's stopped workspace, via ClaudeChatCog (#642).

        Loose-coupled through ``bot.get_cog`` (as ``/reopen-workspace`` is): the
        spawn conditions — checkout, tmux window, model, effort, permission flags
        — live with the turn path, and a copy of them here would drift into
        starting a *different* Claude than the next message expects. A consumer
        that never loaded ClaudeChatCog simply cannot wake anything, which is a
        False, not a crash.
        """
        wake = getattr(self.bot.get_cog("ClaudeChatCog"), "wake_workspace", None)
        if wake is None:
            logger.info(
                "%s no ClaudeChatCog — cannot wake this workspace",
                log_ctx(thread_id=channel.id),
            )
            return False
        try:
            return bool(await wake(channel))
        except Exception:
            logger.warning("%s wake failed", log_ctx(thread_id=channel.id), exc_info=True)
            return False

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

    # ── /resync (#439) — reconnect the Discord mirror to tmux ───────────────
    # A user-facing safety valve for when the tmux→Discord mirror feels out of
    # sync (a menu's buttons never showed, an embed looks stale). It does NOT
    # touch the claude process or the session — only re-projects the *current*
    # tmux state onto Discord: (1) re-bridge any stranded TUI menu via the menu
    # watchdog (#359/#420), and (2) post a fresh pane snapshot. Restarting the
    # claude process is /restart-claude (#440); wiping context is /clear (#56).

    async def _find_thread_window(self, thread_id: int) -> tuple[str | None, str | None]:
        """Locate the tmux session+window backing *thread_id* via the @thread_id sweep.

        Reuses ``_list_all_windows`` (no DB dependency), so it works even for
        channels with no ``/clord-init`` binding — same construction the #420
        menu-watchdog fix relies on.
        """
        import asyncio

        from ..thread_state_sync import _list_all_windows

        windows = await asyncio.to_thread(_list_all_windows)
        for w in windows:
            if (w.get("thread_id") or "") == str(thread_id):
                return w.get("session_name") or None, w.get("window_name") or None
        return None, None

    async def _rebridge_menu(self, thread_id: int, session_name: str, window_name: str) -> None:
        """Re-bridge a stranded TUI menu for one thread via the menu watchdog.

        Delegates to the always-on ``MenuWatchdogLoop`` (wired as
        ``bot.menu_watchdog`` in setup.py) so /resync and the 60s sweep share
        one code path — including the #420 manager fallback and the ask_bus /
        is_processing guards that prevent duplicate bridges. No-op (safe) if the
        watchdog is not wired.
        """
        import asyncio

        from ..thread_state_sync import _capture_pane_text

        watchdog = getattr(self.bot, "menu_watchdog", None)
        maybe_bridge = getattr(watchdog, "_maybe_bridge_open_menu", None)
        if maybe_bridge is None:
            return
        pane_text = await asyncio.to_thread(_capture_pane_text, session_name, window_name)
        await maybe_bridge(thread_id, session_name, window_name, pane_text)

    async def _snapshot_pane(
        self, session_name: str, window_name: str, thread_id: int
    ) -> bytes | None:
        """Render the thread's current tmux pane to a PNG (colors + status bar).

        Builds a ``TmuxSessionManager`` straight from the swept session name so
        it works without a channel binding (#420 fallback). Returns None when
        the pane is empty or the optional Pillow extra is missing.
        """
        import asyncio

        from ..tmux import TmuxSessionManager

        tmux_mgr = TmuxSessionManager(session_name=session_name)
        ansi = await asyncio.to_thread(tmux_mgr.capture_screen, thread_id)
        if not ansi.strip():
            return None
        tabs = await asyncio.to_thread(tmux_mgr.list_window_tabs)
        status_bar = (tmux_mgr.session_name, tabs, window_name) if tabs else None
        return await asyncio.to_thread(render_pane_png, ansi, status_bar)

    async def _resolve_channel_session(self, channel: object, parent_id: int | None) -> str | None:
        """Return the tmux session name backing *channel*'s threads.

        Per the per-channel session model (#10), all threads in a channel share
        one tmux session. Prefer the channel's bound manager; fall back to the
        session the invoking thread's window actually lives in (covers unbound
        channels, mirroring the #420 watchdog fallback).
        """
        if parent_id is not None:
            thread_id = channel.id if isinstance(channel, discord.Thread) else None
            tmux_mgr = await self._resolve_tmux_manager(parent_id, thread_id=thread_id)
            if tmux_mgr is not None:
                return tmux_mgr.session_name
        if isinstance(channel, discord.Thread):
            session_name, _ = await self._find_thread_window(channel.id)
            return session_name
        return None

    async def _resync_impl(
        self, *, channel: object, scope: str, respond: _Responder, ack: _Acknowledger
    ) -> None:
        """Shared core for /resync (thread) and /resync-channel (#439)."""
        import asyncio

        if scope == "thread":
            if not isinstance(channel, discord.Thread):
                await respond(
                    "This command can only be used in a Claude chat thread.",
                    ephemeral=True,
                )
                return
            thread_id = channel.id
            await ack()
            session_name, window_name = await self._find_thread_window(thread_id)
            if not session_name or not window_name:
                # Stopped session: which sentence is true depends on what a message would
                # actually do in this thread, which session_resume owns (#464 ②-2, #538).
                await respond(await hint_for_thread(self.repo, thread_id), ephemeral=True)
                return
            # 1. Re-bridge any stranded TUI menu so its buttons (re)appear.
            await self._rebridge_menu(thread_id, session_name, window_name)
            # 2. Post the current pane snapshot so the user sees the live state.
            png = await self._snapshot_pane(session_name, window_name, thread_id)
            if png is not None:
                file = discord.File(
                    BytesIO(png), filename=f"resync-{session_name}-{window_name}.png"
                )
                await respond("🔄 ミラーを今の tmux 状態に繋ぎ直しました。", file=file)
            else:
                await respond(
                    "🔄 ミラーを繋ぎ直しました（pane は空、または PNG 依存 "
                    "`c-lord[table]` が未導入）。"
                )
            return

        # channel scope — resync every thread window in this channel's session.
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None) or channel_id
        await ack()
        session_name = await self._resolve_channel_session(channel, parent_id)
        if not session_name:
            await respond(
                "ℹ️ このチャンネルの tmux セッションが見つかりません。"
                " `/clord-init` でリポジトリを紐づけてください。",
                ephemeral=True,
            )
            return

        from ..thread_state_sync import _list_all_windows

        windows = await asyncio.to_thread(_list_all_windows)
        count = 0
        for w in windows:
            if w.get("session_name") != session_name:
                continue
            tid = w.get("thread_id") or ""
            if not tid.isdigit():
                continue
            await self._rebridge_menu(int(tid), session_name, w.get("window_name") or "")
            count += 1
        await respond(f"🔄 {count} スレッドのミラーを繋ぎ直しました（session={session_name}）。")

    @app_commands.command(
        name="resync",
        description="Reconnect this thread's Discord mirror to its tmux pane",
    )
    async def resync(self, interaction: discord.Interaction) -> None:
        """Re-bridge a stranded menu and post a fresh pane snapshot (#439)."""
        respond, ack = self._slash_io(interaction)
        await self._resync_impl(
            channel=interaction.channel, scope="thread", respond=respond, ack=ack
        )

    @commands.command(name="resync")
    async def resync_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /resync — webhook-invokable for E2E (#439)."""
        respond, ack = self._ctx_io(ctx)
        await self._resync_impl(channel=ctx.channel, scope="thread", respond=respond, ack=ack)

    @app_commands.command(
        name="resync-channel",
        description="Reconnect the Discord mirror for every thread in this channel",
    )
    async def resync_channel(self, interaction: discord.Interaction) -> None:
        """Channel-wide /resync — re-bridge every thread in this channel (#439)."""
        respond, ack = self._slash_io(interaction)
        await self._resync_impl(
            channel=interaction.channel, scope="channel", respond=respond, ack=ack
        )

    @commands.command(name="resync-channel")
    async def resync_channel_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /resync-channel (#439)."""
        respond, ack = self._ctx_io(ctx)
        await self._resync_impl(channel=ctx.channel, scope="channel", respond=respond, ack=ack)

    async def _stop_transcript_mirror(self, thread_id: int) -> None:
        """Stop the TranscriptMirror tailing this thread, if any (#379).

        Workspace teardown (close/delete) kills the tmux window; that can make
        the Claude harness write a final ``<task-notification>`` user-row into
        the JSONL transcript. If the mirror is still tailing, it posts that as a
        ``👤`` echo *after* we archive the thread — and Discord auto-unarchives a
        thread the moment any message lands in it, so the thread never closes
        (#379). Stopping the mirror **before** the kill/archive breaks that loop.

        No-op when ``CLORD_BRIDGE_MODE`` is not ``jsonl`` (the cog stays idle and
        keeps no per-thread mirror) or when the cog is not registered at all, so
        this is safe to call unconditionally (zero-config).
        """
        mirror_cog = self.bot.get_cog("TranscriptMirrorCog")
        stop_for = getattr(mirror_cog, "stop_for", None)
        if stop_for is None:
            return
        try:
            logger.info(
                "%s stopping transcript mirror (workspace teardown)", log_ctx(thread_id=thread_id)
            )
            await stop_for(thread_id)
        except Exception:  # pragma: no cover - defensive; never block teardown
            logger.warning(
                "%s failed to stop transcript mirror during teardown",
                log_ctx(thread_id=thread_id),
                exc_info=True,
            )

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

        # Stop the mirror before tearing down so it doesn't keep tailing a JSONL
        # whose session dir we're about to remove (and can't echo post-teardown) (#379).
        await self._stop_transcript_mirror(thread_id)

        results: list[str] = []

        # Kill tmux window
        tmux_mgr = await self._resolve_tmux_manager(parent_channel_id, thread_id=thread_id)
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

    async def _sleep_workspace_impl(
        self,
        *,
        channel: object,
        reason: WorkspaceReason = WorkspaceReason.IDLE,
        idle_label: str | None = None,
    ) -> bool:
        """スリープ — stop this workspace's Claude and nothing else (#572).

        The innermost of the three lifecycle operations (スリープ ⊂ 停止 ⊂ 削除).
        It kills the tmux window, which is the whole 400 MB, and stops there:

        * **docker keeps running.** A build in progress or a database must not
          die because nobody typed for four hours, and the memory this reclaims
          is Claude's, not docker's — so stopping containers would cost real work
          to buy nothing. Their host ports stay held, which is the one thing the
          user is told about (below).
        * the working copy, the transcript, the volumes and the ``sessions`` row
          are untouched,
        * no ``closed_at``, no rename, no marker. 停止 is a state the user can
          see and undo; sleep is not a state they should ever have to know about.

        Returns ``True`` only when a window was actually killed. The callers —
        :class:`c_lord.idle_sleep.IdleSleepLoop`, and #576's resident cap — count
        that, not the number of calls: a sweep that counts attempts reports
        progress it never made (#604).

        There is no slash command on purpose. Being invisible is the feature; a
        手動 twin would add a concept ("how is this different from 停止?") that
        buys the user nothing.
        """
        if not isinstance(channel, discord.Thread):
            return False

        import asyncio

        thread_id = channel.id
        parent_channel_id = channel.parent_id or thread_id

        tmux_mgr = await self._resolve_tmux_manager(parent_channel_id, thread_id=thread_id)
        if tmux_mgr is None:
            return False

        # Already asleep (or never resident): there is nothing to reclaim, and
        # going further would re-post the docker line on every tick — which is
        # how an invisible feature turns into a nuisance.
        if not await asyncio.to_thread(tmux_mgr.session_exists, thread_id):
            return False

        # Stop the mirror BEFORE the kill (#379): killing the pane can make the
        # harness write a final ``<task-notification>`` row into the transcript,
        # and a mirror still tailing would echo it into the thread as 👤. Sleep
        # must leave no trace.
        await self._stop_transcript_mirror(thread_id)

        if not await asyncio.to_thread(tmux_mgr.kill_session, thread_id):
            logger.info("%s sleep: no tmux window to kill", log_ctx(thread_id=thread_id))
            return False

        # Remember it, so the next message resumes without a word even across a
        # bot restart. Wording only — see ``SessionRepository.set_slept``.
        with contextlib.suppress(Exception):
            await self.repo.set_slept(thread_id, True)

        logger.info(
            "%s workspace slept (reason=%s, idle=%s)",
            log_ctx(thread_id=thread_id),
            reason.value,
            idle_label or "-",
        )

        # ── the one exception to silence ──────────────────────────────────
        # docker was left running on purpose, so this workspace still holds its
        # host ports. The user cannot work that out for themselves and it comes
        # back as a port collision the next time they start an environment
        # (measured: one supabase workspace holds five ports). Everything else
        # about a sleep is deliberately unremarkable, so nothing else is said.
        containers: list[DevContainer] = []
        with contextlib.suppress(Exception):
            sdm = await self._resolve_session_dir_manager(parent_channel_id)
            if sdm is not None:
                session_dir = str(Path(sdm.base_dir) / str(thread_id))
                containers = await containers_for_session_dir(session_dir)

        if any(c.running for c in containers):
            # Built by the shared inventory builder, not a bespoke line: a notice
            # says what stopped *and what survived*, because "did I just lose
            # something?" is the reader's actual question (#571). Two functions
            # producing "the same" message always drift — that was #538.
            embed = workspace_notice_embed(
                WorkspaceAction.SLEEP,
                reason=reason,
                idle_label=idle_label,
                containers=containers,
                docker=DockerOutcome.LEFT_RUNNING,
            )
            with contextlib.suppress(discord.HTTPException):
                await channel.send(embed=embed)

        return True

    async def _close_workspace_impl(
        self,
        *,
        channel: object,
        respond: _Responder,
        ack: _Acknowledger,
        reason: WorkspaceReason = WorkspaceReason.MANUAL,
        idle_label: str | None = None,
    ) -> None:
        """Shared core for /close-workspace and !close-workspace (#271).

        Non-destructive counterpart to ``/workspace-delete``: kills the tmux
        window and archives the thread to declutter, but **keeps** the session
        directory, transcript, and DB session record.  The next message resumes
        the conversation via ``--continue`` (#270) — that is the whole point of
        "close" vs "delete".  Note this never resolves the session-dir manager,
        so the directory-removal path is structurally unreachable here.

        The thread is expected to **stay archived** after this runs.  To guarantee
        that, the TranscriptMirror is stopped *before* the kill/archive
        (:meth:`_stop_transcript_mirror`): otherwise the kill's own
        ``<task-notification>`` would be echoed (👤) into the thread *after* we
        archive it, and Discord auto-unarchives a thread on any new message — so
        it would never close (#379).
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

        # Stop the mirror BEFORE kill/archive so the kill's task-notification can't
        # be echoed (👤) into the thread after we archive it and un-archive it (#379).
        await self._stop_transcript_mirror(thread_id)

        results: list[str] = []

        # Kill the tmux window to free the w<N> slot.
        tmux_mgr = await self._resolve_tmux_manager(parent_channel_id, thread_id=thread_id)
        if tmux_mgr is not None:
            await asyncio.to_thread(tmux_mgr.kill_session, thread_id)
        else:
            results.append(
                "ℹ️ このチャンネルにはリポジトリが紐づけられていません。"
                " `/clord-init` で設定してください。"
            )

        # #512: record the close so a later message can tell this apart from a
        # pane that merely died — the latter still auto-resumes via --continue
        # (#270), this one holds the message and offers the reopen button.
        with contextlib.suppress(Exception):
            await self.repo.set_closed(thread_id, True, reason=reason.value)

        # Rename to "[終了] …" **and** archive (#271) in a single PATCH. They must
        # not be two calls: Discord refuses to rename an archived thread (code
        # 50083), and each rename spends one of the thread's ~2-per-10-minutes
        # allowance — two edits make a 429 (and a silently lost marker) twice as
        # likely. apply_closed_name falls back to archive-only if the rename half
        # fails, so a missing Manage Threads permission still leaves the thread
        # archived (#512).

        # #574: stop the dev environment too. Leaving it running is how 12
        # supabase containers ended up on the production host holding ports
        # 55321-55327 with no owner: the workspace was gone, so nothing was left
        # that knew they existed. Stopping frees the ports; the volumes (the
        # actual data) are untouched — see docs/specs/workspace-vocabulary.md.
        containers: list[DevContainer] = []
        # Only a discovery that actually completed may be written down. An empty
        # list from a *failed* docker call is indistinguishable from "nothing is
        # running" at the call site, and recording it would mark live containers
        # as gone — losing the very ownership record #612 exists to keep.
        discovered_dir: str | None = None
        with contextlib.suppress(Exception):
            sdm = await self._resolve_session_dir_manager(parent_channel_id)
            if sdm is not None:
                session_dir = str(Path(sdm.base_dir) / str(thread_id))
                containers = await containers_for_session_dir(session_dir)
                discovered_dir = session_dir

        # #612: write the link down *before* stopping. This is the last moment
        # anything knows these containers belong to this thread — after the
        # workspace goes, only this row can answer "who is holding port 55322?".
        if self._devenv_repo is not None and discovered_dir is not None:
            with contextlib.suppress(Exception):
                await self._devenv_repo.record(  # type: ignore[attr-defined]
                    thread_id, discovered_dir, containers
                )

        docker_outcome = DockerOutcome.NONE
        if containers:
            stopped = await stop_containers(containers)
            # Report what actually happened, not what was attempted: a docker
            # that refused leaves the ports held, and the notice must say so
            # rather than announcing a release that did not occur (#571).
            docker_outcome = DockerOutcome.STOPPED if stopped else DockerOutcome.LEFT_RUNNING

        # #571: the notice is an inventory, not a label — it says what stopped
        # *and* what survived, because that is the question the reader has.
        # Built by the one shared function so the manual and automatic notices
        # cannot drift (the #538 failure mode).
        embed = workspace_notice_embed(
            WorkspaceAction.STOP,
            reason=reason,
            idle_label=idle_label,
            containers=containers,
            docker=docker_outcome,
        )
        if results:
            embed.add_field(name="メモ", value="\n".join(results), inline=False)

        # #607: post *before* archiving. Discord un-archives a thread the moment
        # anything is posted to it, so archiving first meant every automatically
        # stopped thread bounced straight back open — measured in production,
        # 6 out of 6 ended up ``archived=False`` and kept cluttering the sidebar.
        # The rename lost too: Discord refuses to rename an archived thread
        # (code 50083), so the ``[停止]`` marker never landed either.
        await respond(embed=embed)

        new_name = await apply_closed_name(self.repo, channel)
        if new_name:
            logger.info(
                "%s stopped workspace renamed to %r",
                log_ctx(thread_id=thread_id),
                new_name,
            )

    @app_commands.command(
        name="workspace-stop",
        description="停止: stop Claude and the dev environment, keep everything else",
    )
    async def workspace_stop(self, interaction: discord.Interaction) -> None:
        """停止 — kill the tmux window and the dev environment (#574).

        Named object-first (``workspace-stop``) because that is already the
        majority convention in this command set (9 of 13 that take an object) and
        because Discord's autocomplete groups by prefix: typing ``/workspace``
        surfaces the whole lifecycle together, which ``close-workspace`` never
        did.
        """
        respond, ack = self._slash_io(interaction)
        await self._close_workspace_impl(channel=interaction.channel, respond=respond, ack=ack)

    @commands.command(name="workspace-stop")
    async def workspace_stop_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /workspace-stop — webhook-invokable for E2E (#271)."""
        respond, ack = self._ctx_io(ctx)
        await self._close_workspace_impl(channel=ctx.channel, respond=respond, ack=ack)

    @app_commands.command(
        name="close-workspace",
        description="(旧名) /workspace-stop と同じです",
    )
    async def close_workspace(self, interaction: discord.Interaction) -> None:
        """Old name for :meth:`workspace_stop`, kept working (#574).

        Renaming a command people have in their fingers is not free. The alias
        calls the same implementation, so there is nothing to keep in sync — and
        consumers get the new name by updating the package alone, which is the
        Zero-Config Principle.
        """
        respond, ack = self._slash_io(interaction)
        await self._close_workspace_impl(channel=interaction.channel, respond=respond, ack=ack)

    @commands.command(name="close-workspace")
    async def close_workspace_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of the /close-workspace alias (#271)."""
        respond, ack = self._ctx_io(ctx)
        await self._close_workspace_impl(channel=ctx.channel, respond=respond, ack=ack)

    async def _reopen_workspace_impl(
        self, *, channel: object, respond: _Responder, ack: _Acknowledger
    ) -> None:
        """Shared core for /reopen-workspace and !reopen-workspace (#512).

        Inverse of :meth:`_close_workspace_impl`: clears ``closed_at`` and drops
        the ``[終了]`` marker from the thread name, so the next message runs
        normally again (resuming the kept session via ``--continue``, #270).

        Deliberately does **not** recreate the tmux window — that happens on the
        next message like any other resume, which keeps one spawn path instead of
        two.
        """
        if not isinstance(channel, discord.Thread):
            await respond(
                "This command can only be used in a Claude chat thread.",
                ephemeral=True,
            )
            return

        await ack()

        record = await self.repo.get(channel.id)
        if record is None:
            await respond("ℹ️ このスレッドには c-lord のワークスペースがありません。")
            return
        if not is_closed(record):
            await respond(
                "ℹ️ このワークスペースは停止していません（そのままメッセージを送れます）。"
            )
            return

        await self.repo.set_closed(channel.id, False)
        # Let the chat cog know this was a deliberate reopen, so the resume it
        # performs on the next message says so instead of reporting a crash
        # ("前回のセッションが落ちていたので…", #464). Loose-coupled through
        # bot.get_cog so this command stays usable when the cog is absent (#512).
        mark_reopened = getattr(self.bot.get_cog("ClaudeChatCog"), "mark_reopened", None)
        if mark_reopened is not None:
            with contextlib.suppress(Exception):
                mark_reopened(channel.id)
        # #607: capture the stopped name before the rename wipes it. It carries
        # the window number, and reopening is exactly when that handle is lost.
        old_name = channel.name if isinstance(channel.name, str) else ""
        new_name = await apply_open_name(self.repo, channel)

        lines = [reopen_rename_notice(old_name, new_name) or f"🏷️ スレッド名: `{new_name}`"]
        lines.append("💬 このスレッドにメッセージを送ると、これまでの会話の続きから再開します。")

        embed = discord.Embed(
            title="▶️ このスレッドのワークスペースを再開しました",
            description="\n".join(lines),
            color=COLOR_SUCCESS,
        )
        await respond(embed=embed)

    @app_commands.command(
        name="workspace-start",
        description="再開: start a stopped (停止) workspace so messages run again",
    )
    async def workspace_start(self, interaction: discord.Interaction) -> None:
        """再開 — clear the 停止 state (#512, renamed in #574).

        ``start`` rather than ``reopen`` because it is the inverse of ``stop``:
        open/close is about visibility, start/stop is about running state, and
        running state is what this actually changes.
        """
        respond, ack = self._slash_io(interaction)
        await self._reopen_workspace_impl(channel=interaction.channel, respond=respond, ack=ack)

    @commands.command(name="workspace-start")
    async def workspace_start_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /workspace-start — webhook-invokable for E2E (#512)."""
        respond, ack = self._ctx_io(ctx)
        await self._reopen_workspace_impl(channel=ctx.channel, respond=respond, ack=ack)

    @app_commands.command(
        name="reopen-workspace",
        description="(旧名) /workspace-start と同じです",
    )
    async def reopen_workspace(self, interaction: discord.Interaction) -> None:
        """Old name for :meth:`workspace_start`, kept working (#574)."""
        respond, ack = self._slash_io(interaction)
        await self._reopen_workspace_impl(channel=interaction.channel, respond=respond, ack=ack)

    @commands.command(name="reopen-workspace")
    async def reopen_workspace_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of the /reopen-workspace alias (#512)."""
        respond, ack = self._ctx_io(ctx)
        await self._reopen_workspace_impl(channel=ctx.channel, respond=respond, ack=ack)
