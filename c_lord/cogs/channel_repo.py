"""Channel-repository binding Cog.

Provides ``/clord-init`` to dynamically bind a Discord channel to a git
repository so that each channel can have its own SessionDirManager.
Provides ``/clord-thread-init`` to override the binding for a specific thread.

Usage::

    /clord-init repo:https://github.com/org/project.git
    /clord-init remove:True
    /clord-init                              # show current bindings (channel + thread)
    /clord-thread-init repo:https://github.com/org/other.git
    /clord-thread-init remove:True
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from ..database.channel_repo import ChannelRepository
    from ..database.thread_repo import ThreadRepository

from ..database.channel_repo import (
    derive_session_name,
    normalize_repo_url,
    validate_repo_url,
)
from ..session_dir import SessionDirManager
from ..tmux import TmuxSessionManager

logger = logging.getLogger(__name__)

# Posts a reply for either a slash interaction or a !text twin (#209 follow-up).
_Responder = Callable[..., Awaitable[None]]


class ChannelRepoCog(commands.Cog):
    """Manages per-channel and per-thread repository bindings and SessionDirManager cache."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        repo: ChannelRepository,
        thread_repo: ThreadRepository | None = None,
        allowed_user_ids: set[int] | None = None,
        session_dir_base: str | None = None,
        allowed_role_name: str | None = None,
    ) -> None:
        self.bot = bot
        self._repo = repo
        self._thread_repo = thread_repo
        self._allowed_user_ids = allowed_user_ids
        self._allowed_role_name = allowed_role_name
        self._session_dir_base = session_dir_base
        self._manager_cache: dict[int, SessionDirManager] = {}
        self._thread_manager_cache: dict[int, SessionDirManager] = {}
        self._tmux_cache: dict[int, TmuxSessionManager] = {}
        self._thread_tmux_cache: dict[int, TmuxSessionManager] = {}

    # ------------------------------------------------------------------
    # Public API (used by ClaudeChatCog)
    # ------------------------------------------------------------------

    async def resolve_manager(
        self, channel_id: int, thread_id: int | None = None
    ) -> SessionDirManager | None:
        """Resolve a SessionDirManager for the given channel (and optional thread).

        Lookup order:
          1. Thread-level: in-memory thread cache → DB thread binding
          2. Channel-level: in-memory channel cache → DB channel binding
          3. None (caller should fall back to global bot.session_dir_manager)

        When thread_id is provided and a thread binding exists, the returned
        manager uses the thread's source_repo instead of the channel's.
        tmux session name is always derived from the channel binding (unchanged).
        """
        # --- Thread-level override ---
        if thread_id is not None and self._thread_repo is not None:
            if thread_id in self._thread_manager_cache:
                return self._thread_manager_cache[thread_id]

            thread_binding = await self._thread_repo.get(thread_id)
            if thread_binding is not None:
                base = self._session_dir_base or "data/sessions"
                manager = SessionDirManager(
                    base_dir=str(Path(base) / str(channel_id)),
                    source_repo=thread_binding["source_repo"],
                )
                self._thread_manager_cache[thread_id] = manager
                return manager

        # --- Channel-level fallback ---
        if channel_id in self._manager_cache:
            return self._manager_cache[channel_id]

        binding = await self._repo.get(channel_id)
        if binding is None:
            return None

        base = self._session_dir_base or "data/sessions"
        manager = SessionDirManager(
            base_dir=str(Path(base) / str(channel_id)),
            source_repo=binding["source_repo"],
        )
        self._manager_cache[channel_id] = manager
        return manager

    async def resolve_tmux_manager(
        self, channel_id: int, thread_id: int | None = None
    ) -> TmuxSessionManager | None:
        """Resolve a TmuxSessionManager for the given channel (and optional thread).

        Lookup order mirrors :meth:`resolve_manager`:
          1. Thread-level: in-memory thread cache → DB thread binding
          2. Channel-level: in-memory channel cache → DB channel binding
          3. None (caller should fall back to global bot.tmux_manager)

        #427: this used to resolve the channel binding only, so a thread bound
        to another repo via ``/clord-thread-init`` got its session_dir from the
        thread's repo but its tmux window in the *parent channel's* session —
        ``tmux attach`` then showed a session name that did not match the
        checkout inside it. Honouring the thread binding here keeps the two in
        step, and gives a thread on an unbound channel a session at all.
        """
        # --- Thread-level override ---
        if thread_id is not None and self._thread_repo is not None:
            if thread_id in self._thread_tmux_cache:
                return self._thread_tmux_cache[thread_id]

            thread_binding = await self._thread_repo.get(thread_id)
            if thread_binding is not None:
                manager = TmuxSessionManager(
                    session_name=derive_session_name(thread_binding["source_repo"])
                )
                self._thread_tmux_cache[thread_id] = manager
                return manager

        # --- Channel-level fallback ---
        if channel_id in self._tmux_cache:
            return self._tmux_cache[channel_id]

        binding = await self._repo.get(channel_id)
        if binding is None:
            return None

        session_name = derive_session_name(binding["source_repo"])
        manager = TmuxSessionManager(session_name=session_name)
        self._tmux_cache[channel_id] = manager
        return manager

    async def managed_session_names(self) -> set[str]:
        """tmux session names this bot manages (#438).

        Used by the menu watchdog to ignore other bots' sessions on a shared
        tmux server. Derived from this bot's channel bindings, plus the global
        default session (``clord``) so that windows for an unbound channel
        (#420) are still recognised as ours.
        """
        from ..tmux import SESSION_NAME

        names: set[str] = {SESSION_NAME}
        bindings = list(await self._repo.list_all())
        # #427: thread bindings now get their own tmux session, so those names
        # are ours as well — otherwise the watchdog reads them as another bot's.
        if self._thread_repo is not None:
            with contextlib.suppress(Exception):
                bindings += list(await self._thread_repo.list_all())
        for binding in bindings:
            with contextlib.suppress(Exception):
                names.add(derive_session_name(binding["source_repo"]))
        return names

    async def bind_thread(self, thread_id: int, source_repo: str, channel_id: int) -> str | None:
        """Bind a thread to *source_repo* and drop its cached managers (#514).

        The extension point ``/clord repo:`` needs: writing the binding by hand
        would leave ``_thread_manager_cache`` / ``_thread_tmux_cache`` holding
        the previous answer, and the very next resolve would clone the wrong
        repo. Returns the normalized repo URL, or None when this bot has no
        thread-binding repository wired up.
        """
        if self._thread_repo is None:
            return None
        repo = normalize_repo_url(validate_repo_url(source_repo))
        await self._thread_repo.save(thread_id=thread_id, source_repo=repo, channel_id=channel_id)
        self.evict_thread_cache(thread_id)
        logger.info("Bound thread %d -> %s (channel %d)", thread_id, repo, channel_id)
        return repo

    async def known_repos(self) -> list[str]:
        """Every repo this bot already knows about, most recently bound first.

        Feeds the ``/clord repo:`` autocomplete so the option offers real
        choices instead of an empty box.
        """
        seen: dict[str, None] = {}
        with contextlib.suppress(Exception):
            for binding in reversed(await self._repo.list_all()):
                seen.setdefault(binding["source_repo"], None)
        if self._thread_repo is not None:
            with contextlib.suppress(Exception):
                for binding in reversed(await self._thread_repo.list_all()):
                    seen.setdefault(binding["source_repo"], None)
        return list(seen)

    async def channel_repo(self, channel_id: int) -> str | None:
        """The repo a channel is bound to, or None. Used to show the default."""
        binding = await self._repo.get(channel_id)
        return binding["source_repo"] if binding else None

    def evict_cache(self, channel_id: int) -> None:
        """Remove a cached channel manager (called on bind/unbind)."""
        self._manager_cache.pop(channel_id, None)
        self._tmux_cache.pop(channel_id, None)

    def evict_thread_cache(self, thread_id: int) -> None:
        """Remove a cached thread manager (called on thread bind/unbind)."""
        self._thread_manager_cache.pop(thread_id, None)
        self._thread_tmux_cache.pop(thread_id, None)

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

    # Lets clord-init / clord-thread-init run from a slash interaction or a
    # !text twin without duplicating the body (#209 follow-up).
    @staticmethod
    def _slash_respond(interaction: discord.Interaction) -> _Responder:
        async def respond(content: str | None = None, *, ephemeral: bool = False) -> None:
            await interaction.response.send_message(content, ephemeral=ephemeral)

        return respond

    @staticmethod
    def _ctx_respond(ctx: commands.Context) -> _Responder:
        async def respond(content: str | None = None, *, ephemeral: bool = False) -> None:
            await ctx.send(content or "")

        return respond

    # ------------------------------------------------------------------
    # Slash command
    # ------------------------------------------------------------------

    async def _clord_init_impl(
        self,
        *,
        channel_id: int | None,
        user: discord.Member | discord.User,
        repo: str | None,
        remove: bool,
        respond: _Responder,
    ) -> None:
        """Shared core for /clord-init and !clord-init (#209 follow-up)."""
        if not self._is_allowed(user):
            await respond("You are not authorized to use this command.", ephemeral=True)
            return

        assert channel_id is not None

        # --- Remove binding ---
        if remove:
            deleted = await self._repo.delete(channel_id)
            self.evict_cache(channel_id)
            if deleted:
                await respond(f"Removed repository binding for <#{channel_id}>.", ephemeral=True)
            else:
                await respond("No binding found for this channel.", ephemeral=True)
            return

        # --- Show bindings (no args) ---
        if repo is None:
            channel_bindings = await self._repo.list_all()
            thread_bindings = (
                await self._thread_repo.list_all() if self._thread_repo is not None else []
            )
            if not channel_bindings and not thread_bindings:
                await respond("No channel-repo bindings configured.", ephemeral=True)
                return

            lines = []
            for b in channel_bindings:
                tmux_name = derive_session_name(b["source_repo"])
                lines.append(f"<#{b['channel_id']}> → `{b['source_repo']}` (tmux: `{tmux_name}`)")
            for b in thread_bindings:
                ch_ref = f" (channel <#{b['channel_id']}>)" if b.get("channel_id") else ""
                lines.append(f"  thread <#{b['thread_id']}>{ch_ref} → `{b['source_repo']}`")
            await respond("\n".join(lines), ephemeral=True)
            return

        # --- Bind channel to repo ---
        try:
            repo = normalize_repo_url(validate_repo_url(repo))  # PR/issue/blob → repo root (#88)
        except ValueError as exc:
            await respond(f"⚠️ リポジトリ URL が不正です: {exc}", ephemeral=True)
            return
        await self._repo.save(channel_id=channel_id, source_repo=repo)
        self.evict_cache(channel_id)

        tmux_display = derive_session_name(repo)
        await respond(f"Bound <#{channel_id}> → `{repo}` (tmux: `{tmux_display}`)", ephemeral=True)

    @app_commands.command(
        name="clord-init",
        description="Bind this channel to a git repository for Claude Code sessions",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        repo="Git repository URL to clone for sessions in this channel",
        remove="Set True to remove the binding for this channel",
    )
    async def clord_init(
        self,
        interaction: discord.Interaction,
        repo: str | None = None,
        remove: bool = False,
    ) -> None:
        """Bind / unbind / show channel-repo bindings."""
        await self._clord_init_impl(
            channel_id=interaction.channel_id,
            user=interaction.user,
            repo=repo,
            remove=remove,
            respond=self._slash_respond(interaction),
        )

    @commands.command(name="clord-init")
    async def clord_init_text(self, ctx: commands.Context, arg: str | None = None) -> None:
        """Text/mention twin of /clord-init — webhook-invokable for E2E (#209).

        Usage: ``!clord-init`` (show) / ``!clord-init <repo-url>`` (bind) /
        ``!clord-init remove`` (unbind this channel).
        Gated by ``_is_allowed`` only (no Discord Manage-Server check, unlike the
        slash command), so restrict the allowlist in production accordingly.
        """
        repo: str | None = None
        remove = False
        if arg == "remove":
            remove = True
        elif arg:
            repo = arg
        await self._clord_init_impl(
            channel_id=ctx.channel.id,
            user=ctx.author,
            repo=repo,
            remove=remove,
            respond=self._ctx_respond(ctx),
        )

    async def _clord_thread_init_impl(
        self,
        *,
        thread_id: int | None,
        channel: object,
        client: discord.Client,
        user: discord.Member | discord.User,
        repo: str | None,
        remove: bool,
        respond: _Responder,
    ) -> None:
        """Shared core for /clord-thread-init and !clord-thread-init (#209 follow-up)."""
        if not self._is_allowed(user):
            await respond("You are not authorized to use this command.", ephemeral=True)
            return

        if self._thread_repo is None:
            await respond("Thread-level bindings are not enabled on this bot.", ephemeral=True)
            return

        assert thread_id is not None
        channel_id = channel.parent_id if isinstance(channel, discord.Thread) else thread_id

        # --- Remove binding ---
        if remove:
            deleted = await self._thread_repo.delete(thread_id)
            self.evict_thread_cache(thread_id)
            if deleted:
                await respond(
                    f"Removed thread repository binding for <#{thread_id}>.", ephemeral=True
                )
            else:
                await respond("No thread binding found.", ephemeral=True)
            return

        # --- Show current thread binding (no args) ---
        if repo is None:
            binding = await self._thread_repo.get(thread_id)
            if binding is None:
                await respond("No thread-level binding for this thread.", ephemeral=True)
            else:
                await respond(f"Thread <#{thread_id}> → `{binding['source_repo']}`", ephemeral=True)
            return

        # --- Access check: verify bot can read the thread's parent channel ---
        bot_channel = client.get_channel(channel_id)
        if bot_channel is None:
            try:
                await client.fetch_channel(channel_id)
            except discord.Forbidden:
                await respond(
                    f"⚠️ Bot がこのスレッドの親チャンネル (<#{channel_id}>) にアクセスできません。\n"
                    "先に Bot をそのチャンネルに追加してください。",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                pass  # 他のエラーは無視してbindを続行

        # --- Bind thread to repo ---
        try:
            repo = normalize_repo_url(validate_repo_url(repo))  # PR/issue/blob → repo root (#88)
        except ValueError as exc:
            await respond(f"⚠️ リポジトリ URL が不正です: {exc}", ephemeral=True)
            return
        await self._thread_repo.save(thread_id=thread_id, source_repo=repo, channel_id=channel_id)
        self.evict_thread_cache(thread_id)
        await respond(f"Bound thread <#{thread_id}> → `{repo}`", ephemeral=True)

    @app_commands.command(
        name="clord-thread-init",
        description="Bind this thread to a git repository (overrides channel binding)",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        repo="Git repository URL to clone for this thread's sessions",
        remove="Set True to remove the thread-level binding",
    )
    async def clord_thread_init(
        self,
        interaction: discord.Interaction,
        repo: str | None = None,
        remove: bool = False,
    ) -> None:
        """Bind / unbind a thread-level repo override."""
        await self._clord_thread_init_impl(
            thread_id=interaction.channel_id,
            channel=interaction.channel,
            client=interaction.client,
            user=interaction.user,
            repo=repo,
            remove=remove,
            respond=self._slash_respond(interaction),
        )

    @commands.command(name="clord-thread-init")
    async def clord_thread_init_text(self, ctx: commands.Context, arg: str | None = None) -> None:
        """Text/mention twin of /clord-thread-init — webhook-invokable for E2E (#209).

        Usage: ``!clord-thread-init`` (show) / ``!clord-thread-init <repo>`` (bind) /
        ``!clord-thread-init remove``. Gated by ``_is_allowed`` only.
        """
        repo: str | None = None
        remove = False
        if arg == "remove":
            remove = True
        elif arg:
            repo = arg
        await self._clord_thread_init_impl(
            thread_id=ctx.channel.id,
            channel=ctx.channel,
            client=ctx.bot,
            user=ctx.author,
            repo=repo,
            remove=remove,
            respond=self._ctx_respond(ctx),
        )
