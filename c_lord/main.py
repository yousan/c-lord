"""Entry point for c-lord bot."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from .bot import ClaudeDiscordBot
from .claude.config import ClaudeConfig
from .setup import setup_bridge
from .utils.logger import setup_logging

logger = logging.getLogger(__name__)


def load_config() -> dict[str, str]:
    """Load and validate configuration from environment."""
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


async def main() -> None:
    """Start the bot."""
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    setup_logging(log_level)
    config = load_config()

    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
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
