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
    from ..database.repository import SessionRepository
    from ..database.thread_repo import ThreadRepository

from ..command_gate import is_message_authorized
from ..database.channel_repo import (
    derive_session_name,
    normalize_repo_url,
    validate_repo_url,
)
from ..session_dir import SessionDirManager
from ..session_resume import NOT_A_CLORD_THREAD_BINDING, classify, is_clord_thread
from ..thread_origin import inspect_origin
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
        session_repo: SessionRepository | None = None,
    ) -> None:
        self.bot = bot
        self._repo = repo
        self._thread_repo = thread_repo
        # #551: ``/clord-thread-init repo:`` binds only threads that are already
        # c-lord's, which is a question about the ``sessions`` table. Defaulting
        # to ``bot.session_repo`` keeps that true for consumers that never pass
        # it — a gate nobody wires up is a gate nobody has.
        self._session_repo = session_repo
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

    async def has_thread_binding(self, thread_id: int) -> bool:
        """Whether ``/clord-thread-init`` bound a repo to ``thread_id`` (#556).

        One of the three traces that outlive a swept ``sessions`` row — see
        :mod:`c_lord.thread_origin`. Never raises: the callers use it to decide
        whether to speak, and a DB hiccup is not an answer to that question.
        """
        if self._thread_repo is None:
            return False
        try:
            return await self._thread_repo.get(thread_id) is not None
        except Exception:
            logger.warning("has_thread_binding failed for thread=%s", thread_id, exc_info=True)
            return False

    async def is_clord_thread(self, thread_id: int, thread: object = None) -> bool:
        """Whether ``thread_id`` is one of c-lord's own threads — #551 AC2.

        Deliberately the *wide* test, not "does it have a session row": #554
        deletes that row after 30 days, and a c-lord thread that merely went
        quiet for a month is still c-lord's. Gating on the row alone would refuse
        to rebind exactly the threads most likely to need it. So a live row
        counts, and so does any trace :mod:`c_lord.thread_origin` finds — the same
        evidence #556 uses, one spelling shared rather than two that drift.

        With no session repository reachable at all the answer is **False**: an
        instance that cannot tell whose thread this is must not hand it over.
        """
        repo = self._session_repo or getattr(self.bot, "session_repo", None)
        if repo is None:
            logger.warning("/clord-thread-init: no session repository — refusing (#551)")
            return False
        if is_clord_thread(classify(await repo.get(thread_id))):
            return True
        base = None
        with contextlib.suppress(Exception):
            channel_id = getattr(thread, "parent_id", None)
            if channel_id is not None:
                manager = await self.resolve_manager(channel_id, thread_id=thread_id)
                base = getattr(manager, "base_dir", None)
        bot_user = getattr(self.bot, "user", None)
        binding = False
        if self._thread_repo is not None:
            with contextlib.suppress(Exception):
                binding = await self._thread_repo.get(thread_id) is not None
        return inspect_origin(
            thread_owner_id=getattr(thread, "owner_id", None),
            bot_user_id=getattr(bot_user, "id", None),
            session_dir_base=base,
            thread_id=thread_id,
            has_binding=binding,
        ).is_clords

    async def resolve_tmux_manager(
        self, channel_id: int, *, thread_id: int | None
    ) -> TmuxSessionManager | None:
        """Resolve a TmuxSessionManager for the given channel (and optional thread).

        Lookup order mirrors :meth:`resolve_manager`:
          1. Thread-level: in-memory thread cache → DB thread binding
          2. Channel-level: in-memory channel cache → DB channel binding
          3. None (caller should fall back to global bot.tmux_manager)

        ``thread_id`` is **required** (#600). It used to default to ``None``, and
        that default caused the same accident twice: #427, then #600, where two
        paths that send a menu answer back to the TUI resolved by parent channel
        and dropped every keystroke into the wrong tmux session. A caller that
        genuinely is not thread-scoped now has to write ``thread_id=None`` on
        purpose, which is reviewable; silence no longer compiles.

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

    async def divergent_session_name(self, channel_id: int, thread_id: int) -> str | None:
        """The thread's tmux session name, but only when it is not the channel's (#618).

        Returns ``None`` for the common case — a thread working in its own
        channel's repository — so its Discord name is left exactly as it was and
        no existing thread gets renamed. Returns the session name when the thread
        was bound elsewhere (``/clord-thread-init``, ``/clord repo:``), which is
        the case where reading ``W<N>`` and attaching to the channel's session
        lands on the wrong window (or on nothing at all).
        """
        from ..tmux import SESSION_NAME

        thread_mgr = await self.resolve_tmux_manager(channel_id, thread_id=thread_id)
        if thread_mgr is None:
            return None
        channel_mgr = await self.resolve_tmux_manager(channel_id, thread_id=None)
        channel_session = channel_mgr.session_name if channel_mgr else SESSION_NAME
        if thread_mgr.session_name == channel_session:
            return None
        return thread_mgr.session_name

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

    def _authorize(
        self, user: discord.Member | discord.User, message: discord.Message | None
    ) -> bool:
        """Allowlist for slash, the shared message-backed rule for text (#508).

        A text command can be driven by a webhook or a trusted bot, whose
        pseudo-user is in no allowlist — gating those on :meth:`_is_allowed` is
        what #507 fixed for ``!clord`` and what this fixes here.
        """
        if message is None:
            return self._is_allowed(user)
        return is_message_authorized(message, self._is_allowed)

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
        message: discord.Message | None = None,
    ) -> None:
        """Shared core for /clord-init and !clord-init (#209 follow-up).

        ``message`` is the invoking message for the text twin, ``None`` for
        slash.  When present, authorization goes through the shared
        message-backed rule so a configured ``DISCORD_OWNER_ID`` does not lock
        out the webhooks this command advertises itself as supporting (#508).
        """
        if not self._authorize(user, message):
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
            message=ctx.message,
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
        message: discord.Message | None = None,
    ) -> None:
        """Shared core for /clord-thread-init and !clord-thread-init (#209 follow-up).

        ``message`` carries the same meaning as in :meth:`_clord_init_impl` (#508).
        """
        if not self._authorize(user, message):
            await respond("You are not authorized to use this command.", ephemeral=True)
            return

        if self._thread_repo is None:
            await respond("Thread-level bindings are not enabled on this bot.", ephemeral=True)
            return

        assert thread_id is not None
        channel_id = channel.parent_id if isinstance(channel, discord.Thread) else thread_id

        # --- #551 AC2: only c-lord's own threads can be bound to a repo ---
        # Binding a human conversation thread was step one of the takeover this
        # closes: bind the thread, then /clord in it, and every message in what
        # had been a human conversation went to Claude. Only the *bind* path is
        # gated — ``remove`` and the no-arg display can never turn a thread into
        # a session, and blocking them would strand a stale binding on a thread
        # that lost its row with no way to clear it.
        if repo is not None and not remove and not await self.is_clord_thread(thread_id, channel):
            logger.info(
                "/clord-thread-init refused — thread=%s is not a c-lord thread (#551)", thread_id
            )
            await respond(NOT_A_CLORD_THREAD_BINDING, ephemeral=True)
            return

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
            message=ctx.message,
        )
