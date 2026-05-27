"""Inject per-session skill files under ``<session_dir>/.claude/skills/``."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .discord_prompt_choice import render_discord_prompt_choice_skill
from .discord_reply import render_discord_reply_skill

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:8080"

# Skill directories inject_skills writes. Kept here so remove_injected_skills
# stays symmetric with what we create.
INJECTED_SKILL_NAMES: tuple[str, ...] = ("discord-reply", "discord-prompt-choice")


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

    # Issue #63: prompt-choice skill — lets Claude push a numbered menu to
    # Discord when it would otherwise stall waiting for the user to pick.
    choice_dir = skills_root / "discord-prompt-choice"
    choice_dir.mkdir(parents=True, exist_ok=True)
    choice_path = choice_dir / "SKILL.md"
    choice_path.write_text(
        render_discord_prompt_choice_skill(
            thread_id=thread_id, api_url=api_url, api_secret=api_secret
        ),
        encoding="utf-8",
    )
    written.append(str(choice_path))
    logger.info(
        "Injected discord-prompt-choice skill for thread %d at %s",
        thread_id,
        choice_path,
    )
    return written


def remove_injected_skills(session_dir: str | os.PathLike[str]) -> list[str]:
    """Remove c-lord skill dirs previously written by :func:`inject_skills`.

    Used when skill-based reply is disabled (e.g. ``CLORD_BRIDGE_MODE=jsonl``)
    so a stale skill left over from an earlier skill-mode session can't mislead
    Claude into POSTing to a REST API that is no longer running. Idempotent and
    only touches c-lord-managed skill dirs — user skills are left untouched.

    Returns:
        Absolute paths of the skill dirs that were removed.
    """
    skills_root = Path(session_dir) / ".claude" / "skills"
    removed: list[str] = []
    for name in INJECTED_SKILL_NAMES:
        skill_dir = skills_root / name
        if skill_dir.is_dir():
            shutil.rmtree(skill_dir, ignore_errors=True)
            removed.append(str(skill_dir))
            logger.info("Removed injected skill %s (skill reply disabled)", skill_dir)
    return removed


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
    return os.getenv("CLORD_BRIDGE_MODE", "skill").strip().lower() != "jsonl"
