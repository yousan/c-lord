"""Channel-repository binding Cog.

Provides ``/clord-init`` to dynamically bind a Discord channel to a git
repository so that each channel can have its own SessionDirManager.

Usage::

    /clord-init repo:https://github.com/org/project.git branch:main
    /clord-init remove:True
    /clord-init                       # show current bindings
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from ..database.channel_repo import ChannelRepository

from ..database.channel_repo import derive_session_name
from ..session_dir import SessionDirManager
from ..tmux import TmuxSessionManager

logger = logging.getLogger(__name__)


class ChannelRepoCog(commands.Cog):
    """Manages per-channel repository bindings and SessionDirManager cache."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        repo: ChannelRepository,
        allowed_user_ids: set[int] | None = None,
        session_dir_base: str | None = None,
        allowed_role_name: str | None = None,
    ) -> None:
        self.bot = bot
        self._repo = repo
        self._allowed_user_ids = allowed_user_ids
        self._allowed_role_name = allowed_role_name
        self._session_dir_base = session_dir_base
        self._manager_cache: dict[int, SessionDirManager] = {}
        self._tmux_cache: dict[int, TmuxSessionManager] = {}

    # ------------------------------------------------------------------
    # Public API (used by ClaudeChatCog)
    # ------------------------------------------------------------------

    async def resolve_manager(self, channel_id: int) -> SessionDirManager | None:
        """Resolve a SessionDirManager for the given channel.

        Lookup order:
          1. In-memory cache
          2. DB binding → create manager and cache it
          3. None (caller should fall back to global bot.session_dir_manager)
        """
        if channel_id in self._manager_cache:
            return self._manager_cache[channel_id]

        binding = await self._repo.get(channel_id)
        if binding is None:
            return None

        base = self._session_dir_base or "data/sessions"
        manager = SessionDirManager(
            base_dir=str(Path(base) / str(channel_id)),
            source_repo=binding["source_repo"],
            clone_branch=binding["clone_branch"],
        )
        self._manager_cache[channel_id] = manager
        return manager

    async def resolve_tmux_manager(self, channel_id: int) -> TmuxSessionManager | None:
        """Resolve a TmuxSessionManager for the given channel.

        Lookup order:
          1. In-memory cache
          2. DB binding → create manager with derived/explicit session name
          3. None (caller should fall back to global bot.tmux_manager)
        """
        if channel_id in self._tmux_cache:
            return self._tmux_cache[channel_id]

        binding = await self._repo.get(channel_id)
        if binding is None:
            return None

        session_name = binding["tmux_session_name"] or derive_session_name(binding["source_repo"])
        manager = TmuxSessionManager(session_name=session_name)
        self._tmux_cache[channel_id] = manager
        return manager

    def evict_cache(self, channel_id: int) -> None:
        """Remove a cached manager (called on bind/unbind)."""
        self._manager_cache.pop(channel_id, None)
        self._tmux_cache.pop(channel_id, None)

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def _is_allowed(self, member: discord.Member | discord.User | int) -> bool:
        """Check if a member/user is authorized.

        Accepts a Member, User, or bare int (user ID) for backward compatibility.
        """
        if isinstance(member, int):
            user_id = member
            if self._allowed_user_ids is not None and user_id in self._allowed_user_ids:
                return True
            return self._allowed_user_ids is None and self._allowed_role_name is None

        if self._allowed_user_ids is not None and member.id in self._allowed_user_ids:
            return True
        if self._allowed_role_name is not None:
            if isinstance(member, discord.Member):
                return any(r.name == self._allowed_role_name for r in member.roles)
            return False  # DM — no role info
        return self._allowed_user_ids is None

    # ------------------------------------------------------------------
    # Slash command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="clord-init",
        description="Bind this channel to a git repository for Claude Code sessions",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        repo="Git repository URL to clone for sessions in this channel",
        branch="Branch to clone (default: repo default branch)",
        tmux_session="tmux session name (default: derived from repo URL)",
        remove="Set True to remove the binding for this channel",
    )
    async def clord_init(
        self,
        interaction: discord.Interaction,
        repo: str | None = None,
        branch: str | None = None,
        tmux_session: str | None = None,
        remove: bool = False,
    ) -> None:
        """Bind / unbind / show channel-repo bindings."""
        if not self._is_allowed(interaction.user):
            await interaction.response.send_message(
                "You are not authorized to use this command.", ephemeral=True
            )
            return

        channel_id = interaction.channel_id
        assert channel_id is not None

        # --- Remove binding ---
        if remove:
            deleted = await self._repo.delete(channel_id)
            self.evict_cache(channel_id)
            if deleted:
                await interaction.response.send_message(
                    f"Removed repository binding for <#{channel_id}>.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "No binding found for this channel.", ephemeral=True
                )
            return

        # --- Show bindings (no args) ---
        if repo is None:
            bindings = await self._repo.list_all()
            if not bindings:
                await interaction.response.send_message(
                    "No channel-repo bindings configured.", ephemeral=True
                )
                return

            lines = []
            for b in bindings:
                branch_str = f" (branch: `{b['clone_branch']}`)" if b["clone_branch"] else ""
                tmux_name = b.get("tmux_session_name") or derive_session_name(b["source_repo"])
                tmux_str = f" (tmux: `{tmux_name}`)"
                lines.append(f"<#{b['channel_id']}> → `{b['source_repo']}`{branch_str}{tmux_str}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
            return

        # --- Bind channel to repo ---
        await self._repo.save(
            channel_id=channel_id,
            source_repo=repo,
            clone_branch=branch,
            tmux_session_name=tmux_session,
        )
        self.evict_cache(channel_id)

        branch_msg = f" (branch: `{branch}`)" if branch else ""
        tmux_display = tmux_session or derive_session_name(repo)
        tmux_msg = f" (tmux: `{tmux_display}`)"
        await interaction.response.send_message(
            f"Bound <#{channel_id}> → `{repo}`{branch_msg}{tmux_msg}", ephemeral=True
        )
