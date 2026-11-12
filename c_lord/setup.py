"""One-call setup for all c-lord bridge Cogs.

Call :func:`setup_bridge` once and every Cog is built, given its storage and added
to the bot — an instance repo never assembles them by hand.
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
    from .database.ask_repo import PendingAskRepository
    from .database.lounge_repo import LoungeRepository
    from .database.repository import SessionRepository
    from .database.resume_repo import PendingResumeRepository
    from .database.settings_repo import SettingsRepository
    from .database.task_repo import TaskRepository
    from .ext.api_server import ApiServer

logger = logging.getLogger(__name__)

#: Repositories an ApiServer only receives when setup actually built them.
_OPTIONAL_API_REPOS = ("task_repo", "lounge_repo", "resume_repo")


@dataclass
class BridgeComponents:
    """Handles to what :func:`setup_bridge` initialized.

    Give ``setup_bridge`` an ``api_server=`` and the repositories reach it on their own::

        components = await setup_bridge(bot, config, api_server=api_server)

    To choose the moment yourself, leave it out and wire the result by hand::

        components = await setup_bridge(bot, config)
        components.apply_to_api_server(api_server)
    """

    session_repo: SessionRepository
    task_repo: TaskRepository | None = None
    lounge_repo: LoungeRepository | None = None
    resume_repo: PendingResumeRepository | None = None
    #: Where per-thread session dirs are cloned (``<base>/<channel>/<thread>``),
    #: as resolved by setup — the 30-day sweep deletes only under it (#902).
    session_dir_base: str | None = None

    def apply_to_api_server(self, api_server: ApiServer) -> None:
        """Point *api_server* at the repositories held here. Safe to repeat.

        A repository this setup did not build (scheduler switched off, say) is
        skipped, so whatever the server already had in that slot stays.
        """
        for slot in _OPTIONAL_API_REPOS:
            built = getattr(self, slot)
            if built is not None:
                setattr(api_server, slot, built)
        api_server.session_repo = self.session_repo
        api_server.session_dir_base = self.session_dir_base


def _ensure_parent_dir(db_path: str) -> None:
    """Make sure the directory of a SQLite file exists (a bare filename means the cwd)."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)


@dataclass(frozen=True)
class _SessionStores:
    """The repositories that share the session database file."""

    sessions: SessionRepository
    settings: SettingsRepository
    asks: PendingAskRepository
    lounge: LoungeRepository
    resumes: PendingResumeRepository


async def _open_session_stores(db_path: str) -> _SessionStores:
    """Create the session schema, then open each repository that lives in that file."""
    from .database.ask_repo import PendingAskRepository
    from .database.lounge_repo import LoungeRepository
    from .database.models import init_db
    from .database.repository import SessionRepository
    from .database.resume_repo import PendingResumeRepository
    from .database.settings_repo import SettingsRepository

    _ensure_parent_dir(db_path)
    await init_db(db_path)
    stores = _SessionStores(
        sessions=SessionRepository(db_path),
        settings=SettingsRepository(db_path),
        asks=PendingAskRepository(db_path),
        lounge=LoungeRepository(db_path),
        resumes=PendingResumeRepository(db_path),
    )
    logger.info("Session DB initialized: %s", db_path)
    return stores


async def _start_scheduler(bot: Bot, runner: ClaudeConfig, task_db_path: str) -> TaskRepository:
    """Open the task database and register the Cog that runs what is due in it."""
    from .cogs.scheduler import SchedulerCog
    from .database.task_repo import TaskRepository

    _ensure_parent_dir(task_db_path)
    task_repo = TaskRepository(task_db_path)
    await task_repo.init_db()
    await bot.add_cog(SchedulerCog(bot, runner, repo=task_repo))
    logger.info("Registered SchedulerCog")
    return task_repo


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

    Hand over ``api_server`` and the repositories built here are attached to it
    as well, which leaves the caller nothing to connect afterwards::

        components = await setup_bridge(bot, config, api_server=api_server)

    Args:
        bot: The Discord bot the Cogs are added to.
        runner: ClaudeConfig with CLI invocation settings.
        api_server: ApiServer that should receive the repositories, if any. Also sets
                    runner.api_port so CLORD_API_URL is available to Claude.
        session_db_path: Where the session SQLite file lives.
        allowed_user_ids: Discord user IDs that may talk to Claude.
        allowed_role_name: Discord role name whose members are allowed to use Claude.
                           OR logic with allowed_user_ids.  Defaults to
                           CLORD_ALLOWED_ROLE env var, or None (disabled).
        claude_channel_id: The Claude chat channel. SkillCommandCog is only
                           registered when this is given.
        enable_scheduler: False leaves SchedulerCog (and its database) out.
        task_db_path: Where the scheduled-task SQLite file lives.
        lounge_channel_id: The AI Lounge channel. Left out, it follows
                           COORDINATION_CHANNEL_ID, so lounge and coordination
                           share one channel with nothing extra to configure.
        session_dir_base: Base directory for session clone directories.
                          When set with ``session_source_repo``, a
                          SessionDirManager is created and attached to the bot.
                          Defaults to SESSION_DIR_BASE env var, else
                          ``~/.c-lord/<instance>/sessions`` (#837; ``<instance>``
                          is ``$CLORD_INSTANCE`` or the clone directory name).
                          An instance that already has ``./data/sessions``
                          keeps using it.
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
        A BridgeComponents holding the repositories this call initialized.
    """
    from .cogs.channel_repo import ChannelRepoCog
    from .cogs.claude_chat import ClaudeChatCog
    from .cogs.session_cleanup import SessionCleanupCog
    from .cogs.session_manage import SessionManageCog
    from .cogs.skill_command import SkillCommandCog
    from .cogs.transcript_mirror import TranscriptMirrorCog
    from .cogs.version_cmd import VersionCog
    from .database.channel_repo import ChannelRepository
    from .database.devenv_repo import DevEnvRepository
    from .database.thread_repo import ThreadRepository
    from .discord_ui.authorization import Authorizer
    from .legacy_env import warn_removed_delivery_env
    from .version import runtime_version, stale_build_age

    # #722: the first thing a startup log should answer is "which build is
    # this?". Until now nothing did — 30+ lines of INFO and ``grep -i version``
    # found nothing — so an instance could sit two days behind and tell its
    # user a shipped feature "does not exist" with no way for anyone to
    # notice. Logged here rather than in main() so instance repos that call
    # setup_bridge() themselves get it too (Zero-Config).
    version = runtime_version()
    logger.info("c-lord version %s", version)
    # #756: …and whether it is old. The line above names the build, but nobody
    # turns "-20260908" into "two weeks ago", so instances ran far behind main
    # while their users kept hitting bugs already fixed there. Judged from the
    # version string and today's date only — no network (Zero-Config: works,
    # and stays quiet, wherever it cannot reach one).
    age = stale_build_age(version)
    if age is not None:
        logger.warning(
            "this c-lord build is %d days old (%s): fixes merged upstream since then "
            "are not running here — upgrade and restart to pick them up",
            age,
            version,
        )

    # Role-based access control — auto-read from env var if not explicitly provided
    if allowed_role_name is None:
        allowed_role_name = os.getenv("CLORD_ALLOWED_ROLE") or None

    # #713: ONE allowlist predicate for every gate — messages, buttons,
    # /skill and /clord-init. They used to carry their own copies, which is how
    # two of them kept the fail-open default after #466 fixed it for the other
    # two. Sharing the instance also means the app owner resolved at on_ready
    # is in effect everywhere at once.
    authorizer = Authorizer(allowed_user_ids, allowed_role_name)

    # #712: say so if the operator's .env still selects the retired skill-push
    # delivery path. Wired here rather than in main() so instance repos that
    # call setup_bridge() themselves get the notice too (Zero-Config).
    warn_removed_delivery_env()

    # Nobody named a lounge channel: follow the coordination channel.
    if lounge_channel_id is None:
        from .coordination.env import coordination_channel_id_from_env

        lounge_channel_id = coordination_channel_id_from_env()  # #1194

    # Global SessionDirManager and TmuxSessionManager are no longer created here.
    # Per-channel managers are resolved dynamically via ChannelRepoCog.
    # The session_dir_base parameter is still accepted and forwarded to
    # ChannelRepoCog so per-channel directories share the same base path.
    if session_dir_base is None:
        session_dir_base = os.getenv("SESSION_DIR_BASE")
    if session_dir_base is None:
        # #837: nothing configured → ~/.c-lord/<instance>/sessions, unless this
        # instance already has workspaces in ./data/sessions (then unchanged).
        from .clord_home import LEGACY_SESSION_DIR_BASE, default_session_dir_base

        session_dir_base = default_session_dir_base()
        if session_dir_base is None:
            logger.info(
                "Session dirs: %s (existing; SESSION_DIR_BASE not set)",
                LEGACY_SESSION_DIR_BASE,
            )
        else:
            logger.info("Session dirs: %s (default; SESSION_DIR_BASE not set)", session_dir_base)
    else:
        logger.info("Session dirs: %s", session_dir_base)
    # #1289: an idle bot builds no SessionDirManager; mark the root now so the
    # other instance on this host (prod / staging) never trusts a workspace here.
    from .clord_home import LEGACY_SESSION_DIR_BASE as _LEGACY_BASE
    from .session_dir import mark_sessions_root

    mark_sessions_root(session_dir_base or _LEGACY_BASE)
    if session_source_repo is not None or os.getenv("SESSION_SOURCE_REPO"):
        import warnings

        warnings.warn(
            "session_source_repo / SESSION_SOURCE_REPO is deprecated. "
            "Use /clord-init to bind channels to repositories.",
            DeprecationWarning,
            stacklevel=2,
        )

    # --- Storage: one SQLite file behind every session-scoped repository ---
    stores = await _open_session_stores(session_db_path)
    session_repo = stores.sessions
    settings_repo = stores.settings
    ask_repo = stores.asks
    lounge_repo = stores.lounge
    resume_repo = stores.resumes

    # Generic Cogs (AutoUpgradeCog and friends) pick these up from the bot, so they work
    # without a hard import dependency on c-lord internals.
    bot.session_repo = session_repo  # type: ignore[attr-defined]
    bot.resume_repo = resume_repo  # type: ignore[attr-defined]
    bot.ask_repo = ask_repo  # type: ignore[attr-defined]
    bot.lounge_repo = lounge_repo  # type: ignore[attr-defined]
    bot.lounge_channel_id = lounge_channel_id  # type: ignore[attr-defined]

    # --- ClaudeChatCog: turns a message in the channel into a Claude session ---
    chat_cog = ClaudeChatCog(
        bot,  # type: ignore[arg-type]
        repo=session_repo,
        runner=runner,
        max_concurrent=max_concurrent,
        allowed_user_ids=allowed_user_ids,
        allowed_role_name=allowed_role_name,
        authorizer=authorizer,
        ask_repo=ask_repo,
        lounge_repo=lounge_repo,
        resume_repo=resume_repo,
        settings_repo=settings_repo,
    )
    await bot.add_cog(chat_cog)
    logger.info("Registered ClaudeChatCog (max_concurrent=%d)", max_concurrent)

    # --- Dev environment ownership (#573, wired in #612) ---
    # init_db() here is what creates the `dev_environments` table. #573 shipped
    # the repository without a single call site, so the table never existed on
    # the production host and the docker↔workspace link was never written.
    devenv_repo = DevEnvRepository(session_db_path)
    await devenv_repo.init_db()

    # --- SessionManageCog: the /workspace-* and session housekeeping commands ---
    session_manage_cog = SessionManageCog(
        bot,  # type: ignore[arg-type]
        repo=session_repo,
        settings_repo=settings_repo,
        devenv_repo=devenv_repo,
        authorizer=authorizer,
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
        authorizer=authorizer,
        session_dir_base=session_dir_base,
        session_repo=session_repo,
    )
    await bot.add_cog(channel_repo_cog)
    logger.info("Registered ChannelRepoCog")

    # --- SkillCommandCog: /skill needs to know which channel to open threads in ---
    if claude_channel_id is not None:
        skill_cog = SkillCommandCog(
            bot,  # type: ignore[arg-type]
            repo=session_repo,
            runner=runner,
            claude_channel_id=claude_channel_id,
            allowed_user_ids=allowed_user_ids,
            allowed_role_name=allowed_role_name,
            authorizer=authorizer,
            settings_repo=settings_repo,
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

    # --- TranscriptMirrorCog (Issue #71) — the delivery path (#712) ---
    transcript_cog = TranscriptMirrorCog(bot, session_repo=session_repo)
    await bot.add_cog(transcript_cog)
    bot.transcript_mirror_cog = transcript_cog  # type: ignore[attr-defined]
    logger.info("Registered TranscriptMirrorCog")

    # --- Login alert (#1309) — the owner hears about a logout by DM ---
    from .login_alert import login_alert

    login_alert.bind(bot)

    # --- Login relay (#1310) — log back in from that DM ---
    from .login_relay import LoginRelayCog, relay_enabled

    if relay_enabled():
        await bot.add_cog(LoginRelayCog(bot))
        logger.info("Registered LoginRelayCog")
    else:
        logger.info("Login relay disabled (CLORD_LOGIN_RELAY=0) — the DM alert stays (#1310)")

    # --- VersionCog (read-only /version + !version twin, zero-config) ---
    await bot.add_cog(VersionCog(bot))
    logger.info("Registered VersionCog")

    # --- SchedulerCog: only an instance that wants it gets tasks.db ---
    task_repo: TaskRepository | None = None
    if enable_scheduler:
        task_repo = await _start_scheduler(bot, runner, task_db_path)

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

    # --- Stopped-thread sweep (#685) ---
    # Always-on (zero-config): archives threads whose name already says
    # ``[停止]``/``[終了]`` but which Discord still shows as open. #609 fixed the
    # archive that runs *at* stop time; nothing ever went back for the ones
    # stopped before it, and 186 of them (58% of every open thread the bot could
    # see) had piled up in the sidebar by 2026-09-04. The idle sweeps above
    # cannot pick them up — they look for workspaces that are still running.
    # ``session_repo`` is the live-workspace guard, not the candidate source:
    # candidates come from the thread name, and a row that says the workspace
    # is *not* stopped holds the sweep back (see the module docstring).
    # Set CLORD_STOPPED_SWEEP=0 to disable.
    if os.getenv("CLORD_STOPPED_SWEEP", "1") not in ("0", "false", "no"):
        from .stopped_sweep import StoppedThreadSweepLoop

        stopped_loop = StoppedThreadSweepLoop(bot, session_repo)
        stopped_loop.start()
        bot.stopped_sweep_loop = stopped_loop  # type: ignore[attr-defined]
    else:
        logger.info("Stopped-thread sweep disabled (CLORD_STOPPED_SWEEP=0)")

    # --- Idle sleep (#572) ---
    # Always-on (zero-config): stops just the *claude process* of workspaces
    # nobody has touched for CLORD_IDLE_SLEEP_HOURS (default 4). docker keeps
    # running, the checkout / transcript / volumes / DB row all stay, and the
    # next message resumes without a word — so the user never sees it. The
    # threshold is a constant for the same reason the 7-day one is: it describes
    # how people use Discord, not how much RAM the host has. Set the env to 0 to
    # disable. See docs/specs/workspace-sleep.md.
    from .idle_sleep import IdleSleepLoop, idle_sleep_hours

    _sleep_hours = idle_sleep_hours()
    if _sleep_hours > 0:
        sleep_loop = IdleSleepLoop(bot, session_repo, threshold_hours=_sleep_hours)
        sleep_loop.start()
        bot.idle_sleep_loop = sleep_loop  # type: ignore[attr-defined]
    else:
        logger.info("Idle-sleep disabled (CLORD_IDLE_SLEEP_HOURS=0)")

    # --- Resident workspace cap (#576) ---
    # Backstop, not the main control: TTL (sleep 4h / stop 7d) is. This only
    # exists so a runaway cannot take the host down before a TTL fires. Unlike
    # the TTLs, the value **is** host-dependent, so the default is computed from
    # MemTotal (cgroup limit first) rather than shipped as a constant — a fixed
    # default would break every host smaller than the one it was measured on
    # (#540). Override with CLORD_MAX_RESIDENT_WORKSPACES; 0 disables.
    #
    # It never blocks a new workspace. The loop only removes residents; there is
    # nothing on the turn path that consults it. See docs/specs/resident-cap.md.
    from .resident_cap import ResidentCapLoop, max_resident_workspaces, memory_total

    _total = memory_total()
    _cap = max_resident_workspaces(total_bytes=_total)
    logger.info(
        "Resident cap: %s (memory budget source: %.1f GiB total)",
        f"{_cap} workspace(s)" if _cap > 0 else "disabled (CLORD_MAX_RESIDENT_WORKSPACES=0)",
        _total / (1024**3) if _total else 0.0,
    )
    if _cap > 0:
        cap_loop = ResidentCapLoop(bot, session_repo, limit=_cap)
        cap_loop.start()
        bot.resident_cap_loop = cap_loop  # type: ignore[attr-defined]

    # --- Orphan working-directory sweep (#613) ---
    # Always-on (zero-config): reclaims workspace directories no ``sessions``
    # row points at any more. #575 deletes a directory as it deletes its row, so
    # a directory whose row is already gone is unreachable by that path — 313 of
    # them (17.3 GB) had piled up on the production host. Needs the base path to
    # know where to look, so it stays off when session dirs are not configured.
    # Set CLORD_ORPHAN_SWEEP_DAYS=0 to disable.
    if session_dir_base:
        from .orphan_dirs import OrphanSweepLoop, orphan_sweep_days

        _orphan_days = orphan_sweep_days()
        if _orphan_days > 0:
            orphan_loop = OrphanSweepLoop(
                session_repo, session_dir_base, min_idle_days=_orphan_days
            )
            orphan_loop.start()
            bot.orphan_sweep_loop = orphan_loop  # type: ignore[attr-defined]
        else:
            logger.info("Orphan-dir sweep disabled (CLORD_ORPHAN_SWEEP_DAYS=0)")

    # --- Menu watchdog (#359) ---
    # Always-on (zero-config): bridges TUI AskUserQuestion/plan menus that no
    # turn is watching (turn finalized early; mirror cannot read the tool_use
    # until resolution). Independent of the thread-name lamp above — the user's
    # ability to answer menus must not depend on an opt-in cosmetic. Opt out
    # with CLORD_MENU_WATCHDOG=0.
    if os.getenv("CLORD_MENU_WATCHDOG", "1") not in ("0", "false", "no"):
        from .database.menu_bridge_repo import MenuBridgeRepository
        from .menu_ledger import MenuRebridgeLedger, use_shared_ledger
        from .thread_state_sync import MenuWatchdogLoop

        # The ledger is SQLite-backed so "already posted this menu" survives a
        # restart — in memory it did not, and every restart re-posted every
        # stranded menu (#633).
        rebridge_ledger = MenuRebridgeLedger(MenuBridgeRepository(session_db_path))
        # #717: and the turn-side bridge writes to the SAME ledger, so a menu it
        # posted is not mistaken for one nobody has seen after a restart. Before
        # this, only the watchdog's own posts were recorded — and the watchdog
        # posts the minority of menus, so a restart duplicated the question card
        # (and its 経緯) in every thread that had one open.
        use_shared_ledger(rebridge_ledger)
        # repo wired so the sweep only bridges windows THIS bot owns on a shared
        # tmux server — never another bot's menu (#438).
        menu_watchdog = MenuWatchdogLoop(
            bot,
            is_processing=chat_cog.is_processing,
            repo=session_repo,
            rebridge_ledger=rebridge_ledger,
        )
        menu_watchdog.start()
        bot.menu_watchdog = menu_watchdog  # type: ignore[attr-defined]

    from .clord_home import LEGACY_SESSION_DIR_BASE

    components = BridgeComponents(
        session_repo=session_repo,
        task_repo=task_repo,
        lounge_repo=lounge_repo,
        resume_repo=resume_repo,
        session_dir_base=session_dir_base or LEGACY_SESSION_DIR_BASE,
    )

    # Auto-wire repos to ApiServer if provided
    if api_server is not None:
        components.apply_to_api_server(api_server)
        logger.info("Auto-wired repos to ApiServer (port=%d)", api_server.port)

    return components
