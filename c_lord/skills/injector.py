"""Inject per-session skill files under ``<session_dir>/.claude/skills/``."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .discord_reply import render_discord_reply_skill

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:8080"


def inject_skills(
    session_dir: str | os.PathLike[str],
    thread_id: int,
    api_url: str | None = None,
) -> list[str]:
    """Write the c-lord skill bundle into ``<session_dir>/.claude/skills/``.

    Idempotent: existing files are overwritten so per-session values
    (``thread_id``, ``api_url``) stay in sync if they change.

    Args:
        session_dir: Path to the per-thread session directory.
        thread_id: Discord thread ID the session is bridged to.
        api_url: Base URL of the c-lord REST API. Defaults to
            ``CLORD_API_URL`` env var, or ``http://127.0.0.1:8080``.

    Returns:
        Absolute paths to the SKILL.md files written.
    """
    if api_url is None:
        api_url = os.getenv("CLORD_API_URL") or DEFAULT_API_URL
    api_url = api_url.rstrip("/")

    skills_root = Path(session_dir) / ".claude" / "skills"

    written: list[str] = []
    skill_dir = skills_root / "discord-reply"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        render_discord_reply_skill(thread_id=thread_id, api_url=api_url),
        encoding="utf-8",
    )
    written.append(str(skill_path))
    logger.info(
        "Injected discord-reply skill for thread %d at %s (api_url=%s)",
        thread_id,
        skill_path,
        api_url,
    )
    return written


def skills_enabled() -> bool:
    """Return True if skill-based reply is enabled via env var (#52 flag)."""
    value = os.getenv("USE_SKILL_REPLY", "").strip().lower()
    return value in {"1", "true", "yes", "on"}
