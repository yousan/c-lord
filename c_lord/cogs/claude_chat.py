"""Claude Code chat Cog.

Handles the core message flow:
1. User sends message in the configured channel
2. Bot creates a thread (or continues in existing thread)
3. Claude Code CLI is invoked with stream-json output
4. Status reactions and tool embeds are posted in real-time
5. Final response is posted to the thread
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import topic as topic_module
from ..claude.config import ClaudeConfig
from ..claude.tmux_runner import TmuxClaudeRunner
from ..concurrency import SessionRegistry
from ..coordination.service import CoordinationService
from ..database.ask_repo import PendingAskRepository
from ..database.lounge_repo import LoungeRepository
from ..database.repository import SessionRepository
from ..database.resume_repo import PendingResumeRepository
from ..database.settings_repo import SettingsRepository
from ..discord_ref import enrich_discord_references
from ..discord_ui.embeds import stopped_embed
from ..discord_ui.status import StatusManager
from ..discord_ui.thread_dashboard import ThreadState, ThreadStatusDashboard
from ..discord_ui.views import StopView
from ..thread_name import thread_lamp_enabled
from ..thread_settings import resolve_auto_archive_duration
from ..utils.logger import log_ctx
from ._run_helper import run_claude_with_config
from .run_config import RunConfig

if TYPE_CHECKING:
    from ..bot import ClaudeDiscordBot
    from ..session_dir import SessionDirManager
    from ..tmux import TmuxSessionManager

logger = logging.getLogger(__name__)

# Posts a reply the way the caller needs (interaction response vs ctx.send),
# letting /stop, /clear and their !text twins share one implementation (#209).
_Responder = Callable[..., Awaitable[None]]

# Attachment filtering constants
_ALLOWED_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
)
_IMAGE_MIME_PREFIXES = ("image/",)
_MAX_ATTACHMENT_BYTES = 50_000  # 50 KB per file
_MAX_TOTAL_BYTES = 100_000  # 100 KB across all text attachments
_MAX_ATTACHMENTS = 5

# Discord CDN URLs are signed and valid for ~24 hours.
_ATTACHMENT_URL_NOTE = (
    "(This is a signed CDN URL valid for ~24 hours. Use the WebFetch tool or curl to retrieve it.)"
)


class ClaudeChatCog(commands.Cog):
    """Cog that handles Claude Code conversations via Discord threads."""

    def __init__(
        self,
        bot: ClaudeDiscordBot,
        repo: SessionRepository,
        runner: ClaudeConfig,
        max_concurrent: int = 3,
        allowed_user_ids: set[int] | None = None,
        registry: SessionRegistry | None = None,
        dashboard: ThreadStatusDashboard | None = None,
        coordination: CoordinationService | None = None,
        ask_repo: PendingAskRepository | None = None,
        lounge_repo: LoungeRepository | None = None,
        resume_repo: PendingResumeRepository | None = None,
        settings_repo: SettingsRepository | None = None,
        allowed_role_name: str | None = None,
        thread_lamp: bool | None = None,
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.runner = runner
        self._max_concurrent = max_concurrent
        # Thread-name status lamp (🟢🟡🔴⚪). Off by default (#329) because
        # repainting the name on every state change saturates Discord's
        # thread-rename rate-limit. Opt in via thread_lamp=True or
        # CLORD_THREAD_LAMP=1. Resolved once here so the env is read at startup.
        self._thread_lamp = thread_lamp_enabled(thread_lamp)
        self._allowed_user_ids = allowed_user_ids
        self._allowed_role_name = allowed_role_name
        self._registry = registry or getattr(bot, "session_registry", None)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_runners: dict[int, TmuxClaudeRunner] = {}
        # Per-thread lock to prevent duplicate _run_claude invocations.
        # Without this, two messages arriving in quick succession could
        # both enter _run_claude before the first registers in _active_runners.
        self._thread_locks: dict[int, asyncio.Lock] = {}
        # Tracks the asyncio.Task running _run_claude for each thread.
        # Used by _handle_thread_reply to wait for an interrupted session
        # to fully clean up before starting the replacement session.
        self._active_tasks: dict[int, asyncio.Task] = {}
        # Dashboard may be None until bot is ready; resolved lazily in _get_dashboard()
        self._dashboard = dashboard
        # Coordination service resolved lazily from bot if not supplied directly
        self._coordination = coordination
        # For AskUserQuestion persistence across restarts
        self._ask_repo = ask_repo or getattr(bot, "ask_repo", None)
        # AI Lounge repo (optional — lounge disabled when None)
        self._lounge_repo = lounge_repo or getattr(bot, "lounge_repo", None)
        # Pending resume repo (optional — startup resume disabled when None)
        self._resume_repo = resume_repo or getattr(bot, "resume_repo", None)
        # Settings repo for dynamic model lookup (optional — falls back to runner.model)
        self._settings_repo = settings_repo or getattr(bot, "settings_repo", None)
        # Issue #95: pending topic writes for sessions whose row hasn't been
        # saved yet (event_processor saves the row only after Claude emits
        # its first session_id).  Drained by _apply_thread_naming on the
        # next call once the row exists.
        self._pending_topic: dict[int, tuple[str, str]] = {}
        self._pending_tmux_window_id: dict[int, str] = {}

    def _is_allowed(self, member: discord.Member | discord.User) -> bool:
        """Check if a member/user is authorized to use the bot.

        OR logic: allowed_user_ids match OR allowed_role_name match.
        When neither is configured, everyone is allowed.
        """
        if self._allowed_user_ids is not None and member.id in self._allowed_user_ids:
            return True
        if self._allowed_role_name is not None:
            if isinstance(member, discord.Member):
                return any(r.name == self._allowed_role_name for r in member.roles)
            return False  # DM — no role info
        return self._allowed_user_ids is None

    def is_processing(self, thread_id: int) -> bool:
        """True while a Claude turn is actively running for ``thread_id``.

        This is the lamp's source of truth for 🟢 ``running`` (#236): the entry
        registers in ``_active_tasks`` the moment a turn starts and is popped in
        the ``finally`` block when it finishes. Consumed by
        :class:`ThreadStateSyncLoop` so the poll never rolls an in-flight thread
        back to 🟡 ``waiting`` during a brief no-spinner window.
        """
        return thread_id in self._active_tasks

    @property
    def active_session_count(self) -> int:
        """Number of Claude sessions currently running in this cog."""
        return len(self._active_runners)

    @property
    def active_count(self) -> int:
        """Alias for active_session_count (satisfies DrainAware protocol)."""
        return self.active_session_count

    def _get_dashboard(self) -> ThreadStatusDashboard | None:
        """Return the dashboard, resolving it from the bot if not yet set."""
        if self._dashboard is None:
            self._dashboard = getattr(self.bot, "thread_dashboard", None)
        return self._dashboard

    async def _resolve_session_dir_manager(
        self, channel_id: int | None, thread_id: int | None = None
    ) -> SessionDirManager | None:
        """Resolve a SessionDirManager for the given channel (and optional thread).

        Lookup order: thread binding → channel binding → None.
        Does NOT fall back to a global bot.session_dir_manager —
        channels without a ``/clord-init`` or ``/clord-thread-init`` binding get no manager.
        """
        if channel_id is None:
            return None
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_manager(channel_id, thread_id=thread_id)
        return None

    async def _resolve_tmux_manager(self, channel_id: int | None) -> TmuxSessionManager | None:
        """Resolve a TmuxSessionManager for the given channel.

        Returns the per-channel manager from ChannelRepoCog if a binding exists,
        or None. Does NOT fall back to a global bot.tmux_manager —
        channels without a ``/clord-init`` binding get no manager.
        """
        if channel_id is None:
            return None
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_tmux_manager(channel_id)
        return None

    async def _get_current_model(self) -> str | None:
        """Return the model override from settings_repo, or None to use runner default.

        When /model set has been used to change the global model, this returns
        the stored value. Returns None if no override is set or settings_repo
        is unavailable.
        """
        if self._settings_repo is None:
            return None
        from .session_manage import SETTING_CLAUDE_MODEL

        return await self._settings_repo.get(SETTING_CLAUDE_MODEL)

    def _get_coordination(self) -> CoordinationService:
        """Return the coordination service (zero-config: auto-creates from env if needed).

        Priority:
        1. Explicitly supplied via constructor or bot.coordination attribute
        2. Auto-created from COORDINATION_CHANNEL_ID env var (no consumer wiring needed)
        3. No-op service when env var is unset
        """
        if self._coordination is None:
            existing = getattr(self.bot, "coordination", None)
            if existing is not None:
                self._coordination = existing
            else:
                channel_id_str = os.getenv("COORDINATION_CHANNEL_ID", "")
                channel_id = int(channel_id_str) if channel_id_str.isdigit() else None
                self._coordination = CoordinationService(self.bot, channel_id)
        return self._coordination

    async def _apply_thread_naming(
        self,
        *,
        thread: discord.Thread,
        tmux_manager: TmuxSessionManager,
        first_message: str,
    ) -> None:
        """Apply the Issue #95 naming scheme to ``thread``.

        - Generates and persists ``topic`` on first use (unless the
          thread is ``auto_topic_locked`` from a previous manual rename).
        - Persists the tmux ``window_id`` (immutable) on the row.
        - Renames the Discord thread to
          ``<status_emoji> W<work_number> │ <topic>`` capped at 30 chars,
          but only if the current name differs (minimises API calls).

        All errors are swallowed by the caller; this helper raises only
        on truly unexpected programmer mistakes.
        """
        from ..thread_name import build_name, parse_topic_from_name

        record = await self.repo.get(thread.id)
        topic = record.topic if record else None
        locked = bool(record.auto_topic_locked) if record else False
        state = (record.state if record else None) or "alive"

        # Drain any topic / window-id pending from a previous call where
        # the session row did not yet exist.
        if record is not None:
            pending = self._pending_topic.pop(thread.id, None)
            if pending and not record.topic and not locked:
                await self.repo.set_topic(thread.id, pending[0], source=pending[1])
                topic = pending[0]
            pending_win = self._pending_tmux_window_id.pop(thread.id, None)
            if pending_win and record.tmux_window_id != pending_win:
                await self.repo.set_tmux_window_id(thread.id, pending_win)

        # Resolve tmux window-id / work-number (the stable work{N} number).
        info = await asyncio.to_thread(tmux_manager.get_window_info, thread.id)
        if info is not None:
            window_id, window_number = info
            if record is not None and record.tmux_window_id != window_id:
                await self.repo.set_tmux_window_id(thread.id, window_id)
            elif record is None:
                self._pending_tmux_window_id[thread.id] = window_id
        else:
            window_number = None

        # Re-summarize title on subsequent messages (#121).
        # Only runs when a topic already exists and the thread is not manually
        # renamed (locked).  Returns None when the LLM deems the current topic
        # still valid (verbatim match) — so no rename API call is triggered.
        if topic and not locked:
            with contextlib.suppress(Exception):
                new_topic = await topic_module.maybe_retitle(first_message or "", topic)
                if new_topic is not None:
                    if record is not None:
                        await self.repo.set_topic(thread.id, new_topic, source="llm_retitle")
                    else:
                        self._pending_topic[thread.id] = (new_topic, "llm_retitle")
                    topic = new_topic

        # Derive topic if missing and not locked (first message).
        if not topic and not locked:
            try:
                topic, source = await topic_module.generate_topic(first_message or "")
            except Exception:
                logger.warning("topic generation failed", exc_info=True)
                topic, source = topic_module.heuristic_topic(first_message or ""), "heuristic"
            if record is not None:
                await self.repo.set_topic(thread.id, topic, source=source)
            else:
                # Save row doesn't exist yet — stash and persist on next call
                # (event_processor saves the row after Claude's first event).
                self._pending_topic[thread.id] = (topic, source)

        if not topic:
            # Last-resort fallback — should never happen since heuristic
            # is non-empty, but guards against odd states (e.g. row was
            # just deleted concurrently).
            topic = parse_topic_from_name(thread.name) or "新しいスレッド"

        # lamp=False (default, #329) keeps the topic but drops the leading
        # status emoji, so the name no longer changes on state transitions and
        # the only rename is the one-off topic naming.
        new_name = build_name(topic, state, window_number, lamp=self._thread_lamp)
        if (thread.name or "") == new_name:
            return
        with contextlib.suppress(discord.HTTPException, TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(thread.edit(name=new_name), timeout=5.0)
            logger.info(
                "%s lamp → %s (event-driven) %r",
                log_ctx(thread_id=thread.id),
                state,
                new_name,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages."""
        # Ignore bot messages (but allow webhook messages and trusted bots through).
        # CLORD_TRUSTED_BOT_IDS env: comma-separated bot user IDs to treat like humans.
        if message.author.bot and not message.webhook_id:
            trusted_raw = os.getenv("CLORD_TRUSTED_BOT_IDS", "")
            trusted_ids = {int(x.strip()) for x in trusted_raw.split(",") if x.strip().isdigit()}
            if message.author.id not in trusted_ids:
                return

        # Authorization check — user must match allowed_user_ids or have
        # the allowed role.  When neither is configured, everyone is allowed.
        if not self._is_allowed(message.author):
            return

        # Channel direct messages are ignored — thread creation is limited to
        # slash commands (/skill) and API (spawn_session) to prevent accidental sessions.
        if message.channel.id == self.bot.channel_id:
            return

        # System messages (thread rename, pin, etc.) must not reach Claude
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        # Text commands (e.g. !attach) are handled by process_commands — skip here
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        # Handle threads: only respond if session exists in DB (opt-in)
        if isinstance(message.channel, discord.Thread) and (
            await self.repo.get(message.channel.id) is not None
        ):
            await self._handle_thread_reply(message)

    async def _clord_impl(
        self,
        *,
        channel: object,
        channel_id_fallback: int | None,
        user: discord.Member | discord.User,
        prompt: str,
        respond: _Responder,
        ack: _Responder,
    ) -> None:
        """Shared core for /clord and !clord (#209 follow-up).

        In a thread → continue the session; in a channel → create a new thread
        via ``spawn_session``. ``ack`` defers (slash) or is a no-op (text); after
        it, all replies go through ``respond``'s post-ack path.
        """
        # Authorization check
        if not self._is_allowed(user):
            await respond("You are not authorized to use this command.", ephemeral=True)
            return

        # Unbound channel check: verify /clord-init or /clord-thread-init binding
        if isinstance(channel, discord.Thread):
            parent_channel_id = channel.parent_id or channel.id
            sdm = await self._resolve_session_dir_manager(parent_channel_id, thread_id=channel.id)
            tmux = await self._resolve_tmux_manager(parent_channel_id)
            if sdm is None and tmux is None:
                await respond(
                    "⚠️ このスレッドにはリポジトリが紐づけられていません。\n"
                    "先に `/clord-thread-init repo:<URL>` または"
                    " `/clord-init repo:<URL>` で設定してください。",
                    ephemeral=True,
                )
                return
        else:
            channel_id = (
                channel.id
                if isinstance(channel, discord.abc.GuildChannel)
                else (channel_id_fallback)
            )
            sdm = await self._resolve_session_dir_manager(channel_id)
            tmux = await self._resolve_tmux_manager(channel_id)
            if sdm is None and tmux is None:
                await respond(
                    "⚠️ このチャンネルにはリポジトリが紐づけられていません。\n"
                    "先に `/clord-init repo:<URL> branch:<branch>` で設定してください。",
                    ephemeral=True,
                )
                return

        await ack()

        if isinstance(channel, discord.Thread):
            # Continue in existing thread
            record = await self.repo.get(channel.id)
            session_id = (record.session_id or None) if record else None
            seed_message = await channel.send(prompt)
            await self._run_claude(seed_message, channel, prompt=prompt, session_id=session_id)
            await respond("Session completed.", silent=True)
        else:
            # Create a new thread via spawn_session (text channels only)
            if not isinstance(channel, discord.TextChannel):
                await respond("This command must be used in a text channel.", ephemeral=True)
                return
            thread = await self.spawn_session(channel, prompt)
            await respond(f"Session started → {thread.mention}")

    @app_commands.command(name="clord", description="Start a new Claude Code session")
    @app_commands.describe(prompt="Message to send to Claude Code")
    async def start_session(self, interaction: discord.Interaction, prompt: str) -> None:
        """Start a new Claude Code session or continue in an existing thread."""
        state = {"acked": False}

        async def ack(*_args: object, **_kwargs: object) -> None:
            state["acked"] = True
            await interaction.response.defer()

        async def respond(
            content: str | None = None, *, ephemeral: bool = False, silent: bool = False
        ) -> None:
            if state["acked"]:
                await interaction.followup.send(content or "", ephemeral=ephemeral, silent=silent)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)

        await self._clord_impl(
            channel=interaction.channel,
            channel_id_fallback=interaction.channel_id,
            user=interaction.user,
            prompt=prompt,
            respond=respond,
            ack=ack,
        )

    @commands.command(name="clord")
    async def clord_text(self, ctx: commands.Context, *, prompt: str | None = None) -> None:
        """Text/mention twin of /clord — invokable from webhooks for E2E (#209)."""
        if not prompt:
            await ctx.send("Usage: `!clord <prompt>`")
            return

        async def ack(*_args: object, **_kwargs: object) -> None:
            return None

        async def respond(
            content: str | None = None, *, ephemeral: bool = False, silent: bool = False
        ) -> None:
            await ctx.send(content or "", silent=silent)

        await self._clord_impl(
            channel=ctx.channel,
            channel_id_fallback=ctx.channel.id if ctx.channel else None,
            user=ctx.author,
            prompt=prompt,
            respond=respond,
            ack=ack,
        )

    async def _stop_impl(self, channel: object, respond: _Responder) -> None:
        """Shared core for /stop and !stop (#209).

        Interrupts the active runner without clearing the session DB so the
        user can resume by sending a new message.  ``respond`` posts the reply
        the way the caller needs (interaction response vs ctx.send).
        """
        if not isinstance(channel, discord.Thread):
            await respond("This command can only be used in a Claude chat thread.", ephemeral=True)
            return

        runner = self._active_runners.get(channel.id)
        if not runner:
            await respond("No active session is running in this thread.", ephemeral=True)
            return

        await runner.interrupt()
        # _active_runners cleanup is handled by _run_claude's finally block.
        # We intentionally do NOT delete from the session DB so the user can resume.
        await respond(embed=stopped_embed())

    @app_commands.command(name="stop", description="Stop the active session (session is preserved)")
    async def stop_session(self, interaction: discord.Interaction) -> None:
        """Stop the active Claude run without clearing the session.

        Unlike /clear, this preserves the session ID so the user can
        resume by sending a new message.
        """

        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            ephemeral: bool = False,
        ) -> None:
            if embed is not None:
                await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)

        await self._stop_impl(interaction.channel, respond)

    @commands.command(name="stop")
    async def stop_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /stop — invokable from webhooks for E2E (#209)."""

        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            ephemeral: bool = False,
        ) -> None:
            if embed is not None:
                await ctx.send(embed=embed)
            else:
                await ctx.send(content or "")

        await self._stop_impl(ctx.channel, respond)

    @app_commands.command(
        name="clord-attach",
        description="Attach this thread to an existing tmux window",
    )
    @app_commands.describe(window="tmux window name (e.g. work1)")
    async def attach_window(self, interaction: discord.Interaction, window: str) -> None:
        """Remap an existing tmux window to the current thread."""
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "This command can only be used in a thread.", ephemeral=True
            )
            return

        if not self._is_allowed(interaction.user):
            await interaction.response.send_message(
                "You are not authorized to use this command.", ephemeral=True
            )
            return

        parent_id = getattr(interaction.channel, "parent_id", None) or interaction.channel.id
        tmux_manager = await self._resolve_tmux_manager(parent_id)
        if tmux_manager is None:
            await interaction.response.send_message(
                "tmux is not configured for this bot.", ephemeral=True
            )
            return

        ok = tmux_manager.remap_window(interaction.channel.id, window)
        if ok:
            await self.repo.save(
                interaction.channel.id, session_id=f"tmux-{interaction.channel.id}"
            )
            await interaction.response.send_message(f"Attached this thread to `{window}`.")
        else:
            await interaction.response.send_message(
                f"Window `{window}` not found in tmux.", ephemeral=True
            )

    @commands.command(name="attach")
    async def attach_text(self, ctx: commands.Context, window: str) -> None:
        """Text command: attach this thread to a tmux window.

        E2E-testable alternative to the /clord-attach slash command.
        Usage: !attach work13
        """
        if not isinstance(ctx.channel, discord.Thread):
            await ctx.send("This command can only be used in a thread.")
            return

        if not self._is_allowed(ctx.author):
            await ctx.send("You are not authorized to use this command.")
            return

        parent_id = getattr(ctx.channel, "parent_id", None) or ctx.channel.id
        tmux_manager = await self._resolve_tmux_manager(parent_id)
        if tmux_manager is None:
            await ctx.send("tmux is not configured for this bot.")
            return

        ok = tmux_manager.remap_window(ctx.channel.id, window)
        if ok:
            await self.repo.save(ctx.channel.id, session_id=f"tmux-{ctx.channel.id}")
            await ctx.send(f"Attached this thread to `{window}`.")
        else:
            await ctx.send(f"Window `{window}` not found in tmux.")

    async def _clear_impl(self, channel: object, respond: _Responder) -> None:
        """Shared core for /clear and !clear (#209).

        Kills the active runner and tmux window, then resets the session row so
        the next message starts fresh.
        """
        if not isinstance(channel, discord.Thread):
            await respond("This command can only be used in a Claude chat thread.", ephemeral=True)
            return

        thread_id = channel.id

        # Kill active runner if any
        runner = self._active_runners.get(thread_id)
        if runner:
            await runner.kill()
            del self._active_runners[thread_id]

        # Kill the tmux window unconditionally — even for idle sessions where the
        # runner has already been removed from _active_runners (issue #123).
        # This ensures `is_claude_running` returns False next time, preventing
        # old context from being resumed via send_input.
        parent_id = getattr(channel, "parent_id", None) or thread_id
        tmux_manager = await self._resolve_tmux_manager(parent_id)
        if tmux_manager is not None:
            await asyncio.to_thread(tmux_manager.kill_session, thread_id)

        reset = await self.repo.reset(thread_id)
        if reset:
            await respond("\U0001f504 Session cleared. Next message will start a fresh session.")
        else:
            await respond("No active session found for this thread.", ephemeral=True)

    @app_commands.command(name="clear", description="Reset the Claude Code session for this thread")
    async def clear_session(self, interaction: discord.Interaction) -> None:
        """Reset the session for the current thread."""

        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            ephemeral: bool = False,
        ) -> None:
            await interaction.response.send_message(content, ephemeral=ephemeral)

        await self._clear_impl(interaction.channel, respond)

    @commands.command(name="clear")
    async def clear_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /clear — invokable from webhooks for E2E (#209)."""

        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            ephemeral: bool = False,
        ) -> None:
            await ctx.send(content or "")

        await self._clear_impl(ctx.channel, respond)

    async def _compact_impl(
        self, channel: object, respond: _Responder, *, instructions: str = ""
    ) -> None:
        """Shared core for /compact and !compact (#278).

        Fires the Claude Code TUI's built-in ``/compact`` slash command in the
        thread's tmux window to compress (summarize) the session context,
        freeing up the context window without losing history (unlike /clear).

        Sent via ``send_literal`` (NOT ``send_input``): under
        ``CLORD_BRIDGE_MODE=jsonl`` ``send_input`` prepends a zero-width-space
        marker, so the line would no longer start with ``/`` and the TUI would
        not treat it as a slash command (see docs/COMMANDS.md). This mirrors the
        existing ``/context`` probe in ``tmux_runner.py``. Enter is sent
        separately via ``send_keys`` since ``send_literal`` does not submit.
        """
        if not isinstance(channel, discord.Thread):
            await respond("This command can only be used in a Claude chat thread.", ephemeral=True)
            return

        thread_id = channel.id
        parent_id = getattr(channel, "parent_id", None) or thread_id
        tmux_manager = await self._resolve_tmux_manager(parent_id)
        if tmux_manager is None:
            await respond("tmux is not configured for this thread.", ephemeral=True)
            return

        if not await asyncio.to_thread(tmux_manager.is_claude_running, thread_id):
            await respond("No running Claude session in this thread to compact.", ephemeral=True)
            return

        instructions = instructions.strip()
        command = f"/compact {instructions}" if instructions else "/compact"

        # send_literal (not send_input): no ZWSP prefix so the leading "/" is
        # preserved and the TUI recognises it as a slash command.
        ok = await asyncio.to_thread(tmux_manager.send_literal, thread_id, command)
        if not ok:
            await respond("Failed to send /compact to the session.", ephemeral=True)
            return
        await asyncio.to_thread(tmux_manager.send_keys, thread_id, "Enter")

        await respond("\U0001f5dc️ Compacting context… (`/compact` sent)")

    @app_commands.command(
        name="compact",
        description="Compact (summarize) this thread's Claude context to free the window",
    )
    @app_commands.describe(
        instructions="Optional focus for the summary (e.g. 'keep open tasks and decisions')"
    )
    async def compact_session(
        self, interaction: discord.Interaction, instructions: str = ""
    ) -> None:
        """Trigger the TUI ``/compact`` for the current thread's session."""

        async def respond(content: str | None = None, *, ephemeral: bool = False) -> None:
            await interaction.response.send_message(content, ephemeral=ephemeral)

        await self._compact_impl(interaction.channel, respond, instructions=instructions)

    @commands.command(name="compact")
    async def compact_text(self, ctx: commands.Context, *, instructions: str = "") -> None:
        """Text/mention twin of /compact — invokable from webhooks for E2E (#278)."""

        async def respond(content: str | None = None, *, ephemeral: bool = False) -> None:
            await ctx.send(content or "")

        await self._compact_impl(ctx.channel, respond, instructions=instructions)

    async def _handle_new_conversation(self, message: discord.Message) -> None:
        """Create a new thread and start a Claude Code session."""
        thread_name = message.content[:100] if message.content else "Claude Chat"
        archive_minutes = await resolve_auto_archive_duration(self._settings_repo)
        thread = await message.create_thread(
            name=thread_name, auto_archive_duration=archive_minutes
        )
        prompt, image_paths = await self._build_prompt_and_images(message)
        prompt = await enrich_discord_references(prompt, message, self.bot)
        await self._run_claude(message, thread, prompt, session_id=None, image_paths=image_paths)

    async def spawn_session(
        self,
        channel: discord.TextChannel,
        prompt: str,
        thread_name: str | None = None,
        session_id: str | None = None,
    ) -> discord.Thread:
        """Create a new thread and start a Claude Code session without a user message.

        This is the API-initiated equivalent of ``_handle_new_conversation``.
        It bypasses the ``on_message`` bot-author guard, enabling programmatic
        spawning of Claude sessions (e.g. from ``POST /api/spawn``).

        A seed message is posted inside the new thread so that ``StatusManager``
        has a concrete ``discord.Message`` to attach reaction-emoji status to.

        Args:
            channel: The parent text channel in which to create the thread.
            prompt: The instruction to send to Claude Code.
            thread_name: Optional thread title; defaults to the first 100 chars
                of *prompt*.
            session_id: Optional Claude session ID to resume via ``--resume``.
                        When supplied the new Claude process continues the
                        previous conversation rather than starting fresh.

        Returns:
            The newly created :class:`discord.Thread`.
        """
        name = (thread_name or prompt)[:100]
        archive_minutes = await resolve_auto_archive_duration(self._settings_repo)
        thread = await channel.create_thread(
            name=name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=archive_minutes,
        )
        # Post the prompt so StatusManager has a Message to add reactions to.
        seed_message = await thread.send(prompt)
        # Run Claude in the background so /api/spawn returns immediately.
        # The caller gets the thread reference without waiting for Claude to finish.
        asyncio.create_task(self._run_claude(seed_message, thread, prompt, session_id=session_id))
        return thread

    async def cog_unload(self) -> None:
        """Mark all mid-run Claude sessions for auto-resume on the next bot startup.

        Called by discord.py whenever the cog is removed — including during a
        clean shutdown triggered by ``systemctl restart/stop``, ``bot.close()``,
        or any other SIGTERM-based shutdown.  This ensures that sessions which
        were actively running when the bot was killed will be automatically
        resumed (with a "bot restarted" prompt) as soon as the bot comes back.

        Idle sessions (where Claude has already replied and is waiting for the
        next human message) are NOT in ``_active_runners`` and therefore are not
        marked — they resume naturally via message-triggered resume when the user
        sends their next message.

        No-op when ``_resume_repo`` is not configured.
        """
        if not self._active_runners or self._resume_repo is None:
            return

        logger.info(
            "Shutdown detected: marking %d active session(s) for restart-resume",
            len(self._active_runners),
        )
        for thread_id in list(self._active_runners):
            try:
                session_id: str | None = None
                record = await self.repo.get(thread_id)
                if record is not None:
                    session_id = record.session_id

                await self._resume_repo.mark(
                    thread_id,
                    session_id=session_id,
                    reason="bot_shutdown",
                    resume_prompt=(
                        "ボットが再起動しました。"
                        "前の作業の続きを確認し、必要な残作業があれば完了してください。"
                    ),
                )
                logger.info(
                    "Marked thread %d for restart-resume (session=%s)", thread_id, session_id
                )
            except Exception:
                logger.warning(
                    "Failed to mark thread %d for restart-resume", thread_id, exc_info=True
                )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Resume any Claude sessions that marked themselves for restart-resume.

        Called each time the bot connects to Discord (including reconnects).
        Only pending resumes within the TTL window (default 5 minutes) are
        processed; older entries are silently discarded by the repository.

        Safety guarantees:
        - Each row is **deleted before** spawning Claude so that even a
          crash during spawn cannot cause a double-resume.
        - The TTL prevents stale markers from triggering after a long
          downtime or accidental second restart.
        - A resume failure (e.g. channel not found) is logged and skipped
          gracefully — it never prevents the bot from becoming ready.
        """
        if self._resume_repo is None:
            return

        pending = await self._resume_repo.get_pending()
        if not pending:
            return

        logger.info("Found %d pending session resume(s) on startup", len(pending))

        for entry in pending:
            # Delete FIRST — prevents double-resume even if spawn fails
            await self._resume_repo.delete(entry.id)

            thread_id = entry.thread_id
            try:
                raw = self.bot.get_channel(thread_id)
                if raw is None:
                    raw = await self.bot.fetch_channel(thread_id)
            except Exception:
                logger.warning(
                    "Pending resume: thread %d not found, skipping", thread_id, exc_info=True
                )
                continue

            if not isinstance(raw, discord.Thread):
                logger.warning("Pending resume: channel %d is not a Thread, skipping", thread_id)
                continue

            thread = raw
            parent = thread.parent
            if not isinstance(parent, discord.TextChannel):
                logger.warning(
                    "Pending resume: thread %d has no TextChannel parent, skipping", thread_id
                )
                continue

            resume_prompt = entry.resume_prompt or (
                "ボットが再起動から復帰しました。"
                "前の作業の続きを確認し、必要な残作業を完了してください。"
            )

            logger.info(
                "Resuming session in thread %d (session_id=%s, reason=%s)",
                thread_id,
                entry.session_id,
                entry.reason,
            )
            try:
                # Post directly into the existing thread — no new thread needed
                seed_message = await thread.send(
                    f"🔄 **Bot が再起動から復帰しました。**\n{resume_prompt}"
                )
                asyncio.create_task(
                    self._run_claude(
                        seed_message,
                        thread,
                        resume_prompt,
                        session_id=entry.session_id,
                        try_continue=True,
                    )
                )
            except Exception:
                logger.error("Failed to resume session in thread %d", thread_id, exc_info=True)

    async def _handle_thread_reply(self, message: discord.Message) -> None:
        """Continue a Claude Code session in an existing thread.

        If Claude is already running in this thread, sends SIGINT to the active
        session (graceful interrupt, like pressing Escape) and waits for it to
        finish cleaning up before starting the new session.  This prevents two
        Claude processes from running in parallel in the same thread.

        A per-thread asyncio.Lock serializes concurrent calls so that a second
        message arriving before the first registers in ``_active_runners``
        cannot spawn a duplicate ``_run_claude``.
        """
        thread = message.channel
        assert isinstance(thread, discord.Thread)

        lock = self._thread_locks.setdefault(thread.id, asyncio.Lock())
        async with lock:
            record = await self.repo.get(thread.id)
            session_id = (record.session_id or None) if record else None
            prompt, image_paths = await self._build_prompt_and_images(message)
            prompt = await enrich_discord_references(prompt, message, self.bot)

            # Interrupt any active session in this thread before starting a new one.
            existing_runner = self._active_runners.get(thread.id)
            existing_task = self._active_tasks.get(thread.id)
            if existing_runner is not None:
                await thread.send("-# ⚡ Interrupted. Starting with new instruction...")
                await existing_runner.interrupt(silent=True)
                # Wait for the interrupted _run_claude to finish its finally block
                # (which releases the semaphore and removes entries from dicts).
                if existing_task is not None and not existing_task.done():
                    with contextlib.suppress(Exception):
                        await existing_task

            # #270: when the tmux pane has died (bot restart / kill -9 / tmux-server
            # death) but a prior session's transcript is still on disk, resume it via
            # --continue instead of starting fresh and discarding the history.
            # Conditions:
            #   - existing_runner is None: an interrupted-but-live session stays alive
            #     in tmux and should just receive send_input, not --continue.
            #   - session_id is not None: a /clear'd thread has its session_id reset,
            #     so it stays fresh — preserving the #123 Part 1 invariant.
            #   - the tmux pane is actually dead (is_claude_running is False).
            # This extends the --continue fallback (previously only on the
            # restart-resume path, #123 Part 2) to the ordinary reply path.
            try_continue = False
            if session_id is not None and existing_runner is None:
                parent_channel_id = getattr(thread, "parent_id", None) or thread.id
                tmux_manager = await self._resolve_tmux_manager(parent_channel_id)
                if tmux_manager is not None:
                    pane_alive = await asyncio.to_thread(tmux_manager.is_claude_running, thread.id)
                    try_continue = not pane_alive

            await self._run_claude(
                message,
                thread,
                prompt,
                session_id=session_id,
                image_paths=image_paths,
                try_continue=try_continue,
            )

    async def _build_prompt(self, message: discord.Message) -> str:
        """Build the prompt string (text only). Use _build_prompt_and_images for full processing."""
        prompt, _ = await self._build_prompt_and_images(message)
        return prompt

    async def _build_prompt_and_images(self, message: discord.Message) -> tuple[str, list[str]]:
        """Build the prompt string from message content and attachments.

        Text attachments (text/*, application/json, application/xml) are appended
        inline to the prompt.

        Image and other binary attachments are embedded as CDN URLs with a note
        explaining that Claude can fetch them via WebFetch or curl.  Discord CDN
        URLs are signed and valid for ~24 hours — no download or tempfile needed.

        Returns:
            (prompt_text, []) — image_paths is always empty (URL embedding, not
            local download).  The second element is kept for API compatibility.
        """
        prompt = message.content or ""
        if not message.attachments:
            return prompt, []

        total_bytes = 0
        sections: list[str] = []

        for attachment in message.attachments[:_MAX_ATTACHMENTS]:
            content_type = attachment.content_type or ""

            # ---- Image and binary attachments → embed CDN URL in prompt ----
            if content_type.startswith(_IMAGE_MIME_PREFIXES) or not content_type.startswith(
                _ALLOWED_MIME_PREFIXES
            ):
                sections.append(
                    f"\n\n--- Attached file: {attachment.filename} ---\n"
                    f"URL: {attachment.url}\n"
                    f"{_ATTACHMENT_URL_NOTE}"
                )
                logger.debug(
                    "Embedded attachment URL for %s (%s)", attachment.filename, content_type
                )
                continue

            # ---- Text attachments → inline content in prompt ----
            if attachment.size > _MAX_ATTACHMENT_BYTES:
                logger.debug(
                    "Skipping attachment %s: too large (%d bytes)",
                    attachment.filename,
                    attachment.size,
                )
                continue
            total_bytes += attachment.size
            if total_bytes > _MAX_TOTAL_BYTES:
                logger.debug("Stopping attachment processing: total size exceeded")
                break
            try:
                data = await attachment.read()
                text = data.decode("utf-8", errors="replace")
                sections.append(f"\n\n--- Attached file: {attachment.filename} ---\n{text}")
            except Exception:
                logger.debug("Failed to read attachment %s", attachment.filename, exc_info=True)
                continue

        return prompt + "".join(sections), []

    async def _run_claude(
        self,
        user_message: discord.Message,
        thread: discord.Thread,
        prompt: str,
        session_id: str | None,
        image_paths: list[str] | None = None,
        try_continue: bool = False,
    ) -> None:
        """Execute Claude Code CLI and stream results to the thread."""
        # Unbound channel check: verify /clord-init or /clord-thread-init binding
        parent_channel_id = getattr(thread, "parent_id", None) or thread.id
        session_dir_manager = await self._resolve_session_dir_manager(
            parent_channel_id, thread_id=thread.id
        )
        tmux_manager = await self._resolve_tmux_manager(parent_channel_id)

        if session_dir_manager is None and tmux_manager is None:
            await thread.send(
                "⚠️ このチャンネルにはリポジトリが紐づけられていません。\n"
                "先に `/clord-init repo:<URL> branch:<branch>` で設定してください。"
            )
            return

        if self._semaphore.locked():
            await thread.send(
                f"\u23f3 Waiting for a free session slot... "
                f"({self._max_concurrent} max sessions running)"
            )

        async with self._semaphore:
            dashboard = self._get_dashboard()
            coordination = self._get_coordination()
            description = prompt[:100].replace("\n", " ")

            # Register the current asyncio Task so _handle_thread_reply can
            # await it after sending SIGINT to the runner.
            current_task = asyncio.current_task()
            if current_task is not None:
                self._active_tasks[thread.id] = current_task

            # #246: the per-turn 🟢/🟡 lamp now lives on the trigger-message
            # reaction (StatusManager below), NOT on the thread name. We no
            # longer set_state("running") + immediately rename here — that
            # per-turn PATCH (added in #236) saturated Discord's ~2-renames-per-
            # 10-min thread bucket and made the lamp stick (#241). The thread
            # name lamp is now the low-frequency, eventually-consistent sidebar
            # view owned by the state-sync poll (its is_processing guard still
            # promotes waiting→running within ≤60s).

            # Mark thread as PROCESSING when Claude starts
            if dashboard is not None:
                await dashboard.set_state(
                    thread.id,
                    ThreadState.PROCESSING,
                    description,
                    thread=thread,
                )

            async def _notify_stall() -> None:
                await thread.send(
                    "-# \u26a0\ufe0f No activity for 30s — could be extended thinking "
                    "or context compression. Will resume automatically."
                )

            status = StatusManager(user_message, on_hard_stall=_notify_stall)
            await status.set_running()

            model_override = await self._get_current_model()

            # Create session directory (git clone) and tmux session if configured
            import asyncio as _asyncio

            # session_dir_manager and tmux_manager already resolved above
            working_dir = self.runner.working_dir  # default
            if session_dir_manager is not None:
                session_dir = await _asyncio.to_thread(
                    session_dir_manager.create_session_dir, thread.id
                )
                working_dir = session_dir
                logger.info("Session dir for thread %d: %s", thread.id, session_dir)

            window_name: str | None = None
            if tmux_manager is not None:
                window_name = await _asyncio.to_thread(
                    tmux_manager.create_session, thread.id, working_dir or "."
                )
                logger.info("tmux window for thread %d: %s", thread.id, window_name)

                # Issue #95: redesigned thread naming.
                # Apply "<emoji> <topic> #<index>" to the Discord thread.
                # - Generate the stable topic once (on the first message that
                #   reaches a session row without a topic), persist it, and
                #   then keep it immutable unless the user renames manually
                #   (auto_topic_locked=1).
                # - The tmux window-index is a *hint* shown at the end of the
                #   name; the immutable tmux window-id is stored in the DB.
                with contextlib.suppress(Exception):
                    await self._apply_thread_naming(
                        thread=thread,
                        tmux_manager=tmux_manager,
                        first_message=prompt,
                    )

            # Create a TmuxClaudeRunner for this thread.
            # TUI mode cannot handle interactive permission prompts,
            # so always use --dangerously-skip-permissions.
            # tmux_manager is guaranteed non-None: resolver returns it iff the
            # channel has a /clord-init binding (same precondition as session_dir_manager,
            # checked above).
            assert tmux_manager is not None
            runner = TmuxClaudeRunner(
                tmux_manager=tmux_manager,
                thread_id=thread.id,
                model=model_override or self.runner.model,
                working_dir=working_dir,
                timeout_seconds=self.runner.timeout_seconds,
                dangerously_skip_permissions=True,
                try_continue=try_continue,
            )

            self._active_runners[thread.id] = runner

            # Issue #71: when CLORD_BRIDGE_MODE=jsonl, kick off a per-thread
            # transcript mirror so JSONL events flow to this Discord thread
            # without going through the discord-reply skill.  No-op otherwise.
            transcript_cog = getattr(self.bot, "transcript_mirror_cog", None)
            if transcript_cog is not None and working_dir:
                try:
                    transcript_cog.start_for(thread.id, working_dir)
                except Exception:
                    logger.warning(
                        "Failed to start TranscriptMirror for thread=%d",
                        thread.id,
                        exc_info=True,
                    )
            # Record the trigger message so TranscriptMirror / ApiServer can
            # thread the final answer back as a Discord reply (Issue #115).
            if transcript_cog is not None:
                with contextlib.suppress(Exception):
                    transcript_cog.set_trigger_message(thread.id, user_message.id)
            with contextlib.suppress(Exception):
                await self.repo.update_trigger_message(thread.id, user_message.id)

            stop_view = StopView(runner)
            if window_name:
                stop_msg = await thread.send(
                    f"-# ⏺ Session running (`{window_name}`)",
                    view=stop_view,
                )
            else:
                stop_msg = await thread.send("-# ⏺ Session running", view=stop_view)
            stop_view.set_message(stop_msg)

            try:
                await run_claude_with_config(
                    RunConfig(
                        thread=thread,
                        runner=runner,
                        repo=self.repo,
                        prompt=prompt,
                        session_id=session_id,
                        status=status,
                        registry=self._registry,
                        ask_repo=self._ask_repo,
                        lounge_repo=self._lounge_repo,
                        stop_view=stop_view,
                        session_dir_manager=session_dir_manager,
                        tmux_manager=tmux_manager,
                        image_paths=image_paths,
                        working_dir=working_dir or None,
                    )
                )
            finally:
                # Issue #91: wrap every Discord call so that RuntimeError("Session is
                # closed") during bot shutdown doesn't propagate and turn the bot into
                # a zombie.  The individual helpers already log errors internally.
                with contextlib.suppress(Exception):
                    await stop_view.disable()
                self._active_runners.pop(thread.id, None)
                self._active_tasks.pop(thread.id, None)

                with contextlib.suppress(Exception):
                    await coordination.post_session_end(thread)

                # #246: no per-turn thread rename here. The turn-end 🟡 lamp is
                # the StatusManager reaction (set via set_done() on the RESULT
                # event); the thread-name lamp flips to 🟡 on the next state-sync
                # poll (is_processing() is already False after the pop above).

                if dashboard is not None:
                    with contextlib.suppress(Exception):
                        await dashboard.set_state(
                            thread.id,
                            ThreadState.WAITING_INPUT,
                            description,
                            thread=thread,
                        )
