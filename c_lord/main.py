"""Entry point for c-lord bot."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
import os
import signal
import sys
from pathlib import Path
from typing import IO

from dotenv import find_dotenv, load_dotenv

from .bot import ClaudeDiscordBot
from .claude.config import ClaudeConfig
from .setup import setup_bridge
from .utils.logger import setup_logging

logger = logging.getLogger(__name__)


def acquire_single_instance_lock(data_dir: Path) -> IO[bytes] | None:
    """Acquire an exclusive non-blocking flock on ``data_dir/.bot.lock``.

    Issue #212: prevents two bot processes with the same data dir from
    double-connecting to the Discord gateway and double-processing events
    (the staging incident on 2026-05-27).

    The returned file handle must be retained for the lifetime of the process;
    closing it releases the lock. flock auto-releases on process exit, so a
    crash does not leave a stale lock.

    Returns ``None`` when ``CLORD_ALLOW_MULTI_INSTANCE=1`` is set (advanced
    users who understand the risk). Calls ``sys.exit(1)`` if another instance
    already holds the lock.
    """
    if os.getenv("CLORD_ALLOW_MULTI_INSTANCE") == "1":
        logger.warning("CLORD_ALLOW_MULTI_INSTANCE=1 set; skipping single-instance guard")
        return None

    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".bot.lock"
    # Intentionally not a context manager: the handle must outlive this function
    # so flock stays held for the process lifetime (released on process exit).
    lock_fp = open(lock_path, "wb")  # noqa: SIM115
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as e:
        lock_fp.close()
        logger.error(
            "Another bot instance is already running with the same data dir "
            "(%s). Refusing to start to avoid Discord double-connect. "
            "Set CLORD_ALLOW_MULTI_INSTANCE=1 to bypass. (flock error: %s)",
            data_dir,
            e,
        )
        sys.exit(1)
    return lock_fp


def acquire_token_lock(token: str, lock_dir: Path | None = None) -> IO[str] | None:
    """Acquire a host-global exclusive flock keyed on the bot TOKEN (#325).

    The #212 data-dir lock keys on a filesystem path, but the real hazard is
    two processes sharing one Discord token: a clone with a different data dir
    (e.g. an ``.env`` pointing at production) passes the data-dir lock and
    double-connects to the gateway anyway. This lock enforces the actual
    invariant — one process per token — before Discord is ever touched.

    The lock file is named by a SHA-256 prefix of the token (no plaintext
    token on disk) and records the holder's pid/cwd so a refused second
    process can say WHO is already running. flock auto-releases on exit.

    Returns ``None`` when ``CLORD_ALLOW_MULTI_INSTANCE=1`` (same escape hatch
    as the data-dir lock). Calls ``sys.exit(1)`` on conflict.
    """
    if os.getenv("CLORD_ALLOW_MULTI_INSTANCE") == "1":
        logger.warning("CLORD_ALLOW_MULTI_INSTANCE=1 set; skipping token-lock guard")
        return None

    if lock_dir is None:
        lock_dir = Path.home() / ".cache" / "c-lord" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(token.encode()).hexdigest()[:16]
    lock_path = lock_dir / f"token-{digest}.lock"
    # Not a context manager: the handle must outlive this function so the
    # flock stays held for the process lifetime (released on process exit).
    lock_fp = open(lock_path, "a+")  # noqa: SIM115
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lock_fp.seek(0)
        holder = lock_fp.read().strip() or "(unknown)"
        lock_fp.close()
        logger.error(
            "Another bot process is already running with the SAME Discord "
            "token — refusing to start to avoid a gateway double-connect "
            "(#325). Holder: %s. If this is intentional, set "
            "CLORD_ALLOW_MULTI_INSTANCE=1.",
            holder,
        )
        sys.exit(1)
        return None  # only reached in tests where sys.exit is mocked
    # Record holder info for the error message of a refused second process.
    lock_fp.seek(0)
    lock_fp.truncate()
    lock_fp.write(f"pid={os.getpid()} cwd={Path.cwd()}\n")
    lock_fp.flush()
    return lock_fp


def resolve_data_dir(env_path: Path | None) -> Path:
    """Resolve the directory that holds sessions.db / notifications.db.

    Issue #202: when launched via ``c-lord start --env <path>``, data files must
    live next to the given .env — not relative to the CWD — otherwise starting
    from a different directory silently creates an empty sessions.db and orphans
    every existing session. Standalone ``python -m c_lord.main`` (env_path=None)
    keeps the legacy CWD-relative ``data/``.
    """
    base = env_path.parent if env_path is not None else Path(".")
    return base / "data"


def load_config(env_path: Path | None = None) -> dict[str, str]:
    """Load and validate configuration from environment.

    Issue #324: ``override=True`` — keys defined in the .env file always beat
    inherited process env. Claude sessions spawned by a bot inherit that bot's
    DISCORD_* vars; with the python-dotenv default (override=False) those
    silently won over the local .env and a "staging" launch booted as the
    production identity (#322). Directory == identity: the .env file is the
    single source of truth for every key it defines. Keys absent from the
    file still fall back to the process env (env-var-only setups keep working).
    """
    if env_path is not None:
        load_dotenv(env_path, override=True)
    else:
        # find_dotenv(usecwd=True): search from the CURRENT DIRECTORY upward,
        # not from this package's location. README documents the bare launch as
        # "reads .env in current dir" — the package-location walk only matched
        # that contract by accident (repo-clone deployments) and diverges for
        # any CWD outside the package tree.
        found = find_dotenv(usecwd=True)
        if found:
            load_dotenv(found, override=True)

    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        logger.error("DISCORD_BOT_TOKEN is required")
        sys.exit(1)

    channel_id = os.getenv("DISCORD_CHANNEL_ID", "")
    if not channel_id:
        logger.error("DISCORD_CHANNEL_ID is required")
        sys.exit(1)

    # Issue #323: optional identity guard. A garbage value must fail loudly —
    # a guard that silently disables itself is worse than no guard.
    expected_bot_user_id = os.getenv("EXPECTED_BOT_USER_ID", "")
    if expected_bot_user_id and not expected_bot_user_id.isdigit():
        logger.error(
            "EXPECTED_BOT_USER_ID must be a numeric Discord user id, got: %r",
            expected_bot_user_id,
        )
        sys.exit(1)

    return {
        "token": token,
        "channel_id": channel_id,
        "expected_bot_user_id": expected_bot_user_id,
        "claude_command": os.getenv("CLAUDE_COMMAND", "claude"),
        "claude_model": os.getenv("CLAUDE_MODEL", "sonnet"),
        "claude_effort": os.getenv("CLAUDE_EFFORT", ""),
        "claude_permission_mode": os.getenv("CLAUDE_PERMISSION_MODE", "acceptEdits"),
        "claude_working_dir": os.getenv("CLAUDE_WORKING_DIR", ""),
        "max_concurrent": os.getenv("MAX_CONCURRENT_SESSIONS", "3"),
        "timeout": os.getenv("SESSION_TIMEOUT_SECONDS", "300"),
        "owner_id": os.getenv("DISCORD_OWNER_ID", ""),
        "coordination_channel_id": os.getenv("COORDINATION_CHANNEL_ID", ""),
    }


async def main(env_path: Path | None = None) -> None:
    """Start the bot."""
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    setup_logging(log_level)
    config = load_config(env_path)

    data_dir = resolve_data_dir(env_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Issue #212: refuse to start if another bot with the same data dir is
    # already running. The handle must be retained for the process lifetime —
    # flock auto-releases on close/exit. Assigning to a local that lives until
    # main() returns is sufficient; we don't need to reference it again.
    _instance_lock = acquire_single_instance_lock(data_dir)  # noqa: F841

    # Issue #325: additionally refuse if another process already holds THIS
    # TOKEN (host-global). The data-dir lock alone cannot stop a same-token
    # double-connect launched from a different clone/directory.
    _token_lock = acquire_token_lock(config["token"])  # noqa: F841

    db_path = str(data_dir / "sessions.db")

    runner = ClaudeConfig(
        command=config["claude_command"],
        model=config["claude_model"],
        effort=config["claude_effort"] or None,
        permission_mode=config["claude_permission_mode"],
        working_dir=config["claude_working_dir"] or None,
        timeout_seconds=int(config["timeout"]),
    )

    owner_id = int(config["owner_id"]) if config["owner_id"] else None
    coordination_channel_id = (
        int(config["coordination_channel_id"]) if config["coordination_channel_id"] else None
    )
    allowed_user_ids = {owner_id} if owner_id else None
    allowed_role_name = os.getenv("CLORD_ALLOWED_ROLE") or None

    bot = ClaudeDiscordBot(
        channel_id=int(config["channel_id"]),
        owner_id=owner_id,
        coordination_channel_id=coordination_channel_id,
        expected_bot_user_id=(
            int(config["expected_bot_user_id"]) if config["expected_bot_user_id"] else None
        ),
    )

    # Issue #53: REST API is the only path Claude has for reaching Discord
    # (the legacy capture-pane scrape→post pipeline was removed). Always start
    # the API server unless skills are explicitly disabled via USE_SKILL_REPLY=0.
    from .skills.injector import skills_enabled

    api_server = None
    api_port_env = os.getenv("CLORD_API_PORT", "")
    if skills_enabled():
        try:
            from .database.notification_repo import NotificationRepository
            from .ext.api_server import ApiServer
        except ImportError:
            logger.warning(
                "aiohttp is not installed; API server will NOT start and "
                "Claude has no path to Discord. Install with `uv add aiohttp`."
            )
        else:
            notif_db = str(data_dir / "notifications.db")
            notif_repo = NotificationRepository(notif_db)
            await notif_repo.init_db()
            api_port = int(api_port_env) if api_port_env.isdigit() else 8080
            api_server = ApiServer(
                repo=notif_repo,
                bot=bot,
                default_channel_id=int(config["channel_id"]),
                host=os.getenv("CLORD_API_HOST", "127.0.0.1"),
                port=api_port,
                api_secret=os.getenv("CLORD_API_SECRET") or None,
                # #372: OGP/URL link previews are OFF by default; opt back in.
                show_url_embeds=os.getenv("CLORD_SHOW_URL_EMBEDS", "false").strip().lower()
                in ("1", "true", "yes", "on"),
            )

    async with bot:
        components = await setup_bridge(
            bot,
            runner,
            api_server=api_server,
            session_db_path=db_path,
            task_db_path=str(data_dir / "tasks.db"),
            allowed_user_ids=allowed_user_ids,
            allowed_role_name=allowed_role_name,
            claude_channel_id=int(config["channel_id"]),
            enable_scheduler=True,
            max_concurrent=int(config["max_concurrent"]),
        )

        if api_server is not None:
            await api_server.start()
            logger.info(
                "REST API enabled (host=%s port=%d) — discord-reply skill ready",
                api_server.host,
                api_server.port,
            )

        # Cleanup old sessions on startup
        deleted = await components.session_repo.cleanup_old(days=30)
        if deleted:
            logger.info("Cleaned up %d old sessions", deleted)

        # Handle signals
        loop = asyncio.get_running_loop()

        async def _shutdown() -> None:
            if api_server is not None:
                await api_server.stop()
            await bot.close()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))

        await bot.start(config["token"])


if __name__ == "__main__":
    asyncio.run(main())
