"""One-call setup for all c-lord bridge Cogs.

Consumers call this instead of manually wiring each Cog.
New Cogs added to c-lord are automatically included — no consumer code changes needed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .claude.config import ClaudeConfig
    from .database.lounge_repo import LoungeRepository
    from .database.repository import SessionRepository
    from .database.resume_repo import PendingResumeRepository
    from .database.task_repo import TaskRepository
    from .ext.api_server import ApiServer

logger = logging.getLogger(__name__)


@dataclass
class BridgeComponents:
    """References to initialized bridge components.

    After calling setup_bridge(), pass this to apply_to_api_server() so the
    ApiServer gains access to all repos without manual wiring::

        components = await setup_bridge(bot, config, api_server=api_server)

    Or manually if you need more control::

        components = await setup_bridge(bot, config)
        components.apply_to_api_server(api_server)
    """

    session_repo: SessionRepository
    task_repo: TaskRepository | None = None
    lounge_repo: LoungeRepository | None = None
    resume_repo: PendingResumeRepository | None = None

    def apply_to_api_server(self, api_server: ApiServer) -> None:
        """Wire all optional repos to an ApiServer instance.

        Idempotent — safe to call multiple times.  Only non-None repos are
        applied, so repos that are disabled (e.g. scheduler off) are left as-is.

        When a new repo is added to BridgeComponents in the future, add it here
        and consumers automatically pick it up without changing their own code.
        """
        if self.task_repo is not None:
            api_server.task_repo = self.task_repo
        if self.lounge_repo is not None:
            api_server.lounge_repo = self.lounge_repo
        if self.resume_repo is not None:
            api_server.resume_repo = self.resume_repo
        api_server.session_repo = self.session_repo


async def setup_bridge(
    bot: Bot,
    runner: ClaudeConfig,
    *,
    api_server: ApiServer | None = None,
    session_db_path: str = "data/sessions.db",
    allowed_user_ids: set[int] | None = None,
    allowed_role_name: str | None = None,
    claude_channel_id: int | None = None,
    enable_scheduler: bool = True,
    task_db_path: str = "data/tasks.db",
    lounge_channel_id: int | None = None,
    session_dir_base: str | None = None,
    session_source_repo: str | None = None,
    enable_tmux: bool = True,
    max_concurrent: int = 3,
) -> BridgeComponents:
    """Initialize and register all c-lord Cogs in one call.

    This is the recommended way for consumers to set up c-lord.
    New Cogs added to c-lord will be automatically included.

    Pass ``api_server`` to automatically wire all repos and set the runner's
    ``api_port`` — consumers then need zero manual wiring::

        components = await setup_bridge(bot, runner, api_server=api_server, ...)
        # Done — no manual repo wiring needed.

    Args:
        bot: Discord bot instance.
        runner: ClaudeConfig with CLI invocation settings.
        api_server: Optional ApiServer to auto-wire repos into.  Also sets
                    runner.api_port so CLORD_API_URL is available to Claude.
        session_db_path: Path for session SQLite DB.
        allowed_user_ids: Set of Discord user IDs allowed to use Claude.
        allowed_role_name: Discord role name whose members are allowed to use Claude.
                           OR logic with allowed_user_ids.  Defaults to
                           CLORD_ALLOWED_ROLE env var, or None (disabled).
        claude_channel_id: Channel ID for Claude chat (needed for SkillCommandCog).
        enable_scheduler: Whether to enable SchedulerCog.
        task_db_path: Path for scheduled tasks SQLite DB.
        lounge_channel_id: Discord channel ID for AI Lounge messages.
                           Defaults to COORDINATION_CHANNEL_ID env var so
                           lounge and coordination share the same channel
                           with no extra configuration needed.
        session_dir_base: Base directory for session clone directories.
                          When set with ``session_source_repo``, a
                          SessionDirManager is created and attached to the bot.
                          Defaults to SESSION_DIR_BASE env var, or None (disabled).
        session_source_repo: Git repository URL or local path to clone for
                             each session. Required when session_dir_base is set.
                             Defaults to SESSION_SOURCE_REPO env var.
        enable_tmux: Whether to enable tmux session management. Enabled by
                     default (degrades gracefully if tmux is not installed).
                     Set CLORD_TMUX_ENABLED to "false"/"0"/"no" to disable.
        max_concurrent: Maximum number of Claude sessions processed at once
                        (the semaphore limit behind the "Waiting for a free
                        session slot" message). Defaults to 3.

    Returns:
        BridgeComponents with references to initialized repositories.
    """
    from .cogs.channel_repo import ChannelRepoCog
    from .cogs.claude_chat import ClaudeChatCog
    from .cogs.scheduler import SchedulerCog
    from .cogs.session_cleanup import SessionCleanupCog
    from .cogs.session_manage import SessionManageCog
    from .cogs.skill_command import SkillCommandCog
    from .cogs.transcript_mirror import TranscriptMirrorCog
    from .cogs.version_cmd import VersionCog
    from .database.ask_repo import PendingAskRepository
    from .database.channel_repo import ChannelRepository
    from .database.lounge_repo import LoungeRepository
    from .database.models import init_db
    from .database.repository import SessionRepository
    from .database.resume_repo import PendingResumeRepository
    from .database.settings_repo import SettingsRepository
    from .database.task_repo import TaskRepository
    from .database.thread_repo import ThreadRepository

    # Role-based access control — auto-read from env var if not explicitly provided
    if allowed_role_name is None:
        allowed_role_name = os.getenv("CLORD_ALLOWED_ROLE") or None

    # Lounge shares the coordination channel unless explicitly overridden
    if lounge_channel_id is None:
        ch_str = os.getenv("COORDINATION_CHANNEL_ID", "")
        lounge_channel_id = int(ch_str) if ch_str.isdigit() else None

    # Global SessionDirManager and TmuxSessionManager are no longer created here.
    # Per-channel managers are resolved dynamically via ChannelRepoCog.
    # The session_dir_base parameter is still accepted and forwarded to
    # ChannelRepoCog so per-channel directories share the same base path.
    if session_dir_base is None:
        session_dir_base = os.getenv("SESSION_DIR_BASE")
    if session_source_repo is not None or os.getenv("SESSION_SOURCE_REPO"):
        import warnings

        warnings.warn(
            "session_source_repo / SESSION_SOURCE_REPO is deprecated. "
            "Use /clord-init to bind channels to repositories.",
            DeprecationWarning,
            stacklevel=2,
        )

    # --- Session DB (also hosts lounge_messages and pending_resumes tables) ---
    os.makedirs(os.path.dirname(session_db_path) or ".", exist_ok=True)
    await init_db(session_db_path)
    session_repo = SessionRepository(session_db_path)
    settings_repo = SettingsRepository(session_db_path)
    ask_repo = PendingAskRepository(session_db_path)
    lounge_repo = LoungeRepository(session_db_path)
    resume_repo = PendingResumeRepository(session_db_path)
    logger.info("Session DB initialized: %s", session_db_path)

    # Attach repos to bot so generic cogs (e.g. AutoUpgradeCog) can discover them
    # without a hard import dependency on c-lord internals.
    bot.session_repo = session_repo  # type: ignore[attr-defined]
    bot.resume_repo = resume_repo  # type: ignore[attr-defined]
    bot.ask_repo = ask_repo  # type: ignore[attr-defined]
    bot.lounge_repo = lounge_repo  # type: ignore[attr-defined]
    bot.lounge_channel_id = lounge_channel_id  # type: ignore[attr-defined]

    # --- ClaudeChatCog ---
    chat_cog = ClaudeChatCog(
        bot,  # type: ignore[arg-type]  # consumers pass their own Bot subclass
        repo=session_repo,
        runner=runner,
        max_concurrent=max_concurrent,
        allowed_user_ids=allowed_user_ids,
        allowed_role_name=allowed_role_name,
        ask_repo=ask_repo,
        lounge_repo=lounge_repo,
        resume_repo=resume_repo,
        settings_repo=settings_repo,
    )
    await bot.add_cog(chat_cog)
    logger.info("Registered ClaudeChatCog (max_concurrent=%d)", max_concurrent)

    # --- SessionManageCog ---
    session_manage_cog = SessionManageCog(
        bot,  # type: ignore[arg-type]  # consumers pass their own Bot subclass
        repo=session_repo,
        settings_repo=settings_repo,
    )
    await bot.add_cog(session_manage_cog)
    logger.info("Registered SessionManageCog")

    # --- ChannelRepoCog (per-channel and per-thread repo bindings, zero-config) ---
    channel_repo = ChannelRepository(session_db_path)
    await channel_repo.init_db()
    thread_repo = ThreadRepository(session_db_path)
    await thread_repo.init_db()
    channel_repo_cog = ChannelRepoCog(
        bot,
        repo=channel_repo,
        thread_repo=thread_repo,
        allowed_user_ids=allowed_user_ids,
        allowed_role_name=allowed_role_name,
        session_dir_base=session_dir_base,
    )
    await bot.add_cog(channel_repo_cog)
    logger.info("Registered ChannelRepoCog")

    # --- SkillCommandCog (requires channel ID) ---
    if claude_channel_id is not None:
        skill_cog = SkillCommandCog(
            bot,
            repo=session_repo,
            runner=runner,
            claude_channel_id=claude_channel_id,
            allowed_user_ids=allowed_user_ids,
            allowed_role_name=allowed_role_name,
        )
        await bot.add_cog(skill_cog)
        logger.info("Registered SkillCommandCog")

    # --- SessionCleanupCog (#554) ---
    # Announce-only: it posts the 「記録を整理しました」 notice for rows the 30-day
    # sweep deleted, and never deletes anything itself. Registering it
    # unconditionally is therefore safe for consumers who do not run the sweep —
    # with no rows handed to it, it does nothing.
    session_cleanup_cog = SessionCleanupCog(bot)
    await bot.add_cog(session_cleanup_cog)
    bot.session_cleanup_cog = session_cleanup_cog  # type: ignore[attr-defined]
    logger.info("Registered SessionCleanupCog")

    # --- TranscriptMirrorCog (Issue #71, gated by CLORD_BRIDGE_MODE=jsonl) ---
    transcript_cog = TranscriptMirrorCog(bot, session_repo=session_repo)
    await bot.add_cog(transcript_cog)
    bot.transcript_mirror_cog = transcript_cog  # type: ignore[attr-defined]
    logger.info("Registered TranscriptMirrorCog (active when CLORD_BRIDGE_MODE=jsonl)")

    # --- VersionCog (read-only /version + !version twin, zero-config) ---
    await bot.add_cog(VersionCog(bot))
    logger.info("Registered VersionCog")

    # --- SchedulerCog (optional) ---
    task_repo: TaskRepository | None = None
    if enable_scheduler:
        os.makedirs(os.path.dirname(task_db_path) or ".", exist_ok=True)
        task_repo = TaskRepository(task_db_path)
        await task_repo.init_db()
        scheduler_cog = SchedulerCog(bot, runner, repo=task_repo)
        await bot.add_cog(scheduler_cog)
        logger.info("Registered SchedulerCog")

    # --- Thread state sync loop (Issue #95) ---
    # Refreshes the leading status emoji + trailing #window-index hint on each
    # Discord thread once per minute.  Topic body is never touched.
    #
    # The thread-name lamp (🟢🟡) is off by default (#329) — repainting the name
    # on every state change saturates Discord's thread-rename rate-limit. When
    # the lamp is off there is nothing for this poll to paint, so skip the loop
    # entirely. Opt in with CLORD_THREAD_LAMP=1 (CLORD_THREAD_STATE_SYNC=0 then
    # keeps the event-driven lamp but drops the ≤60s poll).
    from .thread_name import thread_lamp_enabled

    _lamp_on = thread_lamp_enabled()
    _sync_on = os.getenv("CLORD_THREAD_STATE_SYNC", "1") not in ("0", "false", "no")
    if _lamp_on and _sync_on:
        from .thread_state_sync import ThreadStateSyncLoop

        interval_env = os.getenv("CLORD_THREAD_STATE_SYNC_INTERVAL", "60")
        try:
            interval = float(interval_env)
        except ValueError:
            interval = 60.0
        sync_loop = ThreadStateSyncLoop(
            bot,
            session_repo,
            interval_seconds=interval,
            # Let the poll keep an in-flight thread 🟢 instead of rolling it back
            # to 🟡 during a brief no-spinner window (#236).
            is_processing=chat_cog.is_processing,
        )
        sync_loop.start()
        bot.thread_state_sync = sync_loop  # type: ignore[attr-defined]
        logger.info("Started ThreadStateSyncLoop (interval=%.0fs)", interval)
    elif not _lamp_on:
        logger.info(
            "ThreadStateSyncLoop disabled: thread lamp is off "
            "(set CLORD_THREAD_LAMP=1 to enable 🟢🟡 thread-name lamps)"
        )

    # --- Idle stop (#574) ---
    # Always-on (zero-config): stops workspaces nobody has touched for
    # CLORD_IDLE_STOP_DAYS (default 7). The threshold is a constant rather than
    # something derived from the host because it describes how people use
    # Discord, not how much RAM the machine has — see docs/specs/
    # workspace-vocabulary.md. Set the env to 0 to disable.
    from .idle_stop import IdleStopLoop, idle_stop_days

    _idle_days = idle_stop_days()
    if _idle_days > 0:
        idle_loop = IdleStopLoop(bot, session_repo, threshold_days=_idle_days)
        idle_loop.start()
        bot.idle_stop_loop = idle_loop  # type: ignore[attr-defined]
    else:
        logger.info("Idle-stop disabled (CLORD_IDLE_STOP_DAYS=0)")

    # --- Menu watchdog (#359) ---
    # Always-on (zero-config): bridges TUI AskUserQuestion/plan menus that no
    # turn is watching (turn finalized early; mirror cannot read the tool_use
    # until resolution). Independent of the thread-name lamp above — the user's
    # ability to answer menus must not depend on an opt-in cosmetic. Opt out
    # with CLORD_MENU_WATCHDOG=0.
    if os.getenv("CLORD_MENU_WATCHDOG", "1") not in ("0", "false", "no"):
        from .thread_state_sync import MenuWatchdogLoop

        # repo wired so the sweep only bridges windows THIS bot owns on a shared
        # tmux server — never another bot's menu (#438).
        menu_watchdog = MenuWatchdogLoop(
            bot, is_processing=chat_cog.is_processing, repo=session_repo
        )
        menu_watchdog.start()
        bot.menu_watchdog = menu_watchdog  # type: ignore[attr-defined]

    components = BridgeComponents(
        session_repo=session_repo,
        task_repo=task_repo,
        lounge_repo=lounge_repo,
        resume_repo=resume_repo,
    )

    # Auto-wire repos to ApiServer if provided
    if api_server is not None:
        components.apply_to_api_server(api_server)
        logger.info("Auto-wired repos to ApiServer (port=%d)", api_server.port)

    return components
