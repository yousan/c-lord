"""Skill bundles c-lord injects into each Claude Code session directory.

When ``USE_SKILL_REPLY`` env is enabled, :func:`inject_skills` writes per-session
SKILL.md files (with ``thread_id`` and ``api_url`` baked in) under
``<session_dir>/.claude/skills/``. This lets Claude push final answers to
Discord by curl-ing the c-lord REST API, replacing the legacy
``capture-pane`` scraping path (#52).
"""

from __future__ import annotations

from .discord_reply import DISCORD_REPLY_SKILL, render_discord_reply_skill
from .injector import inject_skills

__all__ = ["DISCORD_REPLY_SKILL", "inject_skills", "render_discord_reply_skill"]
