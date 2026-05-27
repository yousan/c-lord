"""Session management Cog.

Provides slash commands for viewing and managing Claude Code sessions:
- /resume-info: Show CLI resume command for the current thread's session
- /sessions: List all known sessions
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..database.repository import SessionRepository
from ..database.settings_repo import SettingsRepository
from ..discord_ui.embeds import COLOR_INFO, COLOR_SUCCESS, COLOR_TOOL
from ..session_dir import SessionDirManager

if TYPE_CHECKING:
    from ..bot import ClaudeDiscordBot
    from ..tmux import TmuxSessionManager

logger = logging.getLogger(__name__)

_ORIGIN_ICON = {
    "discord": "\U0001f4ac",  # 💬
    "cli": "\U0001f5a5\ufe0f",  # 🖥️
}


def _is_tmux_session(session_id: str) -> bool:
    """Check if a session ID represents a tmux-managed session."""
    return session_id.startswith("tmux-")


def _format_session_short(session_id: str, *, window_name: str | None = None) -> str:
    """Format a session ID for display.

    For tmux sessions: show window_name if available, otherwise 'tmux'.
    For CLI sessions: show first 8 chars of the session ID.
    """
    if _is_tmux_session(session_id):
        return window_name or "tmux"
    return session_id[:8]


# Model management
SETTING_CLAUDE_MODEL = "claude_model"
_VALID_MODELS = {"haiku", "sonnet", "opus"}
_MODEL_CHOICES = [
    app_commands.Choice(name="Haiku 4.5 (fast, cost-effective)", value="haiku"),
    app_commands.Choice(name="Sonnet 4.6 (balanced, default)", value="sonnet"),
    app_commands.Choice(name="Opus 4.7 (powerful, deep reasoning)", value="opus"),
]


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

    # ── Model commands ────────────────────────────────────────────────────────

    model_group = app_commands.Group(
        name="model",
        description="View and change the Claude model used for new sessions",
    )

    @model_group.command(name="show", description="Show the current Claude model")
    async def model_show(self, interaction: discord.Interaction) -> None:
        """Display the current global model and, if in a thread, the per-session model."""
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
        if isinstance(interaction.channel, discord.Thread):
            record = await self.repo.get(interaction.channel.id)
            if record and record.model:
                embed.add_field(
                    name="This thread's last session",
                    value=f"`{record.model}`",
                    inline=False,
                )

        embed.set_footer(text=f"Effective model for new sessions: {effective_model}")
        await interaction.response.send_message(embed=embed)

    @model_group.command(name="set", description="Change the global Claude model for new sessions")
    @app_commands.describe(model="Model to use for all new Claude sessions")
    @app_commands.choices(model=_MODEL_CHOICES)
    async def model_set(self, interaction: discord.Interaction, model: str) -> None:
        """Set the global default model stored in settings_repo."""
        if model not in _VALID_MODELS:
            await interaction.response.send_message(
                f"❌ Unknown model `{model}`. Valid choices: {', '.join(sorted(_VALID_MODELS))}",
                ephemeral=True,
            )
            return

        if self.settings_repo is None:
            await interaction.response.send_message(
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
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="resume-info",
        description="Show the CLI command to resume this thread's session",
    )
    async def resume_info(self, interaction: discord.Interaction) -> None:
        """Show the claude --resume command for the current thread."""
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "This command can only be used in a Claude chat thread.",
                ephemeral=True,
            )
            return

        record = await self.repo.get(interaction.channel.id)
        if not record:
            await interaction.response.send_message(
                "No session found for this thread.",
                ephemeral=True,
            )
            return

        if _is_tmux_session(record.session_id):
            # tmux session — show tmux attach instructions
            thread_id = interaction.channel.id
            tmux_mgr = await self._resolve_tmux_manager(interaction.channel.parent_id or thread_id)
            window_name: str | None = None
            session_name: str | None = None
            if tmux_mgr is not None:
                import asyncio

                window_name = await asyncio.to_thread(tmux_mgr._find_window_for_thread, thread_id)
                session_name = tmux_mgr.session_name

            if window_name and session_name:
                cmd = f"tmux attach -t {session_name}:{window_name}"
            elif session_name:
                cmd = f"tmux attach -t {session_name}"
            else:
                cmd = "tmux attach"

            embed = discord.Embed(
                title="\U0001f517 Resume via tmux",
                description=(
                    f"This session is managed by tmux.\n\n"
                    f"```\n{cmd}\n```\n"
                    f"Run this command to attach to the session."
                ),
                color=COLOR_INFO,
            )
        else:
            embed = discord.Embed(
                title="\U0001f517 Resume from CLI",
                description=(
                    f"```\nclaude --resume {record.session_id}\n```\n"
                    f"Run this command in your terminal to continue this session."
                ),
                color=COLOR_INFO,
            )
        if record.working_dir:
            embed.add_field(name="Working Directory", value=f"`{record.working_dir}`", inline=True)
        if record.model:
            embed.add_field(name="Model", value=record.model, inline=True)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="sessions",
        description="List all known Claude Code sessions",
    )
    async def sessions_list(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """List all sessions with origin, summary, and last activity."""
        records = await self.repo.list_all(limit=25)

        if not records:
            embed = discord.Embed(
                title="\U0001f4cb Sessions",
                description="No sessions found.",
                color=COLOR_INFO,
            )
            await interaction.response.send_message(embed=embed)
            return

        embed = discord.Embed(
            title=f"\U0001f4cb Sessions ({len(records)})",
            color=COLOR_INFO,
        )

        # Build thread→window mapping from all tmux managers for display
        has_tmux = any(_is_tmux_session(r.session_id) for r in records)
        thread_to_window: dict[int, str] = {}
        if has_tmux:
            import asyncio

            managers = await self._resolve_all_tmux_managers()
            for mgr in managers:
                await asyncio.to_thread(mgr._rebuild_mapping)
                thread_to_window.update(mgr._thread_to_window)

        for record in records:
            icon = _ORIGIN_ICON.get(record.origin, "\u2753")
            summary = record.summary or "(no summary)"
            window_name = thread_to_window.get(record.thread_id)
            session_short = _format_session_short(record.session_id, window_name=window_name)

            name = f"{icon} {summary[:50]}"
            if _is_tmux_session(record.session_id):
                value = f"`{session_short}` | {record.last_used_at}"
            else:
                value = f"`{session_short}...` | {record.last_used_at}"
            if record.working_dir:
                # Show just the last directory component
                dir_short = record.working_dir.rsplit("/", 1)[-1]
                value += f" | `{dir_short}`"

            embed.add_field(name=name, value=value, inline=False)

        await interaction.response.send_message(embed=embed)

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

    @app_commands.command(
        name="session-dirs",
        description="List all active Claude Code session directories",
    )
    async def session_dirs_list(self, interaction: discord.Interaction) -> None:
        """Show all session directories and their status (across all bindings)."""
        bindings = await self._get_all_bindings()
        if not bindings:
            await interaction.response.send_message(
                "❌ No channel-repo bindings configured. Use `/clord-init` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        import asyncio

        all_dirs = []
        for binding in bindings:
            sdm = await self._resolve_session_dir_manager(binding["channel_id"])
            if sdm is not None:
                dirs = await asyncio.to_thread(sdm.find_session_dirs)
                all_dirs.extend(dirs)

        if not all_dirs:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="📁 Session Directories",
                    description="No session directories found.",
                    color=COLOR_INFO,
                )
            )
            return

        embed = discord.Embed(
            title=f"📁 Session Directories ({len(all_dirs)})",
            color=COLOR_INFO,
        )
        for d in all_dirs:
            status = "✅ clean" if d.is_clean else "⚠️ dirty"
            name = f"`{d.thread_id}`"
            value = f"Path: `{d.path}`\nCommit: `{d.commit or 'unknown'}`\nStatus: {status}"
            embed.add_field(name=name, value=value, inline=False)

        await interaction.followup.send(embed=embed)

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
        bindings = await self._get_all_bindings()
        if not bindings:
            await interaction.response.send_message(
                "❌ No channel-repo bindings configured. Use `/clord-init` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

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
            await interaction.followup.send(
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
            await interaction.followup.send(embed=embed)
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

        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="tmux-list",
        description="List all active tmux windows for Claude Code",
    )
    async def tmux_list(self, interaction: discord.Interaction) -> None:
        """Show all windows across all channel tmux sessions."""
        bindings = await self._get_all_bindings()
        if not bindings:
            await interaction.response.send_message(
                "❌ No channel-repo bindings configured. Use `/clord-init` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        import asyncio

        all_windows: list[dict] = []
        for binding in bindings:
            tmux_mgr = await self._resolve_tmux_manager(binding["channel_id"])
            if tmux_mgr is not None:
                windows = await asyncio.to_thread(tmux_mgr.list_sessions)
                all_windows.extend(windows)

        if not all_windows:
            await interaction.followup.send(
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

        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="workspace-delete",
        description="Delete the tmux window and session directory for this thread",
    )
    async def workspace_delete(self, interaction: discord.Interaction) -> None:
        """Delete the tmux window and session directory for the current thread."""
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "This command can only be used in a Claude chat thread.",
                ephemeral=True,
            )
            return

        thread_id = interaction.channel.id
        parent_channel_id = interaction.channel.parent_id or thread_id
        await interaction.response.defer()

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
        await interaction.followup.send(embed=embed)
