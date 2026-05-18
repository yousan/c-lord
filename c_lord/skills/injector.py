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
    api_secret: str | None = None,
) -> list[str]:
    """Write the c-lord skill bundle into ``<session_dir>/.claude/skills/``.

    Idempotent: existing files are overwritten so per-session values
    (``thread_id``, ``api_url``, ``api_secret``) stay in sync if they change.

    Args:
        session_dir: Path to the per-thread session directory.
        thread_id: Discord thread ID the session is bridged to.
        api_url: Base URL of the c-lord REST API. Defaults to
            ``CLORD_API_URL`` env var, or ``http://127.0.0.1:8080``.
        api_secret: Bearer token the API server requires. When omitted,
            falls back to the ``CLORD_API_SECRET`` env var. If neither is
            set, the auth-less template is rendered.

    Returns:
        Absolute paths to the SKILL.md files written.
    """
    if api_url is None:
        api_url = os.getenv("CLORD_API_URL") or DEFAULT_API_URL
    api_url = api_url.rstrip("/")

    if api_secret is None:
        api_secret = os.getenv("CLORD_API_SECRET") or None

    skills_root = Path(session_dir) / ".claude" / "skills"

    written: list[str] = []
    skill_dir = skills_root / "discord-reply"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        render_discord_reply_skill(thread_id=thread_id, api_url=api_url, api_secret=api_secret),
        encoding="utf-8",
    )
    written.append(str(skill_path))
    logger.info(
        "Injected discord-reply skill for thread %d at %s (api_url=%s, auth=%s)",
        thread_id,
        skill_path,
        api_url,
        "bearer" if api_secret else "none",
    )
    return written


def skills_enabled() -> bool:
    """Return True unless explicitly disabled via env var.

    Skill-based reply has historically been the only path for posting Claude's
    answers to Discord (#53).  Issue #71 introduces the JSONL transcript
    mirror as a replacement; while it is enabled (``CLORD_BRIDGE_MODE=jsonl``)
    the skill must NOT be injected — otherwise every event would be posted
    twice.  ``USE_SKILL_REPLY=0`` remains available as a manual override.
    """
    value = os.getenv("USE_SKILL_REPLY", "").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if os.getenv("CLORD_BRIDGE_MODE", "skill").strip().lower() == "jsonl":
        return False
    return True
