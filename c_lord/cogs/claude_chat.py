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
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import issue_ref as issue_ref_module
from .. import topic as topic_module
from ..attachments import ensure_git_excluded, save_attachment
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
from ..discord_ui.authorization import Authorizer
from ..discord_ui.embeds import stopped_embed
from ..discord_ui.permission_help import ThreadCreateForbiddenError, create_thread_permission_help
from ..discord_ui.status import StatusManager
from ..discord_ui.thread_dashboard import ThreadState, ThreadStatusDashboard
from ..discord_ui.views import (
    STOP_MESSAGE_PREFIX,
    ReopenSessionView,
    StopView,
    TextAnsweredMenuView,
)
from ..notify_policy import Kind, owner_notify_id
from ..session_close import apply_open_name, closed_notice_embed, is_closed
from ..session_reattach import (
    HISTORY_FILENAME,
    Plan,
    Recovery,
    plan_recovery,
    reattach_notice,
    recoverable_notice,
    render_history,
)
from ..session_resume import (
    NOT_A_CLORD_THREAD,
    UNTRACKED_NOTICE,
    UNTRACKED_REACTION,
    accepts_message,
    classify,
    is_clord_thread,
    resume_notice,
)
from ..thread_name import thread_lamp_enabled, thread_retitle_enabled
from ..thread_origin import inspect_origin
from ..thread_settings import resolve_auto_archive_duration
from ..utils.logger import log_ctx
from ._run_helper import run_claude_with_config
from .run_config import RunConfig

if TYPE_CHECKING:
    from ..bot import ClaudeDiscordBot
    from ..session_dir import SessionDirManager
    from ..tmux import TmuxSessionManager

logger = logging.getLogger(__name__)

# Longest sentence still treated as an answer to an open menu (#536 AC7).
# Matches AskModal's free-text limit: the two are the same affordance reached
# two ways, and past this length the message reads as a new request rather than
# a pick from a short menu.
_MENU_TEXT_ANSWER_MAX = 500

# Posts a reply the way the caller needs (interaction response vs ctx.send),
# letting /stop, /clear and their !text twins share one implementation (#209).
_Responder = Callable[..., Awaitable[None]]

# Attachment limits (#528). Attachments are written to disk and referenced by
# path, so the prompt no longer grows with the file — the old 50KB/100KB caps
# existed only because the bytes were pasted into it, and they silently ate
# every larger file. What is left is a disk-abuse guard sized above Discord's
# own upload ceiling, so in practice nothing a user can upload hits it.
_MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100 MB — Discord's largest tier
_MAX_ATTACHMENTS = 10

# Tells Claude the file is on disk — without it a bare path in the prompt reads
# like a mention rather than an instruction, and it answers from the filename.
_ATTACHMENT_PATH_NOTE = (
    "(The user attached this file; it is saved at the path above. "
    "Read it with the Read tool — it is not inlined in this message.)"
)


def _human_size(num_bytes: int) -> str:
    """Byte count as something a person can read in a Discord notice."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# Issue #75: shown when the bot lacks send permission (discord.Forbidden / 50001
# Missing Access) on a channel or thread.  Names the exact permissions to grant
# so the user can self-diagnose instead of staring at a silent failure.
_SEND_PERMISSION_HELP = (
    "❌ このチャンネル/スレッドに書き込み権限がありません。\n"
    "Bot に「メッセージの送信」「公開スレッドでのメッセージ送信」"
    "「プライベートスレッドでのメッセージ送信」を付与してください。"
)


def _requester_of_turn(
    user_message: discord.Message | None,
    explicit: discord.Member | discord.User | None,
    bot: object,
) -> discord.Member | discord.User | None:
    """Return who asked for this turn, or ``None`` when nobody did (#520).

    The trigger message's author is *not* always the requester: ``/clord`` and
    ``POST /api/spawn`` seed the thread with a message **c-lord itself** posts,
    so reading the author there names the bot — which is who the completion
    mention (#481), the interactive-prompt mention (#480) and the
    ``Co-authored-by`` trailer (#519) then pointed at.

    An ``explicit`` requester (the slash command's invoker) wins; otherwise the
    trigger message's author is used — unless it is c-lord's own seed message,
    which nobody is behind. A webhook or companion bot *is* a requester here:
    #519 deliberately records it as the provenance of the turn.
    """
    for candidate in (explicit, getattr(user_message, "author", None)):
        if candidate is None or _is_self(candidate, bot):
            continue
        return candidate
    return None


def _is_self(user: object, bot: object) -> bool:
    """True when *user* is c-lord itself (i.e. our own seed message)."""
    self_id = getattr(getattr(bot, "user", None), "id", None)
    return self_id is not None and getattr(user, "id", None) == self_id


def _notify_target(requester: object, bot: object, *, kind: Kind) -> int | None:
    """Discord user to @-mention for this turn, or ``None`` (#520).

    Only a human reads a ping, so a webhook- or bot-driven turn falls back to
    the configured owner — the same convention the scheduler and webhook cogs
    already use — instead of mentioning an account nobody watches. How far that
    fallback goes is deployment policy (#525): see :mod:`c_lord.notify_policy`.
    A turn a person actually asked for always mentions that person.
    """
    if requester is not None and not bool(getattr(requester, "bot", False)):
        return getattr(requester, "id", None)
    return owner_notify_id(bot, kind=kind)


async def _safe_set_state(
    dashboard: ThreadStatusDashboard,
    thread_id: int,
    state: ThreadState,
    description: str,
    **kwargs: object,
) -> None:
    """Update the dashboard, never letting its failure take the turn down (#632).

    The dashboard embed is decoration: a closed aiohttp session, a revoked
    permission or a Discord outage must not stop Claude from running or from
    answering. Before #632 the PROCESSING update was the one un-guarded Discord
    call on the turn path, so any of those killed the task before
    ``run_claude_with_config`` was reached and the user's message vanished with
    no reply, no ❌, nothing. Swallowed — but logged at WARNING, never silently.
    """
    try:
        await dashboard.set_state(thread_id, state, description, **kwargs)  # type: ignore[arg-type]
    except Exception:
        logger.warning(
            "%s dashboard set_state(%s) failed; continuing the turn",
            log_ctx(thread_id=thread_id),
            state.value,
            exc_info=True,
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
        thread_retitle: bool | None = None,
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
        # Automatic mid-conversation topic re-titling (#121). Off by default
        # (#414) — it fired too eagerly and renamed threads users didn't want
        # renamed. Opt in via thread_retitle=True or CLORD_THREAD_RETITLE=1.
        self._thread_retitle = thread_retitle_enabled(thread_retitle)
        self._allowed_user_ids = allowed_user_ids
        self._allowed_role_name = allowed_role_name
        # #466: one allowlist predicate shared by message gating (_is_allowed)
        # and button gating (View.interaction_check). Published on the bot so
        # cross-cog Views (AutoUpgrade) and the persistent-view restore path
        # (bot.py) enforce the same allowlist without extra wiring.
        self._authorizer = Authorizer(allowed_user_ids, allowed_role_name)
        if getattr(bot, "authorizer", None) is None:
            bot.authorizer = self._authorizer
        self._registry = registry or getattr(bot, "session_registry", None)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # #634: the startup sweep for a previous process's dead ⏹ Stop buttons.
        # Held so the fire-and-forget task is not garbage-collected mid-sweep.
        self._stop_sweep_task: asyncio.Task[None] | None = None
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
        # Issue #414: issue/PR number resolved before the session row exists;
        # drained by _apply_thread_naming on the next call once the row is saved.
        self._pending_issue_ref: dict[int, str] = {}
        # #512: thread ids reopened since the last turn. Consumed once, to swap the
        # #464 crash-recovery wording ("前回のセッションが落ちていたので…") for the
        # deliberate-reopen wording — a user who closed the session on purpose is
        # not recovering from a crash and should not be told they are.
        self._reopened_threads: set[int] = set()
        # Issue #429: thread ids already shown the "rename needs Manage Threads"
        # hint, so it is posted at most once per process per thread (not per turn).
        self._rename_hint_sent: set[int] = set()
        self._pending_tmux_window_id: dict[int, str] = {}
        # Issue #538: thread ids already told "this thread has no session to
        # restore", so the notice is posted at most once per process per thread
        # (the ⚠️ reaction still marks every dropped message).
        self._untracked_notice_sent: set[int] = set()
        # #538: where Claude Code keeps its transcripts. None = its real
        # location (``~/.claude/projects``); tests point it at a tmp dir.
        self._projects_root: Path | None = None

    def _is_allowed(self, member: discord.Member | discord.User) -> bool:
        """Check if a member/user is authorized to use the bot.

        OR logic: allowed_user_ids match OR allowed_role_name match.
        When neither is configured, everyone is allowed.

        Delegates to :class:`Authorizer` so message gating and button gating
        (View.interaction_check, #466) apply the exact same rule.
        """
        return self._authorizer.is_allowed(member)

    def _is_someone_elses_channel(
        self, channel_id: int | None, message: discord.Message | None
    ) -> bool:
        """True when this instance should stay quiet here (#522).

        Several c-lord instances can share a guild, and a text command reaches
        **every** one that can read the channel — so answering an unbound
        channel means each bystander bot posts the same public warning. A
        channel is ours when it is the configured ``DISCORD_CHANNEL_ID``; a
        bound channel never reaches this check (it resolves a manager).

        Only message-backed invocations are silenced: a slash command's reply
        is ephemeral, so it reaches its invoker and nobody else.
        """
        if message is None:
            return False
        return channel_id != getattr(self.bot, "channel_id", None)

    def _is_message_authorized(self, message: discord.Message) -> bool:
        """Whether *message* is allowed to drive Claude.

        Infrastructure bypasses the human allowlist so that configuring an
        owner does not break it:

        - **Webhook** messages (``webhook_id`` set): possession of the webhook
          URL is itself authorization (CI/CD triggers, E2E).
        - **Trusted bots** (``CLORD_TRUSTED_BOT_IDS``): companion bots treated
          like humans; pre-authorized.

        Any other bot is rejected. Human users must satisfy :meth:`_is_allowed`
        (owner / role), so a configured ``DISCORD_OWNER_ID`` restricts access to
        the owner **without** locking out webhooks or trusted bots.

        Applies to every request that has a message behind it: ``on_message``
        and the text commands ``!clord`` / ``!attach`` (#507). Slash commands
        have no message and can never be webhook-driven, so they gate on
        :meth:`_is_allowed` directly.
        """
        if message.webhook_id:
            return True
        if message.author.bot:
            trusted_raw = os.getenv("CLORD_TRUSTED_BOT_IDS", "")
            trusted_ids = {int(x.strip()) for x in trusted_raw.split(",") if x.strip().isdigit()}
            return message.author.id in trusted_ids
        return self._is_allowed(message.author)

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

    async def _divergent_session_name(self, thread: object) -> str | None:
        """The tmux session this thread is in, when it is not its channel's (#618).

        ``None`` for the common case, which keeps every existing thread name
        byte-identical (and therefore un-renamed).
        """
        from .channel_repo import ChannelRepoCog

        channel_id = getattr(thread, "parent_id", None)
        thread_id = getattr(thread, "id", None)
        if channel_id is None or thread_id is None:
            return None
        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is None or not isinstance(channel_cog, ChannelRepoCog):
            return None
        with contextlib.suppress(Exception):
            return await channel_cog.divergent_session_name(channel_id, thread_id)
        return None

    async def _bind_thread_repo(self, thread_id: int, channel_id: int, repo: str) -> str | None:
        """Bind a freshly created thread to *repo* (#514).

        Must happen before ``_run_claude``: that is where the session dir is
        cloned, and ``create_session_dir`` is idempotent, so a binding written
        afterwards would never be reflected on disk.
        """
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is None or not isinstance(channel_cog, ChannelRepoCog):
            return None
        return await channel_cog.bind_thread(thread_id, repo, channel_id)

    async def _resolve_tmux_manager(
        self, channel_id: int | None, *, thread_id: int | None
    ) -> TmuxSessionManager | None:
        """Resolve a TmuxSessionManager for the given channel (and optional thread).

        Lookup order: thread binding → channel binding → None. Does NOT fall
        back to a global bot.tmux_manager — a channel with neither a
        ``/clord-init`` nor a ``/clord-thread-init`` binding gets no manager.

        #427: ``thread_id`` used to be absent here while
        ``_resolve_session_dir_manager`` already honoured it, so a thread bound
        to another repo ran out of that repo's checkout inside the *parent
        channel's* tmux session. Always pass ``thread_id`` when a thread is in
        scope, so the two resolvers agree.
        """
        if channel_id is None:
            return None
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_tmux_manager(channel_id, thread_id=thread_id)
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
        working_dir: str | None = None,
    ) -> None:
        """Apply the Issue #95 / #414 naming scheme to ``thread``.

        - Generates and persists ``topic`` on first use (unless the
          thread is ``auto_topic_locked`` from a previous manual rename).
        - Resolves and persists the Issue/PR number (#414) from the session's
          git branch (``working_dir``) or, as a fallback, the first message.
        - Persists the tmux ``window_id`` (immutable) on the row.
        - Renames the Discord thread to
          ``<status_emoji> W<work_number> │ #<origin> <topic> →#<current>``
          capped at ``thread_name.MAX_NAME_LEN`` chars, but only if the current
          name differs (minimises API calls). The ``→#<current>`` half appears
          only while the work has moved off the Issue the thread was opened for
          (#593).

        Mid-conversation topic re-titling (#121) is gated on ``self._thread_retitle``
        (off by default, #414).

        All errors are swallowed by the caller; this helper raises only
        on truly unexpected programmer mistakes.
        """
        from ..thread_name import build_name, parse_topic_from_name

        record = await self.repo.get(thread.id)
        topic = record.topic if record else None
        locked = bool(record.auto_topic_locked) if record else False
        state = (record.state if record else None) or "alive"
        issue_ref = record.issue_ref if record else None
        # #593: the number the thread was *opened for*. Unlike issue_ref it never
        # follows the branch, so the sidebar keeps a stable handle on the thread
        # after the work moves to a spun-off Issue. set_issue_ref seeds it the
        # first time any number is known, so nothing else has to write it here.
        origin_issue_ref = record.origin_issue_ref if record else None
        # #428: text-based issue-ref detection runs ONLY on the thread's first
        # message (the branch is still read every turn). Captured before topic
        # generation: no topic persisted yet and none pending = first naming.
        # Otherwise a casual mid-thread '#1' would be captured and stick forever.
        is_first_message = not topic and thread.id not in self._pending_topic

        # Drain any topic / issue-ref / window-id pending from a previous call
        # where the session row did not yet exist.
        if record is not None:
            pending = self._pending_topic.pop(thread.id, None)
            if pending and not record.topic and not locked:
                await self.repo.set_topic(thread.id, pending[0], source=pending[1])
                topic = pending[0]
            pending_ref = self._pending_issue_ref.pop(thread.id, None)
            if pending_ref and not record.issue_ref:
                await self.repo.set_issue_ref(thread.id, pending_ref)
                issue_ref = pending_ref
                origin_issue_ref = origin_issue_ref or pending_ref
            pending_win = self._pending_tmux_window_id.pop(thread.id, None)
            if pending_win and record.tmux_window_id != pending_win:
                await self.repo.set_tmux_window_id(thread.id, pending_win)

        # Resolve tmux window-id / work-number (the stable w{N} number).
        info = await asyncio.to_thread(tmux_manager.get_window_info, thread.id)
        if info is not None:
            window_id, window_number = info
            if record is not None and record.tmux_window_id != window_id:
                await self.repo.set_tmux_window_id(thread.id, window_id)
            elif record is None:
                self._pending_tmux_window_id[thread.id] = window_id
        else:
            window_number = None

        # Re-summarize title on subsequent messages (#121) — opt-in only (#414).
        # Off by default because it renamed threads too eagerly; enable via
        # CLORD_THREAD_RETITLE=1. Only runs when a topic already exists and the
        # thread is not manually renamed (locked). Returns None when the LLM
        # deems the current topic still valid — so no rename API call is fired.
        if topic and not locked and self._thread_retitle:
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

        # Resolve the Issue/PR number (#414). The git branch is authoritative
        # and re-read every call (so a branch switch is followed); the first
        # message's #NNN / issue URL is only a fallback used until a number is
        # known. Never auto-cleared — a known number persists until a *different*
        # branch number appears.
        detected_ref = await self._detect_issue_ref(
            working_dir, first_message, current=issue_ref, allow_text=is_first_message
        )
        if detected_ref and detected_ref != issue_ref:
            issue_ref = detected_ref
            # #593: the very first number a thread learns is also its origin.
            origin_issue_ref = origin_issue_ref or detected_ref
            if record is not None:
                await self.repo.set_issue_ref(thread.id, detected_ref)
            else:
                self._pending_issue_ref[thread.id] = detected_ref

        # lamp=False (default, #329) keeps the topic but drops the leading
        # status emoji, so the name no longer changes on state transitions and
        # the only rename is the one-off topic naming. The #<issue> number (#414)
        # is shown when known.
        # #512: a thread closed mid-turn keeps its [終了] marker here too, so the
        # naming pass can never race the close and repaint the marker away.
        new_name = build_name(
            topic,
            state,
            window_number,
            lamp=self._thread_lamp,
            issue_ref=issue_ref,
            origin_issue_ref=origin_issue_ref,
            closed=is_closed(record),
            # #618: only set when the thread sits in another repo's session, so
            # the name carries its own attach target instead of a W<N> that is
            # ambiguous across sessions.
            session_label=await self._divergent_session_name(thread),
        )
        if (thread.name or "") == new_name:
            return
        try:
            await asyncio.wait_for(thread.edit(name=new_name), timeout=5.0)
            logger.info(
                "%s lamp → %s (event-driven) %r",
                log_ctx(thread_id=thread.id),
                state,
                new_name,
            )
        except (  # noqa: UP041 — asyncio.TimeoutError != builtins.TimeoutError on Python 3.10
            discord.HTTPException,
            TimeoutError,
            asyncio.TimeoutError,
        ) as exc:
            # #423: previously suppressed silently — a failed rename left a stuck
            # title (e.g. the monitoring thread) with no trace of *why*. Log the
            # reason (HTTP status/code if any) but keep swallowing: a cosmetic
            # rename must never break the response path.
            logger.warning(
                "%s thread rename failed: %r → %r: %s status=%s code=%s: %s",
                log_ctx(thread_id=thread.id),
                thread.name,
                new_name,
                type(exc).__name__,
                getattr(exc, "status", None),
                getattr(exc, "code", None),
                exc,
            )
            # #429: a 403 means the bot lacks "Manage Threads" in this server, so it
            # can post but never rename. Surface that to the user once per process
            # (re-hint after a restart so it stays visible until the perm is added).
            # Only for 403 — timeouts / other errors aren't user-actionable.
            if isinstance(exc, discord.Forbidden) and thread.id not in self._rename_hint_sent:
                self._rename_hint_sent.add(thread.id)
                with contextlib.suppress(discord.HTTPException):
                    # `-# ` renders as Discord subtext (small, muted) so the hint is
                    # unobtrusive — one quiet line, not a full warning message (#429).
                    await thread.send(
                        "-# スレッド名を自動更新できませんでした"
                        "（bot に「スレッドの管理 / Manage Threads」権限が必要）"
                    )

    async def _detect_issue_ref(
        self,
        working_dir: str | None,
        first_message: str,
        *,
        current: str | None,
        allow_text: bool,
    ) -> str | None:
        """Resolve the thread's Issue/PR number (#414), or ``None``.

        Priority: the session's git branch (authoritative, re-read each call) →
        the first message's ``#NNN`` / issue URL (only when ``allow_text`` — i.e.
        the thread's first message, #428 — and no number is known yet) → the
        current value (no change). Reading text on later messages would let a
        casual mid-thread ``#1`` be captured and stick forever.
        """
        branch_ref = await self._read_branch_issue_ref(working_dir)
        if branch_ref:
            return branch_ref
        if allow_text and not current:
            return issue_ref_module.extract_from_text(first_message or "")
        return current

    async def _read_branch_issue_ref(self, working_dir: str | None) -> str | None:
        """Read the git branch of ``working_dir`` and extract its issue number."""
        if not working_dir:
            return None
        branch = await asyncio.to_thread(self._git_current_branch, working_dir)
        return issue_ref_module.extract_from_branch(branch)

    @staticmethod
    def _git_current_branch(working_dir: str) -> str | None:
        """Return the current git branch name of ``working_dir`` (best-effort).

        Never raises — returns ``None`` on any error or detached HEAD. Uses a
        list-arg subprocess (no shell) per the security policy.
        """
        import subprocess

        try:
            result = subprocess.run(
                ["git", "-C", working_dir, "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages."""
        # Authorization: webhooks + trusted bots bypass the human allowlist;
        # any other bot is ignored; humans must match owner / role. See
        # _is_message_authorized. When no allowlist is configured, humans are
        # still allowed (zero-config default unchanged).
        if not self._is_message_authorized(message):
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

        # Handle threads: only respond if session exists in DB (opt-in).
        # The opt-in check goes through the shared verdict so that what we accept
        # here and what /tmux-screenshot & /resync *promise* can never disagree —
        # that disagreement is #538. A thread we do not accept is answered rather
        # than dropped in silence.
        if not isinstance(message.channel, discord.Thread):
            return
        verdict = classify(await self.repo.get(message.channel.id))
        if accepts_message(verdict):
            await self._handle_thread_reply(message)
        else:
            await self._handle_untracked_thread(message, message.channel)

    async def _handle_untracked_thread(
        self, message: discord.Message, thread: discord.Thread
    ) -> None:
        """Answer a message that landed in a thread with no ``sessions`` row (#538).

        Before #538 this path was ``return`` — no reply, no log — so a thread whose
        row was missing swallowed every message, including the ones sent because
        c-lord itself had promised the thread would resume on the next message
        (:mod:`c_lord.session_resume`). Three things happen instead, in decreasing
        order of how sure we are they are wanted:

        1. **A log line.** Greppable by ``thread=<id>``, so "I sent it and nothing
           happened" is diagnosable at all.
        2. **A ⚠️ reaction on the message**, so the 2nd and later messages still
           read as *seen but not run* rather than ignored.
        3. **The notice, once per thread per process.** It names the next step; it
           is also a wall of text, so it is not repeated on every message.

        Three cases get the log line only. **Webhook messages** (#556): nobody is
        waiting on the other end, so all three responses above are noise — see the
        guard below for what that cost in production. In a thread that is not ours
        (#522) it drops to DEBUG: several c-lord instances can share a guild and
        every one of them sees this message, so answering would mean each bystander
        bot posting the same notice. And while a turn is already in flight, the row
        is simply not written yet — see below. Nothing here may raise: this is
        already the path for a message we are failing to run.
        """
        parent_channel_id = getattr(thread, "parent_id", None) or thread.id
        ctx = log_ctx(thread_id=thread.id, channel_id=parent_channel_id)
        if message.webhook_id is not None:
            # #556: nothing that arrives from a webhook is waiting for an answer,
            # so none of the three responses below are owed to it. #538's guard
            # asked whether the *channel* was ours, which every thread under a
            # /clord-init binding satisfies — including Grafana's server-alert
            # thread, where from the #545 deploy on, each alert was given a ⚠️ and
            # a wall of text about restoring a session, during incidents.
            #
            # DEBUG, not INFO: an alerting webhook can be chatty, and unlike the
            # human case there is no one to tell.
            logger.debug("%s webhook message in an untracked thread — quiet (#556)", ctx)
            return
        if thread.id in self._active_runners or self.is_processing(thread.id):
            # A freshly spawned thread has no row until Claude emits its first
            # session_id, so a message sent in that window is not an untracked
            # thread — it is a session being born. Saying "no session to restore"
            # here would be wrong, and would be the first thing a new user sees.
            logger.info("%s message not run — the first turn is still starting (#538)", ctx)
            return
        if not await self._is_our_thread(parent_channel_id, thread.id, message):
            # Another instance's thread: DEBUG, so a shared guild's traffic does
            # not drown the log — the INFO line below is for threads we own.
            logger.debug("%s message ignored — not this instance's thread (#538)", ctx)
            return
        if not await self._was_ever_our_thread(thread, parent_channel_id):
            # #556: the check above says the *channel* is ours, which every
            # thread under a /clord-init binding satisfies — so on its own it
            # sent this notice into ordinary conversation threads. A thread that
            # carries no trace of c-lord at all has nothing to restore and never
            # did; there is nothing to tell its author.
            logger.debug("%s message ignored — never a c-lord thread (#556)", ctx)
            return
        logger.info("%s message not run — no session row for this thread (#538)", ctx)

        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction(UNTRACKED_REACTION)

        if thread.id in self._untracked_notice_sent:
            return
        self._untracked_notice_sent.add(thread.id)
        await self._offer_recovery(thread, parent_channel_id)

    async def _offer_recovery(self, thread: discord.Thread, parent_channel_id: int) -> None:
        """Post the notice, with a 🔗 再接続する button when there is one — #538 AC6.

        Before this the notice could only say the thread was beyond help and send
        the reader off to start a new one. Usually that was wrong: #554 takes the
        row and leaves the checkout, so the work is still there and the thread can
        be reconnected to it. The button goes on this message because this is the
        message the confused person is already reading (AC6: reachable from
        Discord). When nothing survived, the wording is unchanged and names the
        way forward instead (AC8).
        """
        from ..discord_ui.views import ReattachSessionView

        sdm = await self._resolve_session_dir_manager(parent_channel_id, thread_id=thread.id)
        plan = plan_recovery(
            session_dir_base=getattr(sdm, "base_dir", None),
            thread_id=thread.id,
            projects_root=self._projects_root,
        )
        if plan.kind is Recovery.NONE:
            with contextlib.suppress(discord.HTTPException):
                await thread.send(UNTRACKED_NOTICE)
            return

        view = ReattachSessionView(lambda _i: self._reattach_thread(thread))
        with contextlib.suppress(discord.HTTPException):
            await thread.send(recoverable_notice(plan), view=view)

    async def _is_our_thread(
        self, parent_channel_id: int, thread_id: int, message: discord.Message
    ) -> bool:
        """Whether this instance should speak up in this thread (#522).

        Ours when the parent channel is the configured ``DISCORD_CHANNEL_ID``, or
        when either resolver finds a ``/clord-init`` / ``/clord-thread-init``
        binding for it. Everything else belongs to another bot in the same guild.
        """
        if not self._is_someone_elses_channel(parent_channel_id, message):
            return True
        if await self._resolve_tmux_manager(parent_channel_id, thread_id=thread_id) is not None:
            return True
        return (
            await self._resolve_session_dir_manager(parent_channel_id, thread_id=thread_id)
            is not None
        )

    async def _was_ever_our_thread(self, thread: discord.Thread, parent_channel_id: int) -> bool:
        """Whether this thread carries any trace of having been c-lord's (#556).

        Distinct from :meth:`_is_our_thread`, which asks about the *channel* and
        stays as the #522 cross-instance guard. This asks about the thread, and
        it has to, because the ``sessions`` row that would have answered it is
        exactly what the 30-day sweep deletes (#554) — see
        :mod:`c_lord.thread_origin` for why these three signals and not the row.

        Best-effort: a resolver or DB hiccup must not decide the question, and it
        errs toward **speaking** — the cost of a stray notice in a c-lord thread
        is far below the cost of silently swallowing a message again, which is
        the bug (#538) this whole path exists to fix.
        """
        try:
            sdm = await self._resolve_session_dir_manager(parent_channel_id, thread_id=thread.id)
            has_binding = await self._thread_binding_exists(thread.id)
        except Exception:
            logger.warning("%s origin check failed — assuming ours", log_ctx(thread_id=thread.id))
            return True
        bot_user = getattr(self.bot, "user", None)
        origin = inspect_origin(
            thread_owner_id=getattr(thread, "owner_id", None),
            bot_user_id=getattr(bot_user, "id", None),
            session_dir_base=getattr(sdm, "base_dir", None),
            thread_id=thread.id,
            has_binding=has_binding,
        )
        return origin.is_clords

    async def _handle_clord_without_session(
        self, thread: discord.Thread, parent_channel_id: int, respond: _Responder
    ) -> None:
        """``/clord`` in a thread with no ``sessions`` row — #551 branches 2 and 3.

        No row means one of two very different things, and the first cut of #551
        conflated them. #554 deletes the row after 30 days, so a month-quiet
        c-lord thread looks exactly like a thread c-lord never touched — and
        refusing both would have made ``W3 │ Qiita``, checkout and half-written
        article intact, permanently unreachable.

        :mod:`c_lord.thread_origin` tells them apart (the same test #556 uses, so
        the two cannot drift):

        * **was ours** → offer the reconnect (#538). Not a takeover: it reattaches
          to what is already on disk, and starts no new session.
        * **never ours** → refuse and change nothing, which is what #551 is for.
        """
        ctx = log_ctx(thread_id=thread.id, channel_id=parent_channel_id)
        if await self._was_ever_our_thread(thread, parent_channel_id):
            logger.info("%s /clord: no session row, offering reconnect (#551/#538)", ctx)
            await self._offer_recovery(thread, parent_channel_id)
            await respond(
                "ℹ️ このスレッドの記録が見つかりませんでした。スレッドに再接続の案内を出しました。",
                ephemeral=True,
            )
            return
        logger.info("%s /clord refused — never a c-lord thread (#551)", ctx)
        await respond(NOT_A_CLORD_THREAD, ephemeral=True)

    async def _reattach_thread(self, thread: discord.Thread) -> Plan:
        """Reconnect ``thread`` to the Claude session it already has — #538 AC6.

        Writes back the ``sessions`` row that #554's 30-day sweep deleted, using
        only what is already on disk. It reads; it never clones, never creates a
        session dir, and returns :attr:`Recovery.NONE` without writing anything
        when there is nothing to reattach to (AC7) — a recovery that could
        manufacture a session would be #551's takeover under a nicer name.

        For a WORKDIR recovery the Discord thread is written into the checkout as
        the hand-off document, because the transcript that held the conversation
        is gone and the thread is where it still exists. That export is a bonus,
        not the recovery: the row is what reconnects the thread, so a failure to
        collect or write the history is logged and the recovery still stands.
        """
        parent_channel_id = getattr(thread, "parent_id", None) or thread.id
        ctx = log_ctx(thread_id=thread.id, channel_id=parent_channel_id)
        sdm = await self._resolve_session_dir_manager(parent_channel_id, thread_id=thread.id)
        plan = plan_recovery(
            session_dir_base=getattr(sdm, "base_dir", None),
            thread_id=thread.id,
            # None = Claude Code's real ~/.claude/projects; overridden in tests.
            projects_root=self._projects_root,
        )
        if plan.kind is Recovery.NONE or plan.working_dir is None:
            logger.info("%s reattach found nothing to reconnect to (#538)", ctx)
            return plan

        if plan.kind is Recovery.WORKDIR:
            await self._export_thread_history(thread, plan.working_dir, ctx)

        # ``session_id`` is what --resume needs; for a WORKDIR recovery there is
        # no id left to resume, so the tmux-derived placeholder is used — the
        # same shape a thread carries before Claude reports its first real id.
        await self.repo.save(
            thread_id=thread.id,
            session_id=plan.session_id or f"tmux-{thread.id}",
            working_dir=plan.working_dir,
        )
        logger.info("%s reattached (%s) dir=%s", ctx, plan.kind.value, plan.working_dir)
        return plan

    async def _export_thread_history(
        self, thread: discord.Thread, working_dir: str, ctx: str
    ) -> None:
        """Write the Discord thread into the checkout for Claude to read."""
        try:
            messages = await self._collect_thread_history(thread)
            target = Path(working_dir) / HISTORY_FILENAME
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_history(messages), encoding="utf-8")
            logger.info("%s wrote %d message(s) to %s", ctx, len(messages), HISTORY_FILENAME)
        except Exception:
            # Never fatal — see _reattach_thread's docstring.
            logger.warning("%s could not export thread history (#538)", ctx, exc_info=True)

    async def _collect_thread_history(
        self, thread: discord.Thread, limit: int = 500
    ) -> list[tuple[str, str, str]]:
        """Oldest-first ``(author, timestamp, text)`` for this thread.

        c-lord reads its own threads with its own token, so no extra plumbing is
        needed. Empty messages (embed-only tool cards, attachments) are skipped:
        they carry nothing for a reader trying to reconstruct what was being done.
        """
        out: list[tuple[str, str, str]] = []
        async for message in thread.history(limit=limit, oldest_first=True):
            text = (message.content or "").strip()
            if not text:
                continue
            stamp = message.created_at.strftime("%Y-%m-%d %H:%M") if message.created_at else ""
            out.append((message.author.display_name, stamp, text))
        return out

    async def _thread_binding_exists(self, thread_id: int) -> bool:
        """Whether ``/clord-thread-init`` bound a repo to this thread."""
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is None or not isinstance(channel_cog, ChannelRepoCog):
            return False
        return await channel_cog.has_thread_binding(thread_id)

    async def _clord_impl(
        self,
        *,
        channel: object,
        channel_id_fallback: int | None,
        user: discord.Member | discord.User,
        prompt: str,
        respond: _Responder,
        ack: _Responder,
        message: discord.Message | None = None,
        repo: str | None = None,
    ) -> None:
        """Shared core for /clord and !clord (#209 follow-up).

        In a thread → continue the session; in a channel → create a new thread
        via ``spawn_session``. ``ack`` defers (slash) or is a no-op (text); after
        it, all replies go through ``respond``'s post-ack path.

        ``repo`` (#514) names the repository for a *new* thread, making the
        channel binding optional — the point being to pick a repo without first
        having to find or rebind a channel.

        ``message`` is the invoking message for the text command; ``None`` for
        slash, which has no message behind it. When present, authorization goes
        through :meth:`_is_message_authorized` so a configured
        ``DISCORD_OWNER_ID`` does not lock out webhooks / trusted bots (#507).
        """
        # Authorization check. `!clord` is reachable from webhooks by design —
        # ClaudeDiscordBot.process_commands lets them through — so a message-
        # backed invocation must use the same rule as on_message, not the
        # human-only allowlist (#507).
        authorized = (
            self._is_message_authorized(message) if message is not None else self._is_allowed(user)
        )
        if not authorized:
            await respond("You are not authorized to use this command.", ephemeral=True)
            return

        # Unbound channel check: verify /clord-init or /clord-thread-init binding
        if repo is not None:
            from ..database.channel_repo import validate_repo_url

            try:
                repo = validate_repo_url(repo)
            except ValueError as exc:
                await respond(f"⚠️ リポジトリ URL が不正です: {exc}", ephemeral=True)
                return

        if isinstance(channel, discord.Thread):
            if repo is not None:
                # This thread's session dir is already cloned and
                # create_session_dir() is idempotent, so rebinding here would
                # change the record and nothing on disk — it would read as the
                # option being ignored. Point at the command that does rebind.
                await respond(
                    "⚠️ `repo:` は新しいスレッドを立てるときだけ指定できます。\n"
                    "このスレッドのリポジトリを変えるには `/clord-thread-init repo:<URL>` を"
                    "使ってください。",
                    ephemeral=True,
                )
                return
            parent_channel_id = channel.parent_id or channel.id
            sdm = await self._resolve_session_dir_manager(parent_channel_id, thread_id=channel.id)
            tmux = await self._resolve_tmux_manager(parent_channel_id, thread_id=channel.id)
            if sdm is None and tmux is None:
                if self._is_someone_elses_channel(parent_channel_id, message):
                    return
                await respond(
                    "⚠️ このスレッドにはリポジトリが紐づけられていません。\n"
                    "先に `/clord-thread-init repo:<URL>` または"
                    " `/clord-init repo:<URL>` で設定してください。",
                    ephemeral=True,
                )
                return
            # #551: everything above only established that *a repo* is reachable
            # from here, which is true of every thread under a bound channel —
            # human conversations included. Running on for one of those wrote the
            # ``sessions`` row, and from that moment ``on_message`` sent every
            # message in the thread to Claude. Nothing below this line is
            # reversible from the thread, so the branch goes here: before the seed
            # message, before ``_run_claude``'s clone.
            #
            # Ordering note: after the resolver check on purpose. A thread in
            # another instance's unbound channel returns silently above, so a
            # shared guild does not get one refusal per bystander bot (#522).
            thread_record = await self.repo.get(channel.id)
            if not is_clord_thread(classify(thread_record)):
                await self._handle_clord_without_session(channel, parent_channel_id, respond)
                return
        else:
            channel_id = (
                channel.id
                if isinstance(channel, discord.abc.GuildChannel)
                else (channel_id_fallback)
            )
            # An explicit repo: stands in for the channel binding (#514) — that
            # is the whole point of the option, so do not demand /clord-init.
            if repo is None:
                sdm = await self._resolve_session_dir_manager(channel_id)
                # No thread exists yet — /clord is creating one, so this is a
                # channel-binding existence check (#600 audit).
                tmux = await self._resolve_tmux_manager(channel_id, thread_id=None)
                if sdm is None and tmux is None:
                    if self._is_someone_elses_channel(channel_id, message):
                        return
                    await respond(
                        "⚠️ このチャンネルにはリポジトリが紐づけられていません。\n"
                        "`/clord repo:<URL> prompt:<やること>` でこの1回だけ指定するか、"
                        "`/clord-init repo:<URL>` でチャンネルに設定してください。",
                        ephemeral=True,
                    )
                    return

        await ack()

        if isinstance(channel, discord.Thread):
            # Continue in existing thread. The row was read by the #551 branch
            # above, which only lets a thread through when it has one.
            session_id = (thread_record.session_id or None) if thread_record else None
            try:
                seed_message = await channel.send(prompt)
            except discord.Forbidden:
                # #75: bot lacks "Send Messages in Threads" → tell the user the
                # exact missing permission instead of a silent 50001 traceback.
                logger.warning(
                    "%s seed send forbidden (missing send permission)",
                    log_ctx(thread_id=channel.id),
                    exc_info=True,
                )
                await respond(_SEND_PERMISSION_HELP, ephemeral=True)
                return
            # #520: the seed message above is ours, so hand the invoker down
            # explicitly — otherwise the turn would ping and credit the bot.
            await self._run_claude(
                seed_message, channel, prompt=prompt, session_id=session_id, requester=user
            )
            await respond("Session completed.", silent=True)
        else:
            # Create a new thread via spawn_session (text channels only)
            if not isinstance(channel, discord.TextChannel):
                await respond("This command must be used in a text channel.", ephemeral=True)
                return
            try:
                thread = await self.spawn_session(channel, prompt, repo=repo, requester=user)
            except ThreadCreateForbiddenError:
                # #443: bot lacks View Channel / Create Public Threads here.
                # Reply via the interaction (ephemeral) — the channel itself is
                # unreachable, so this is the only path that reaches the user.
                await respond(create_thread_permission_help(), ephemeral=True)
                return
            except discord.Forbidden:
                # #75: seed send into the new thread failed; spawn_session has
                # already notified the parent channel.  Acknowledge via the
                # interaction too so the slash command does not surface a raw
                # traceback (defer with no followup = hung "thinking" spinner).
                await respond(_SEND_PERMISSION_HELP, ephemeral=True)
                return
            await respond(f"Session started → {thread.mention}")

    async def _repo_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Offer the channel default first, then every repo this bot knows (#514).

        Showing the default is what tells the reader the option is skippable —
        an empty box reads as something they are expected to fill in.
        """
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is None or not isinstance(channel_cog, ChannelRepoCog):
            return []

        choices: list[app_commands.Choice[str]] = []
        needle = current.lower()

        parent_id = getattr(interaction.channel, "parent_id", None) or interaction.channel_id
        default = None
        if parent_id is not None:
            with contextlib.suppress(Exception):
                default = await channel_cog.channel_repo(parent_id)
        if default is not None and needle in default.lower():
            choices.append(
                app_commands.Choice(
                    name=f"未指定ならこれ（このチャンネル）: {default}"[:100], value=default[:100]
                )
            )

        with contextlib.suppress(Exception):
            for repo in await channel_cog.known_repos():
                if repo == default or needle not in repo.lower():
                    continue
                choices.append(app_commands.Choice(name=repo[:100], value=repo[:100]))
                if len(choices) >= 25:
                    break
        return choices

    @app_commands.command(name="clord", description="Start a new Claude Code session")
    @app_commands.describe(
        prompt="Message to send to Claude Code",
        repo="省略可 — 新しいスレッドで使うリポジトリ。未指定ならこのチャンネルの設定",
    )
    @app_commands.autocomplete(repo=_repo_autocomplete)
    async def start_session(
        self, interaction: discord.Interaction, prompt: str, repo: str | None = None
    ) -> None:
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
            repo=repo,
        )

    @staticmethod
    def _split_repo_prefix(prompt: str) -> tuple[str | None, str]:
        """Peel a leading ``repo:<url>`` off a ``!clord`` prompt (#514).

        Mirrors the slash option's spelling so the two twins read the same. Only
        the *first* token counts, and only when something follows it — a prompt
        that merely starts with the word "repository" is left alone.
        """
        if not prompt.startswith("repo:"):
            return None, prompt
        head, sep, rest = prompt.partition(" ")
        repo = head[len("repo:") :]
        if not sep or not repo or not rest.strip():
            return None, prompt
        return repo, rest.strip()

    @commands.command(name="clord")
    async def clord_text(self, ctx: commands.Context, *, prompt: str | None = None) -> None:
        """Text/mention twin of /clord — invokable from webhooks for E2E (#209).

        Usage: ``!clord <prompt>`` / ``!clord repo:<URL> <prompt>`` (#514).
        """
        if not prompt:
            await ctx.send("Usage: `!clord [repo:<URL>] <prompt>`")
            return

        repo, prompt = self._split_repo_prefix(prompt)

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
            message=ctx.message,
            repo=repo,
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

    @app_commands.command(
        name="clord-reattach",
        description="このスレッドのワークスペースに再接続する（記録が消えたとき）",
    )
    async def clord_reattach(self, interaction: discord.Interaction) -> None:
        """Reconnect this thread to the Claude session already on disk — #538 AC6.

        The button on the notice covers the case where someone stumbled into it;
        this is for someone who knows what happened and wants to fix it without
        first sending a message that will not run.

        It reattaches only — see :meth:`_reattach_thread`. In a thread with
        nothing on disk it reports that and changes nothing (AC7/AC8), which is
        what keeps it from being an "adopt this thread" command by another name.
        """
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message(
                "このコマンドはスレッド内でのみ使えます。", ephemeral=True
            )
            return
        if not self._is_allowed(interaction.user):
            await interaction.response.send_message(
                "You are not authorized to use this command.", ephemeral=True
            )
            return

        existing = await self.repo.get(channel.id)
        if existing is not None:
            await interaction.response.send_message(
                "ℹ️ このスレッドの記録は失われていません。"
                "そのままメッセージを送れば続きから再開します。",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        plan = await self._reattach_thread(channel)
        await interaction.followup.send(reattach_notice(plan))

    @commands.command(name="clord-reattach")
    async def clord_reattach_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /clord-reattach — webhook-invokable for E2E (#209)."""
        channel = ctx.channel
        if not isinstance(channel, discord.Thread):
            await ctx.send("このコマンドはスレッド内でのみ使えます。")
            return
        if not self._is_message_authorized(ctx.message):
            await ctx.send("You are not authorized to use this command.")
            return
        if await self.repo.get(channel.id) is not None:
            await ctx.send(
                "ℹ️ このスレッドの記録は失われていません。"
                "そのままメッセージを送れば続きから再開します。"
            )
            return
        plan = await self._reattach_thread(channel)
        await ctx.send(reattach_notice(plan))

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
    @app_commands.describe(window="tmux window name (e.g. w1)")
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
        tmux_manager = await self._resolve_tmux_manager(parent_id, thread_id=interaction.channel.id)
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
        Usage: !attach w13
        """
        if not isinstance(ctx.channel, discord.Thread):
            await ctx.send("This command can only be used in a thread.")
            return

        # Message-backed invocation: webhooks reach text commands by design, so
        # use the same rule as on_message rather than the human allowlist (#507).
        if not self._is_message_authorized(ctx.message):
            await ctx.send("You are not authorized to use this command.")
            return

        parent_id = getattr(ctx.channel, "parent_id", None) or ctx.channel.id
        tmux_manager = await self._resolve_tmux_manager(parent_id, thread_id=ctx.channel.id)
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
        tmux_manager = await self._resolve_tmux_manager(parent_id, thread_id=thread_id)
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

    async def _restart_impl(self, channel: object, respond: _Responder) -> None:
        """Shared core for /claude-restart and its /restart-claude alias (#440, #578).

        Restarts the Claude **process** for this thread while PRESERVING the
        conversation. It kills the active runner and the tmux window — so a
        stuck / wedged claude process is gone — but, unlike :meth:`_clear_impl`,
        it does **not** reset the session row. With the window dead and a live
        ``session_id`` still on disk, the next message hits the #270 dead-pane
        path and resumes via ``--continue``, so the context survives.

        This sits between ``/resync`` (reconnect the Discord mirror only — the
        process is untouched) and ``/clear`` (wipe the session, start fresh).
        Observationally for the Discord user: after this, your next message is
        handled by a fresh claude process that still remembers the conversation.
        """
        if not isinstance(channel, discord.Thread):
            await respond("This command can only be used in a Claude chat thread.", ephemeral=True)
            return

        thread_id = channel.id

        # A session must exist to restart-and-resume; without one there is
        # nothing to ``--continue`` and this would be a no-op.
        record = await self.repo.get(thread_id)
        if record is None:
            await respond("No active session found for this thread to restart.", ephemeral=True)
            return

        # Kill the active runner (graceful kill of the in-flight, possibly
        # wedged, turn) if one is registered.
        runner = self._active_runners.get(thread_id)
        if runner:
            await runner.kill()
            del self._active_runners[thread_id]

        # Kill the tmux window so the old/stuck claude process is gone and
        # ``is_claude_running`` returns False. We deliberately do NOT reset the
        # session row — that is what distinguishes restart from /clear and lets
        # the next message resume the conversation via --continue (#270, #123).
        parent_id = getattr(channel, "parent_id", None) or thread_id
        tmux_manager = await self._resolve_tmux_manager(parent_id, thread_id=thread_id)
        if tmux_manager is not None:
            await asyncio.to_thread(tmux_manager.kill_session, thread_id)

        await respond(
            "\U0001f504 Claude を再起動しました。会話は保持されています — "
            "次のメッセージで `--continue` により文脈を引き継いで再開します。"
        )

    @staticmethod
    def _slash_text_responder(interaction: discord.Interaction) -> _Responder:
        """Responder that answers a slash interaction with plain text."""

        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            ephemeral: bool = False,
        ) -> None:
            await interaction.response.send_message(content, ephemeral=ephemeral)

        return respond

    @staticmethod
    def _ctx_text_responder(ctx: commands.Context) -> _Responder:
        """Responder that answers a text/mention command with plain text."""

        async def respond(
            content: str | None = None,
            *,
            embed: discord.Embed | None = None,
            ephemeral: bool = False,
        ) -> None:
            await ctx.send(content or "")

        return respond

    @app_commands.command(
        name="claude-restart",
        description="Restart the Claude process for this thread (keeps the conversation)",
    )
    async def claude_restart(self, interaction: discord.Interaction) -> None:
        """Restart Claude for this thread, preserving context (#440).

        Named object-first (``claude-restart``) in #578: the object is the
        Claude **process**, and Discord's autocomplete matches by prefix, so
        typing ``/claude`` surfaces this next to the other Claude-scoped
        commands instead of hiding it under ``/restart``.
        """
        await self._restart_impl(interaction.channel, self._slash_text_responder(interaction))

    @commands.command(name="claude-restart")
    async def claude_restart_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of /claude-restart — invokable from webhooks (#440)."""
        await self._restart_impl(ctx.channel, self._ctx_text_responder(ctx))

    @app_commands.command(
        name="restart-claude",
        description="(旧名) /claude-restart と同じです",
    )
    async def restart_claude(self, interaction: discord.Interaction) -> None:
        """Old name for :meth:`claude_restart`, kept working (#578).

        Renaming a command people have in their fingers is not free. The alias
        calls the same implementation, so there is nothing to keep in sync — and
        consumers get the new name by updating the package alone, which is the
        Zero-Config Principle.
        """
        await self._restart_impl(interaction.channel, self._slash_text_responder(interaction))

    @commands.command(name="restart-claude")
    async def restart_claude_text(self, ctx: commands.Context) -> None:
        """Text/mention twin of the /restart-claude alias (#440)."""
        await self._restart_impl(ctx.channel, self._ctx_text_responder(ctx))

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
        tmux_manager = await self._resolve_tmux_manager(parent_id, thread_id=thread_id)
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
        try:
            thread = await message.create_thread(
                name=thread_name, auto_archive_duration=archive_minutes
            )
        except discord.Forbidden:
            # #443: the bot can see this channel (it received the message) but
            # lacks "Create Public Threads".  Tell the user in-channel; if
            # "Send Messages" is also missing, suppress (nothing else we can do).
            logger.warning(
                "%s create_thread forbidden on message trigger (missing Create Public Threads)",
                log_ctx(thread_id=message.id),
                exc_info=True,
            )
            with contextlib.suppress(discord.HTTPException):
                await message.reply(create_thread_permission_help())
            return
        prompt, image_paths = await self._build_prompt_and_images(message)
        prompt = await enrich_discord_references(prompt, message, self.bot)
        await self._run_claude(message, thread, prompt, session_id=None, image_paths=image_paths)

    async def spawn_session(
        self,
        channel: discord.TextChannel,
        prompt: str,
        thread_name: str | None = None,
        session_id: str | None = None,
        repo: str | None = None,
        requester: discord.Member | discord.User | None = None,
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
            repo: Optional repository for this thread (#514). Bound to the new
                  thread before Claude starts, so the session dir is cloned
                  from it instead of from the channel's binding.
            requester: Discord user who asked for this session (#520). The seed
                  message below is authored by the bot, so without this the turn
                  would ping and credit c-lord itself. ``None`` (e.g.
                  ``POST /api/spawn``) ⇒ no person behind the turn: the ping
                  falls back to the bot owner and no Discord co-author is
                  recorded.

        Returns:
            The newly created :class:`discord.Thread`.
        """
        name = (thread_name or prompt)[:100]
        archive_minutes = await resolve_auto_archive_duration(self._settings_repo)
        try:
            thread = await channel.create_thread(
                name=name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=archive_minutes,
            )
        except discord.Forbidden as exc:
            # #443: bot was denied "View Channel" and/or "Create Public Threads"
            # on this channel.  The channel itself is unreachable (a notice
            # posted there would fail with another Forbidden), so signal the
            # caller to surface the permission help via the interaction instead.
            logger.warning(
                "%s create_thread forbidden (missing View Channel / Create Public Threads)",
                log_ctx(channel_id=channel.id),
                exc_info=True,
            )
            raise ThreadCreateForbiddenError() from exc

        # #514: bind before anything clones. _run_claude resolves the session
        # dir manager from this binding, and create_session_dir() is idempotent,
        # so a binding written any later would never reach the disk.
        if repo is not None:
            bound = await self._bind_thread_repo(thread.id, channel.id, repo)
            if bound is None:
                logger.warning(
                    "%s repo:%s requested but thread bindings are not enabled — "
                    "falling back to the channel binding",
                    log_ctx(thread_id=thread.id),
                    repo,
                )

        # Post the prompt so StatusManager has a Message to add reactions to.
        try:
            seed_message = await thread.send(prompt)
        except discord.Forbidden:
            # #75: bot can create the thread but lacks "Send Messages in
            # Threads".  The thread itself is unreachable, so surface the
            # permission guidance in the parent channel where the user can see
            # it, then re-raise so the caller (/clord, /api/spawn) knows the
            # spawn failed instead of returning a dead thread.
            logger.warning(
                "%s spawn seed send forbidden (missing send permission)",
                log_ctx(thread_id=thread.id),
                exc_info=True,
            )
            with contextlib.suppress(discord.HTTPException):
                await channel.send(_SEND_PERMISSION_HELP)
            raise
        # Run Claude in the background so /api/spawn returns immediately.
        # The caller gets the thread reference without waiting for Claude to finish.
        asyncio.create_task(
            self._run_claude(
                seed_message, thread, prompt, session_id=session_id, requester=requester
            )
        )
        return thread

    async def cog_unload(self) -> None:
        """Record all mid-run Claude sessions so they get a restart notice.

        Called by discord.py whenever the cog is removed — including during a
        clean shutdown triggered by ``systemctl restart/stop``, ``bot.close()``,
        or any other SIGTERM-based shutdown.  Sessions that were actively running
        when the bot was killed are recorded so that, on the next startup,
        ``on_ready`` posts a quiet restart notice into each thread.

        #406: we do **not** re-prompt Claude on restart.  The ``claude`` process
        in tmux survives a bot restart and the observers (TranscriptMirror + menu
        watchdog) self-restore, so injecting a "continue your work" prompt only
        caused unwanted autonomous progress (意図しない前進).

        Idle sessions (where Claude has already replied and is waiting for the
        next human message) are NOT in ``_active_runners`` and therefore are not
        recorded — they resume naturally via message-triggered resume when the
        user sends their next message.

        No-op when ``_resume_repo`` is not configured.
        """
        if not self._active_runners or self._resume_repo is None:
            return

        logger.info(
            "Shutdown detected: recording %d active session(s) for restart notice",
            len(self._active_runners),
        )
        for thread_id in list(self._active_runners):
            try:
                session_id: str | None = None
                record = await self.repo.get(thread_id)
                if record is not None:
                    session_id = record.session_id

                # resume_prompt is intentionally None (#406): on_ready posts a
                # quiet notice, it never re-runs Claude with a stored prompt.
                await self._resume_repo.mark(
                    thread_id,
                    session_id=session_id,
                    reason="bot_shutdown",
                    resume_prompt=None,
                )
                logger.info(
                    "Recorded thread %d for restart notice (session=%s)", thread_id, session_id
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

        It also sweeps away ``⏹ Stop`` buttons a previous process could not
        delete (#634). Startup is the only moment at which "every stop button in
        the DB's threads is dead" is guaranteed true, so it is the only moment
        the sweep is safe. Spawned as its own task: it walks up to a few hundred
        threads and must not hold up becoming ready.
        """
        # Held on the cog so the task is not garbage-collected mid-sweep.
        self._stop_sweep_task = asyncio.create_task(self._sweep_dead_stop_buttons())

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

            # #406: Do NOT re-prompt Claude on restart.  The ``claude`` process
            # in tmux survives a bot restart, and the observers (TranscriptMirror
            # for the reply text + the always-on menu watchdog for AskUserQuestion
            # /plan menus) self-restore on startup.  Re-running Claude with a
            # "continue your remaining work" prompt only caused unwanted autonomous
            # progress (意図しない前進) and made the conversation appear to "jump"
            # when the queued tmux activity surfaced all at once.  Just post a
            # quiet, human-facing notice that recent display may have lagged.
            logger.info(
                "Restart notice for thread %d (session_id=%s, reason=%s) — "
                "observers self-restored; not re-prompting Claude (#406)",
                thread_id,
                entry.session_id,
                entry.reason,
            )
            try:
                await thread.send(
                    "-# 🔄 C-lord を再起動しました。少し前の表示が遅延した可能性があります。"
                )
            except Exception:
                logger.error("Failed to post restart notice in thread %d", thread_id, exc_info=True)

    async def _sweep_dead_stop_buttons(self) -> None:
        """Remove the previous process's dead ⏹ Stop buttons (#634). Never raises."""
        from ..stale_stop_buttons import sweep_dead_stop_buttons

        with contextlib.suppress(Exception):
            await sweep_dead_stop_buttons(self.bot, self.repo)

    async def _handle_thread_reply(self, message: discord.Message) -> None:
        """Continue a Claude Code session in an existing thread.

        A new message while a turn is already in flight **interrupts** that turn
        and starts fresh with the new instruction — the documented behaviour (see
        USER_GUIDE → "Interrupting").  :meth:`_preempt_prior_turn` does the
        teardown, which also covers a turn parked on a bridged
        AskUserQuestion/plan menu whose ``timeout=None`` await previously wedged
        the thread forever (#315).

        A per-thread asyncio.Lock serializes the *setup* of concurrent calls so a
        second message arriving before the first registers cannot spawn a
        duplicate ``_run_claude``.  The lock is **not** held across the turn
        itself (the run is dispatched as its own task), so a turn that blocks on a
        bridged menu can never hold the lock — that held-across-run lock was the
        #315 deadlock.
        """
        thread = message.channel
        assert isinstance(thread, discord.Thread)

        # #512: a session the user closed on purpose (/close-workspace) holds
        # incoming messages instead of running them. Checked before the lock and
        # before any prompt building so nothing is spent on a message we will not
        # run. Note this keys on the persisted ``closed_at`` — NOT on "the tmux
        # pane is dead", which is the crash case that must keep auto-resuming
        # via --continue (#270, #464).
        if is_closed(await self.repo.get(thread.id)):
            await self._post_closed_notice(thread, message)
            return

        # #536 AC7: if a menu is open, this sentence is almost certainly the
        # ANSWER to it, not a new order — that is what a user types when the
        # buttons feel unresponsive (yousan sent `y`). Route it into the menu
        # instead of pre-empting the turn and throwing the question away.
        # Checked before the lock: nothing below it is needed for an answer.
        if await self._maybe_answer_open_menu(message, thread):
            return

        lock = self._thread_locks.setdefault(thread.id, asyncio.Lock())
        async with lock:
            record = await self.repo.get(thread.id)
            session_id = (record.session_id or None) if record else None
            prompt, image_paths = await self._build_prompt_and_images(message)
            prompt = await enrich_discord_references(prompt, message, self.bot)

            # Interrupt any in-flight turn for this thread before starting the new
            # one.  We key on ``_active_tasks`` (set synchronously below, under
            # this same lock) rather than ``_active_runners`` (registered later,
            # inside ``_run_claude``): this removes the registration race the lock
            # was added for, without holding the lock across the run — so a turn
            # parked on a bridged menu can no longer wedge the thread (#315).
            prev_task = self._active_tasks.get(thread.id)
            prev_runner = self._active_runners.get(thread.id)
            had_active = prev_task is not None and not prev_task.done()
            if prev_task is not None and had_active:
                await self._preempt_prior_turn(thread, prev_task, prev_runner)
                # #565: the interrupt succeeded — say so, so a thread that
                # goes quiet after this can be told apart from one that
                # never got here.
                logger.info("%s preempted prior turn", log_ctx(thread_id=thread.id))

            # #270: when the tmux pane has died (bot restart / kill -9 / tmux-server
            # death) but a prior session's transcript is still on disk, resume it via
            # --continue instead of starting fresh and discarding the history.
            # Conditions:
            #   - no in-flight turn: an interrupted-but-live session stays alive
            #     in tmux and should just receive send_input, not --continue.
            #   - session_id is not None: a /clear'd thread has its session_id reset,
            #     so it stays fresh — preserving the #123 Part 1 invariant.
            #   - the tmux pane is actually dead (is_claude_running is False).
            # This extends the --continue fallback (previously only on the
            # restart-resume path, #123 Part 2) to the ordinary reply path.
            try_continue = False
            if session_id is not None and not had_active:
                parent_channel_id = getattr(thread, "parent_id", None) or thread.id
                tmux_manager = await self._resolve_tmux_manager(
                    parent_channel_id, thread_id=thread.id
                )
                if tmux_manager is not None:
                    pane_alive = await asyncio.to_thread(tmux_manager.is_claude_running, thread.id)
                    try_continue = not pane_alive

            # #464 ②: the dead pane is about to be auto-resumed via --continue.
            # Announce it first, otherwise the resumed turn re-emits the prior
            # turn's output and reads as the bot replaying garbage / being broken
            # (exactly what the 2026-06-25 tmux-server-death incident looked like
            # to the user). A visible notice makes the recovery legible.
            if try_continue:
                # #512 / #572: same mechanism, three different stories. A user
                # who just pressed 「再開する」 did not suffer a crash, and a
                # workspace c-lord itself put to sleep after 4 idle hours never
                # had a problem to report at all. The wording lives in one
                # function so the three cannot drift apart.
                reopened = thread.id in self._reopened_threads
                self._reopened_threads.discard(thread.id)
                slept = bool(record is not None and record.slept_at)
                notice = resume_notice(slept=slept, reopened=reopened)
                if notice is not None:
                    with contextlib.suppress(discord.HTTPException):
                        await thread.send(notice)
                elif slept:
                    # Invisible to the user, but not to whoever reads the log:
                    # a silent restore must still be greppable, or a sleep that
                    # failed to come back would look identical to one that never
                    # happened (#565).
                    logger.info(
                        "%s silently restoring a slept workspace (#572)",
                        log_ctx(thread_id=thread.id),
                    )

            # A turn is starting, so this workspace is awake — clear the mark
            # where it is consumed rather than trusting a later write to do it.
            if record is not None and record.slept_at:
                with contextlib.suppress(Exception):
                    await self.repo.set_slept(thread.id, False)

            # Run as its own task so the per-thread lock is NOT held for the
            # (possibly long, possibly menu-parked) duration of the turn — that
            # held-across-run lock was the #315 deadlock.  Registering the task
            # here, under the lock, preserves the no-duplicate-run invariant: a
            # second reply that gets the lock sees this task and pre-empts it.
            # #565: this dispatch is the boundary the silent-drop bug hid behind
            # — everything before it had logged, and ``run_claude: enter`` is the
            # next line that does. Log both sides so the gap can never be dark
            # again (`grep "thread=<id>"` shows dispatch → enter as a pair).
            logger.info("%s dispatching run_claude", log_ctx(thread_id=thread.id))
            task = asyncio.create_task(
                self._run_claude(
                    message,
                    thread,
                    prompt,
                    session_id=session_id,
                    image_paths=image_paths,
                    try_continue=try_continue,
                )
            )
            # #565: ``_active_tasks`` keeps a strong reference to this task, so a
            # turn that dies before anyone awaits it is never garbage-collected —
            # and Python only reports "Task exception was never retrieved" at GC
            # time. The exception would therefore be swallowed *forever*. Attach
            # a callback that reports it instead.
            task.add_done_callback(partial(self._report_turn_task_outcome, thread.id))
            self._active_tasks[thread.id] = task

    @staticmethod
    def _report_turn_task_outcome(thread_id: int, task: asyncio.Task) -> None:
        """Surface a turn task that died without anyone awaiting it (#565).

        A turn is dispatched with ``create_task`` and parked in ``_active_tasks``,
        which keeps it referenced for the life of the thread entry. Python only
        emits its "Task exception was never retrieved" warning when an un-awaited
        task is *collected*, so that strong reference means an early crash in
        ``_run_claude`` produces no log line anywhere — exactly the "message
        accepted, then total silence" shape #565 was reported as.

        Cancellation is normal here (that is how :meth:`_preempt_prior_turn`
        tears a turn down), so only a real exception is worth a line.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "%s turn task died before completing: %s",
                log_ctx(thread_id=thread_id),
                exc,
                exc_info=exc,
            )

    async def _maybe_answer_open_menu(
        self, message: discord.Message, thread: discord.Thread
    ) -> bool:
        """Deliver *message*'s text as the open menu's answer (#536 AC7).

        Returns True when the message was consumed as an answer, so the caller
        must NOT run it as a new instruction.

        Decision (recorded in issue #536): the sentence IS the answer. Before
        this, typing while a menu was open silently discarded the question and
        ran the text as a fresh instruction (``⚡ Interrupted``) — so an attempt
        to answer looked, from the user's side, like nothing happening at all.
        A wrong guess costs one button (:class:`TextAnsweredMenuView`); dropping
        the question costs the whole exchange.

        Four cases stay instructions, because as answers they are nonsense:
        an empty body, a message carrying attachments, prose longer than the
        ✏️ Other modal accepts (past that length it is a request, not a pick
        from a three-option menu), and a menu with no free-text row (plan
        approval — typing there would mis-send keystrokes).
        """
        from ..discord_ui.ask_bus import ask_bus
        from ..discord_ui.ask_menus import disable_stale_copies

        text = (message.content or "").strip()
        if not text or message.attachments or len(text) > _MENU_TEXT_ANSWER_MAX:
            return False
        if not ask_bus.accepts_free_text(thread.id):
            return False
        if not ask_bus.post_answer(thread.id, [text]):
            return False

        logger.info(
            "%s message delivered as the open menu's answer (#536 AC7)",
            log_ctx(thread_id=thread.id),
        )
        # The answer came from outside the view, so no interaction edits the
        # menu — blank every live copy here instead.
        with contextlib.suppress(Exception):
            await disable_stale_copies(
                thread.id,
                keep_message_id=None,
                note=f"-# ✏️ 文章で回答しました: {text[:150]}",
            )

        async def _rerun(_interaction: discord.Interaction) -> None:
            # The menu is closed by now, so this takes the ordinary reply path:
            # pre-empt the running turn and start fresh with the same text.
            await self._handle_thread_reply(message)

        view = TextAnsweredMenuView(_rerun, authorizer=self._authorizer)
        with contextlib.suppress(discord.HTTPException):
            await thread.send(
                content=(
                    f"✏️ **この文章を、開いていた質問への回答として送りました:** {text[:150]}\n"
                    "-# 質問への回答ではなく新しい指示のつもりだった場合は、"
                    "下のボタンを押してください。"
                ),
                view=view,
            )
        return True

    async def wake_workspace(self, thread: discord.Thread) -> bool:
        """Restore a stopped workspace without running a turn — #642.

        Public so ``SessionManageCog``'s ``/tmux-screenshot`` can reach it through
        ``bot.get_cog``, the same loose coupling as :meth:`mark_reopened`. It
        lives here rather than in the command because **this** is where a turn's
        spawn is assembled — checkout, tmux window, model, effort, permission
        flags. A second assembly of those in another cog is how the process a
        wake leaves behind stops matching the one the next message expects, and
        the user gets two Claudes on one checkout.

        Whether a workspace *may* be woken is the caller's decision (a `[終了]`
        thread must not be), so this only reports whether it worked.

        Takes the same per-thread setup lock ``on_message`` does. Two
        ``/tmux-screenshot`` clicks (or a message landing mid-wake) would
        otherwise both find no running Claude and each type a ``claude`` command
        line into the same pane — the second one landing inside the first one's
        TUI as a stray turn. Unlike a turn, the whole wake runs under the lock:
        the race is the startup itself, and it is bounded by ``wake``'s timeout.
        """
        parent_channel_id = getattr(thread, "parent_id", None) or thread.id
        tmux_manager = await self._resolve_tmux_manager(parent_channel_id, thread_id=thread.id)
        if tmux_manager is None:
            logger.info("%s wake: no repo binding for this channel", log_ctx(thread_id=thread.id))
            return False

        lock = self._thread_locks.setdefault(thread.id, asyncio.Lock())
        async with lock:
            return await self._wake_locked(thread, tmux_manager, parent_channel_id)

    async def _wake_locked(
        self,
        thread: discord.Thread,
        tmux_manager: TmuxSessionManager,
        parent_channel_id: int,
    ) -> bool:
        """The body of :meth:`wake_workspace`, under the per-thread setup lock."""
        import asyncio as _asyncio

        # Claim the row as "used now" BEFORE anything starts. The 4-hour sweep
        # and #576's LRU cap both select on ``last_used_at``, and a workspace
        # being woken still carries yesterday's value — on staging the sweep
        # killed the window 47 seconds into the wake, and the wake then sat
        # watching a window that no longer existed. Marking it first takes the
        # thread out of both reapers' candidate sets for the whole startup.
        with contextlib.suppress(Exception):
            await self.repo.touch(thread.id)

        session_dir_manager = await self._resolve_session_dir_manager(
            parent_channel_id, thread_id=thread.id
        )
        working_dir = self.runner.working_dir
        if session_dir_manager is not None:
            # Idempotent: the checkout is still there after a sleep (sleep takes
            # the tmux window and nothing else), so this returns the existing
            # path. It re-clones only when the directory really is gone.
            working_dir = await _asyncio.to_thread(
                session_dir_manager.create_session_dir, thread.id, None
            )
        await _asyncio.to_thread(tmux_manager.create_session, thread.id, working_dir or ".")

        model = await self._get_current_model() or self.runner.model
        runner = TmuxClaudeRunner(
            tmux_manager=tmux_manager,
            thread_id=thread.id,
            model=model,
            working_dir=working_dir,
            timeout_seconds=self.runner.timeout_seconds,
            dangerously_skip_permissions=True,
            effort=self.runner.effort,
        )
        if not await runner.wake():
            return False

        # The pane is up, so the sleep mark is no longer true. It only ever
        # words the next resume (#572), which is why it is cleared here and not
        # with the ``touch`` above: until this point "this workspace was slept"
        # was still the honest answer.
        with contextlib.suppress(Exception):
            await self.repo.set_slept(thread.id, False)

        # The restore assigns a new tmux window, and the thread name carries that
        # number as a hint (#95). Left stale it points at a window that no longer
        # exists — the same rename the message path performs.
        try:
            await self._apply_thread_naming(
                thread=thread,
                tmux_manager=tmux_manager,
                first_message="",
                working_dir=working_dir,
            )
        except Exception:
            logger.warning(
                "%s wake: thread naming failed", log_ctx(thread_id=thread.id), exc_info=True
            )

        logger.info("%s workspace woken for capture (#642)", log_ctx(thread_id=thread.id))
        return True

    def mark_reopened(self, thread_id: int) -> None:
        """Note that ``thread_id`` was just reopened from 終了 (#512).

        Consumed once by the next ``--continue`` resume to pick the right wording
        (deliberate reopen vs. crash recovery). Public so ``SessionManageCog``'s
        ``/reopen-workspace`` can reach it through ``bot.get_cog`` — the same
        loose-coupling pattern used for the transcript mirror.
        """
        self._reopened_threads.add(thread_id)

    async def _reopen_thread(self, thread: discord.Thread) -> None:
        """Clear the 終了 state and drop the ``[終了]`` marker from the name (#512)."""
        await self.repo.set_closed(thread.id, False)
        self.mark_reopened(thread.id)
        await apply_open_name(self.repo, thread)
        logger.info("%s session reopened (#512)", log_ctx(thread_id=thread.id))

    async def _post_closed_notice(self, thread: discord.Thread, message: discord.Message) -> None:
        """Tell the user the thread is closed and offer a one-click reopen (#512).

        The button reopens **and then runs the message that was held**, so the
        cost of hitting a closed thread is one click rather than retyping the
        instruction.
        """

        async def _on_reopen(_interaction: discord.Interaction) -> None:
            await self._reopen_thread(thread)
            await self._handle_thread_reply(message)

        view = ReopenSessionView(_on_reopen, authorizer=self._authorizer)
        logger.info("%s message held — session is closed (#512)", log_ctx(thread_id=thread.id))
        with contextlib.suppress(discord.HTTPException):
            await thread.send(embed=closed_notice_embed(), view=view)

    async def _preempt_prior_turn(
        self,
        thread: discord.Thread,
        prev_task: asyncio.Task,
        prev_runner: TmuxClaudeRunner | None,
    ) -> None:
        """Interrupt the in-flight turn so the new message starts fresh (#315).

        This is the documented behaviour (USER_GUIDE → "Interrupting"): a new
        message while Claude is working interrupts the current turn and resumes
        with the new instruction.  A graceful SIGINT breaks the runner's poll
        loop, so a normally-progressing turn winds down on its own.  A turn parked
        on a bridged menu, however, ignores the SIGINT — its ``await`` is on the
        Discord button (``AskView`` is ``timeout=None``), not the poll loop — so
        an empty ask-bus answer dismisses the menu (``bridge_pane_ask`` sends Esc
        and returns) and a task cancel is the final backstop.  Together these
        guarantee a parked menu can never wedge the thread (the #315 deadlock).
        """
        from ..discord_ui.ask_bus import ask_bus

        await thread.send("-# ⚡ Interrupted. Starting with new instruction...")
        if prev_runner is not None:
            with contextlib.suppress(Exception):
                await prev_runner.interrupt(silent=True)
        # No-op unless the turn is parked on a bridged menu; then it unblocks
        # bridge_pane_ask so the run can wind down instead of waiting on a click.
        ask_bus.post_answer(thread.id, [])
        await self._drain_thread_task(prev_task)

    async def _drain_thread_task(self, task: asyncio.Task, *, grace: float = 5.0) -> None:
        """Tear down a prior turn so a new one can start cleanly (#315).

        Waits up to ``grace`` seconds for ``task`` to finish on its own (the
        graceful interrupt may already be winding it down); if it is still alive
        — e.g. parked on a bridged menu whose ``await`` ignores the interrupt —
        it is cancelled.  The cancellation propagates through ``bridge_pane_ask``
        (which unregisters from the ask-bus in its ``finally``) and unwinds
        ``_run_claude``'s ``finally`` (releasing the semaphore slot and clearing
        ``_active_runners``), so a parked turn can never hold the thread hostage.

        #565: the prior turn's outcome must never escape into the caller.  This
        used to reap the task with ``contextlib.suppress(Exception)``, but
        ``asyncio.CancelledError`` derives from ``BaseException``, so awaiting
        the task we had just cancelled raised straight through
        ``_preempt_prior_turn`` into ``_handle_thread_reply`` — aborting it
        *before* the ``create_task(self._run_claude(...))`` that starts the new
        turn.  The user saw "⚡ Interrupted. Starting with new instruction…" and
        then silence, with no ``run_claude: enter`` and no traceback (discord.py
        treats a CancelledError leaving an event handler as plain cancellation).

        ``asyncio.wait`` reports the outcome instead of raising it, and
        ``gather(..., return_exceptions=True)`` reaps the task's exception —
        including its ``CancelledError`` — as a value.  A genuine cancellation of
        *this* coroutine still propagates from both, which is what keeps the
        #315 teardown honest.
        """
        _done, pending = await asyncio.wait({task}, timeout=grace)
        if pending:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _build_prompt(self, message: discord.Message) -> str:
        """Build the prompt string (text only). Use _build_prompt_and_images for full processing."""
        prompt, _ = await self._build_prompt_and_images(message)
        return prompt

    async def _build_prompt_and_images(self, message: discord.Message) -> tuple[str, list[str]]:
        """Build the prompt string from the message text.

        Attachments are **not** part of this any more (#528): they are written
        to disk next to the checkout by :meth:`_stage_attachments`, which runs
        later — once the session directory is known — and appends their paths.
        Pasting file contents in here is what made a 20KB ``.md`` exceed tmux's
        input cap (#527) and what forced the 50KB drop that ate files silently.

        Returns:
            (prompt_text, []) — the second element is kept for API compatibility.
        """
        return message.content or "", []

    async def _stage_attachments(
        self,
        message: discord.Message,
        work_dir: str | None,
        thread: discord.abc.Messageable,
    ) -> str:
        """Save the message's attachments to disk; return the prompt sections.

        Every attachment becomes a real file under ``work_dir`` and the prompt
        gets its **path**, so "I attached a file" means Claude can open a file
        — the thing the user assumed was happening all along.

        Anything that cannot be handed over is named in the thread with the
        reason. The old code dropped over-cap files with a ``logger.debug`` and
        nothing else, which is how "添付したのに見つからないと言われる" happened:
        Claude was never told a file existed, and neither was the user.
        """
        attachments = list(message.attachments)
        if not attachments:
            return ""

        skipped: list[str] = []
        sections: list[str] = []

        if not work_dir or not Path(work_dir).is_dir():
            # No checkout to write into (unbound channel). Say so rather than
            # letting the files evaporate.
            skipped = [f"`{a.filename}` — 保存先のワークスペースがありません" for a in attachments]
            await self._report_skipped_attachments(thread, skipped)
            return ""

        ensure_git_excluded(work_dir)

        for attachment in attachments[:_MAX_ATTACHMENTS]:
            if attachment.size > _MAX_ATTACHMENT_BYTES:
                skipped.append(
                    f"`{attachment.filename}` — {_human_size(attachment.size)} は上限 "
                    f"{_human_size(_MAX_ATTACHMENT_BYTES)} を超えています"
                )
                continue
            try:
                data = await attachment.read()
            except Exception as exc:  # noqa: BLE001 — any download failure is reportable
                logger.warning(
                    "%s failed to download attachment %s: %s",
                    log_ctx(thread_id=getattr(thread, "id", None)),
                    attachment.filename,
                    exc,
                )
                skipped.append(f"`{attachment.filename}` — ダウンロードに失敗しました")
                continue
            try:
                path = await asyncio.to_thread(
                    save_attachment, work_dir, message.id, attachment.filename, data
                )
            except OSError as exc:
                logger.warning(
                    "%s failed to save attachment %s: %s",
                    log_ctx(thread_id=getattr(thread, "id", None)),
                    attachment.filename,
                    exc,
                )
                skipped.append(f"`{attachment.filename}` — 保存に失敗しました ({exc.strerror})")
                continue

            sections.append(
                f"\n\n--- Attached file: {attachment.filename} "
                f"({_human_size(len(data))}) ---\n"
                f"Saved to: {path}\n"
                f"{_ATTACHMENT_PATH_NOTE}"
            )
            logger.info(
                "%s saved attachment %s -> %s",
                log_ctx(thread_id=getattr(thread, "id", None)),
                attachment.filename,
                path,
            )

        for extra in attachments[_MAX_ATTACHMENTS:]:
            skipped.append(f"`{extra.filename}` — 1メッセージあたり {_MAX_ATTACHMENTS} 個までです")

        await self._report_skipped_attachments(thread, skipped)
        return "".join(sections)

    @staticmethod
    async def _report_skipped_attachments(
        thread: discord.abc.Messageable, skipped: list[str]
    ) -> None:
        """Tell the user, in the thread, which attachments never reached Claude."""
        if not skipped:
            return
        body = "\n".join(f"- {line}" for line in skipped)
        with contextlib.suppress(discord.HTTPException):
            await thread.send(f"⚠️ 次の添付は Claude に渡せませんでした:\n{body}")

    async def _run_claude(
        self,
        user_message: discord.Message,
        thread: discord.Thread,
        prompt: str,
        session_id: str | None,
        image_paths: list[str] | None = None,
        try_continue: bool = False,
        requester: discord.Member | discord.User | None = None,
    ) -> None:
        """Execute Claude Code CLI and stream results to the thread.

        ``requester`` (#520) names the person who asked for this turn when the
        trigger message is not theirs — ``/clord`` seeds the thread with a
        message c-lord itself posts, so ``user_message.author`` is the bot.
        Defaults to the trigger message's author, as before.
        """
        # #520: resolve who asked for this turn — the invoker for /clord, the
        # trigger message's author otherwise, and nobody when the trigger is
        # c-lord's own seed message. That answers both "who to credit" (#519)
        # and, once bots are filtered out, "who to ping" (#480/#481).
        requester = _requester_of_turn(user_message, requester, self.bot)
        # #525: the two mentions answer different questions — "your turn is
        # parked, come and decide" vs "your turn is done" — so a deployment can
        # keep the first and drop the second for turns nobody human asked for.
        notify_user_id = _notify_target(requester, self.bot, kind="blocked")
        completion_notify_id = _notify_target(requester, self.bot, kind="completion")

        # Unbound channel check: verify /clord-init or /clord-thread-init binding
        parent_channel_id = getattr(thread, "parent_id", None) or thread.id
        session_dir_manager = await self._resolve_session_dir_manager(
            parent_channel_id, thread_id=thread.id
        )
        tmux_manager = await self._resolve_tmux_manager(parent_channel_id, thread_id=thread.id)

        # #565: both resolvers are awaits that sit between the dispatch and
        # the first ``run_claude: enter``. If one of them ever hangs, this
        # line is the last one logged.
        logger.info("%s resolved managers for turn", log_ctx(thread_id=thread.id))
        if session_dir_manager is None and tmux_manager is None:
            await thread.send(
                "⚠️ このチャンネルにはリポジトリが紐づけられていません。\n"
                "先に `/clord-init repo:<URL> branch:<branch>` で設定してください。"
            )
            return

        if self._semaphore.locked():
            # #632: a courtesy notice — never a reason to drop the turn.
            with contextlib.suppress(Exception):
                await thread.send(
                    f"\u23f3 Waiting for a free session slot... "
                    f"({self._max_concurrent} max sessions running)"
                )

        async with self._semaphore:
            # #565: distinguishes "waiting for a slot" from "never got here".
            logger.info("%s acquired session slot", log_ctx(thread_id=thread.id))
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

            # Mark thread as PROCESSING when Claude starts. #632: through
            # _safe_set_state — a failed embed refresh is decoration failing,
            # and it must never be the reason a turn never happened.
            if dashboard is not None:
                await _safe_set_state(
                    dashboard,
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
                # #518: hand the turn's requester down so the session dir's
                # commit hook can credit them as a Co-authored-by. #520: that is
                # the requester, not the trigger message's author — a /clord
                # seed message is authored by the bot itself.
                session_dir = await _asyncio.to_thread(
                    session_dir_manager.create_session_dir,
                    thread.id,
                    requester,
                )
                working_dir = session_dir
                logger.info("Session dir for thread %d: %s", thread.id, session_dir)

            # #528: attachments become real files in the checkout, and the
            # prompt gets their paths. Done here rather than while building the
            # prompt because only now do we know where the checkout is.
            prompt += await self._stage_attachments(user_message, working_dir, thread)

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
                try:
                    await self._apply_thread_naming(
                        thread=thread,
                        tmux_manager=tmux_manager,
                        first_message=prompt,
                        working_dir=working_dir,
                    )
                except Exception:
                    # #423: naming is best-effort and must not break the run, but a
                    # failure before the rename (e.g. window lookup) was invisible.
                    # Log it (with stacktrace) instead of swallowing silently.
                    logger.warning(
                        "%s thread naming failed", log_ctx(thread_id=thread.id), exc_info=True
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
                effort=self.runner.effort,
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

            stop_view = StopView(runner, authorizer=self._authorizer)
            # #632: the Stop-button notice is decoration too. If Discord refuses
            # it (rate limit, revoked permission, closed session) the turn must
            # still run — StopView already treats a missing message as "nothing
            # to delete", so the only thing lost is the button.
            notice = (
                f"{STOP_MESSAGE_PREFIX} (`{window_name}`)" if window_name else STOP_MESSAGE_PREFIX
            )
            try:
                stop_view.set_message(await thread.send(notice, view=stop_view))
            except Exception:
                logger.warning(
                    "%s could not post the Stop-button notice; continuing the turn",
                    log_ctx(thread_id=thread.id),
                    exc_info=True,
                )

            # #562: kept in a variable so the turn-end ping below can read the
            # run's outcome — a turn that produced nothing must not be announced
            # as finished.
            run_config = RunConfig(
                thread=thread,
                runner=runner,
                repo=self.repo,
                prompt=prompt,
                session_id=session_id,
                status=status,
                registry=self._registry,
                ask_repo=self._ask_repo,
                lounge_repo=self._lounge_repo,
                settings_repo=self._settings_repo,
                stop_view=stop_view,
                session_dir_manager=session_dir_manager,
                tmux_manager=tmux_manager,
                image_paths=image_paths,
                working_dir=working_dir or None,
                authorizer=self._authorizer,
                # #480: ping the requester of THIS turn when an
                # interactive prompt (permission/plan/elicitation/ask)
                # blocks it — a mid-turn pause never reaches the
                # turn-end mention.
                notify_user_id=notify_user_id,
            )
            try:
                await run_claude_with_config(run_config)
            finally:
                # Issue #91: wrap every Discord call so that RuntimeError("Session is
                # closed") during bot shutdown doesn't propagate and turn the bot into
                # a zombie.  The individual helpers already log errors internally.
                with contextlib.suppress(Exception):
                    await stop_view.disable()
                # Conditional pop: runs are no longer serialised by a lock held
                # across the whole turn (#315), so a newer reply may have already
                # replaced these entries with its own runner/task — only clear our
                # own, never a successor's.
                if self._active_runners.get(thread.id) is runner:
                    self._active_runners.pop(thread.id, None)
                if self._active_tasks.get(thread.id) is current_task:
                    self._active_tasks.pop(thread.id, None)

                with contextlib.suppress(Exception):
                    await coordination.post_session_end(thread)

                # #246: no per-turn thread rename here. The turn-end 🟡 lamp is
                # the StatusManager reaction (set via set_done() on the RESULT
                # event); the thread-name lamp flips to 🟡 on the next state-sync
                # poll (is_processing() is already False after the pop above).

                if dashboard is not None:
                    # #632: same guard as the PROCESSING update above, but this
                    # one logs instead of suppressing silently.
                    await _safe_set_state(
                        dashboard,
                        thread.id,
                        ThreadState.WAITING_INPUT,
                        description,
                        thread=thread,
                        # #481: ping the requester of THIS turn, not a
                        # fixed owner — so completion reaches whoever is
                        # waiting (any guild / any authorized user, owner or
                        # not). #520: bot-seeded turns fall back to owner,
                        # #525: and only when the deployment asked for it.
                        notify_user_id=completion_notify_id,
                        # #562: say what happened. "終わりました" is a summons;
                        # when nothing was produced it is a lie, and a
                        # notification that lies stops being worth reading.
                        no_response=run_config.outcome.no_response,
                    )
