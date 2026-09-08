"""Inject per-session skill files under ``<session_dir>/.claude/skills/``."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .discord_read import render_discord_read_skill

logger = logging.getLogger(__name__)

READ_SKILL_NAME = "discord-read"

# Skills c-lord used to inject for the *output* path: Claude was told to POST
# its final answer (and interactive menus) to the c-lord REST API at the end of
# every turn (#53, #63). #712 removed that path — delivery is the JSONL
# transcript mirror — but a session dir created before the upgrade still has the
# SKILL.md on disk, and the REST API it names is now always listening. Left
# alone, Claude would follow it and the thread would get the answer twice, so
# every session dir is scrubbed of them on each turn.
LEGACY_SKILL_NAMES: tuple[str, ...] = ("discord-reply", "discord-prompt-choice")


def inject_read_skill(
    session_dir: str | os.PathLike[str],
    env_path: str | None = None,
) -> str:
    """Write the ``discord-read`` skill into ``<session_dir>/.claude/skills/``.

    Reading other Discord channels via curl is independent of how Claude's
    *output* reaches Discord, and it does not touch the c-lord REST API — it
    talks to Discord's own. So it is injected into every session (#259).

    Idempotent (overwrites). Only the ``.env`` *path* is baked in — never the
    token value.

    Args:
        session_dir: Path to the per-thread session directory.
        env_path: Absolute path to c-lord's ``.env``. Defaults to the
            ``CLORD_ENV_PATH`` env var; if unset, the path-less read template
            is rendered.

    Returns:
        Absolute path to the SKILL.md written.
    """
    if env_path is None:
        env_path = os.getenv("CLORD_ENV_PATH") or None

    read_dir = Path(session_dir) / ".claude" / "skills" / READ_SKILL_NAME
    read_dir.mkdir(parents=True, exist_ok=True)
    read_path = read_dir / "SKILL.md"
    read_path.write_text(
        render_discord_read_skill(env_path=env_path),
        encoding="utf-8",
    )
    logger.info(
        "Injected discord-read skill at %s (env_path=%s)",
        read_path,
        env_path or "(none)",
    )
    return str(read_path)


def remove_legacy_skills(session_dir: str | os.PathLike[str]) -> list[str]:
    """Remove skill dirs left over from the retired skill-push path (#712).

    See :data:`LEGACY_SKILL_NAMES` for why a leftover is harmful rather than
    merely stale. Idempotent, and only touches c-lord-managed skill dirs —
    ``discord-read`` and the user's own skills are left alone.

    Returns:
        Absolute paths of the skill dirs that were removed.
    """
    skills_root = Path(session_dir) / ".claude" / "skills"
    removed: list[str] = []
    for name in LEGACY_SKILL_NAMES:
        skill_dir = skills_root / name
        if skill_dir.is_dir():
            shutil.rmtree(skill_dir, ignore_errors=True)
            removed.append(str(skill_dir))
            logger.info("Removed legacy skill %s (skill-push path retired, #712)", skill_dir)
    return removed
