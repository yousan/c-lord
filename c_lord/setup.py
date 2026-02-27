"""One-call setup for all c-lord bridge Cogs.

Consumers call this instead of manually wiring each Cog.
New Cogs added to c-lord are automatically included — no consumer code changes needed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .claude.runner import ClaudeRunner
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

        components = await setup_bridge(bot, runner, api_server=api_server)

    Or manually if you need more control::

        components = await setup_bridge(bot, runner)
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
    runner: ClaudeRunner,
    *,
    api_server: ApiServer | None = None,
    session_db_path: str = "data/sessions.db",
    allowed_user_ids: set[int] | None = None,
    claude_channel_id: int | None = None,
    cli_sessions_path: str | None = None,
    enable_scheduler: bool = True,
    task_db_path: str = "data/tasks.db",
    lounge_channel_id: int | None = None,
    session_dir_base: str | None = None,
    session_source_repo: str | None = None,
    session_clone_branch: str | None = None,
    enable_tmux: bool = True,
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
        runner: ClaudeRunner for Claude CLI invocation.
        api_server: Optional ApiServer to auto-wire repos into.  Also sets
                    runner.api_port so CLORD_API_URL is available to Claude.
        session_db_path: Path for session SQLite DB.
        allowed_user_ids: Set of Discord user IDs allowed to use Claude.
        claude_channel_id: Channel ID for Claude chat (needed for SkillCommandCog).
        cli_sessions_path: Path to ~/.claude/projects for session sync.
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
        session_clone_branch: Optional branch to clone. Defaults to
                              SESSION_CLONE_BRANCH env var, or None (default branch).
        enable_tmux: Whether to enable tmux session management. Enabled by
                     default (degrades gracefully if tmux is not installed).
                     Set CLORD_TMUX_ENABLED to "false"/"0"/"no" to disable.

    Returns:
        BridgeComponents with references to initialized repositories.
    """
    from .cogs.channel_repo import ChannelRepoCog
    from .cogs.claude_chat import ClaudeChatCog
    from .cogs.scheduler import SchedulerCog
    from .cogs.session_manage import SessionManageCog
    from .cogs.skill_command import SkillCommandCog
    from .database.ask_repo import PendingAskRepository
    from .database.channel_repo import ChannelRepository
    from .database.lounge_repo import LoungeRepository
    from .database.models import init_db
    from .database.repository import SessionRepository
    from .database.resume_repo import PendingResumeRepository
    from .database.settings_repo import SettingsRepository
    from .database.task_repo import TaskRepository
    from .session_dir import SessionDirManager
    from .tmux import TmuxSessionManager

    # Lounge shares the coordination channel unless explicitly overridden
    if lounge_channel_id is None:
        ch_str = os.getenv("COORDINATION_CHANNEL_ID", "")
        lounge_channel_id = int(ch_str) if ch_str.isdigit() else None

    # SessionDirManager — attach to bot so cogs can access it via bot.session_dir_manager
    if session_dir_base is None:
        session_dir_base = os.getenv("SESSION_DIR_BASE")
    if session_source_repo is None:
        session_source_repo = os.getenv("SESSION_SOURCE_REPO")
    if session_clone_branch is None:
        session_clone_branch = os.getenv("SESSION_CLONE_BRANCH") or None
    if session_dir_base is not None and session_source_repo is not None:
        if not hasattr(bot, "session_dir_manager"):
            bot.session_dir_manager = SessionDirManager(  # type: ignore[attr-defined]
                base_dir=session_dir_base,
                source_repo=session_source_repo,
                clone_branch=session_clone_branch,
            )
        logger.info(
            "SessionDirManager enabled (base=%s, repo=%s)", session_dir_base, session_source_repo
        )

    # TmuxSessionManager — attach to bot so cogs can access it via bot.tmux_manager
    # Enabled by default; set CLORD_TMUX_ENABLED=false/0/no to disable.
    tmux_env = os.getenv("CLORD_TMUX_ENABLED", "").lower()
    if tmux_env in ("false", "0", "no"):
        enable_tmux = False
    if enable_tmux:
        session_name = os.getenv("CLORD_TMUX_SESSION_NAME") or Path.cwd().name
        if not hasattr(bot, "tmux_manager"):
            bot.tmux_manager = TmuxSessionManager(  # type: ignore[attr-defined]
                session_name=session_name,
            )
        logger.info("TmuxSessionManager enabled (session=%s)", session_name)

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
        allowed_user_ids=allowed_user_ids,
        ask_repo=ask_repo,
        lounge_repo=lounge_repo,
        resume_repo=resume_repo,
        settings_repo=settings_repo,
    )
    await bot.add_cog(chat_cog)
    logger.info("Registered ClaudeChatCog")

    # --- SessionManageCog ---
    session_manage_cog = SessionManageCog(
        bot,  # type: ignore[arg-type]  # consumers pass their own Bot subclass
        repo=session_repo,
        cli_sessions_path=cli_sessions_path,
        settings_repo=settings_repo,
    )
    await bot.add_cog(session_manage_cog)
    logger.info("Registered SessionManageCog")

    # --- ChannelRepoCog (per-channel repo bindings, zero-config) ---
    channel_repo = ChannelRepository(session_db_path)
    await channel_repo.init_db()
    channel_repo_cog = ChannelRepoCog(
        bot,
        repo=channel_repo,
        allowed_user_ids=allowed_user_ids,
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
        )
        await bot.add_cog(skill_cog)
        logger.info("Registered SkillCommandCog")

    # --- SchedulerCog (optional) ---
    task_repo: TaskRepository | None = None
    if enable_scheduler:
        os.makedirs(os.path.dirname(task_db_path) or ".", exist_ok=True)
        task_repo = TaskRepository(task_db_path)
        await task_repo.init_db()
        scheduler_cog = SchedulerCog(bot, runner, repo=task_repo)
        await bot.add_cog(scheduler_cog)
        logger.info("Registered SchedulerCog")

    components = BridgeComponents(
        session_repo=session_repo,
        task_repo=task_repo,
        lounge_repo=lounge_repo,
        resume_repo=resume_repo,
    )

    # Auto-wire repos to ApiServer and set runner.api_port if provided
    if api_server is not None:
        components.apply_to_api_server(api_server)
        if runner.api_port is None:
            runner.api_port = api_server.port
        logger.info("Auto-wired repos to ApiServer (port=%d)", api_server.port)

    return components
