"""Entry point for c-lord bot."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import sys
from pathlib import Path
from typing import IO

from dotenv import load_dotenv

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
    """Load and validate configuration from environment."""
    if env_path is not None:
        load_dotenv(env_path)
    else:
        load_dotenv()

    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        logger.error("DISCORD_BOT_TOKEN is required")
        sys.exit(1)

    channel_id = os.getenv("DISCORD_CHANNEL_ID", "")
    if not channel_id:
        logger.error("DISCORD_CHANNEL_ID is required")
        sys.exit(1)

    return {
        "token": token,
        "channel_id": channel_id,
        "claude_command": os.getenv("CLAUDE_COMMAND", "claude"),
        "claude_model": os.getenv("CLAUDE_MODEL", "sonnet"),
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

    db_path = str(data_dir / "sessions.db")

    runner = ClaudeConfig(
        command=config["claude_command"],
        model=config["claude_model"],
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
