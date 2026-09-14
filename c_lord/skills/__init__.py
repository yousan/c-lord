"""Skill bundles c-lord injects into each Claude Code session directory.

Only ``discord-read`` is injected (#259): it lets Claude read other Discord
channels by curl-ing the *Discord* REST API, which is independent of how
Claude's own answers reach Discord.

The output side used to live here too — a ``discord-reply`` SKILL.md Claude had
to remember to call at the end of every turn (#53). That path was replaced by
the JSONL transcript mirror (#71/#216) and removed in #712, together with the
env switch that could bring it back.
"""

from __future__ import annotations

from .discord_read import render_discord_read_skill
from .injector import inject_read_skill, remove_legacy_skills

__all__ = [
    "inject_read_skill",
    "remove_legacy_skills",
    "render_discord_read_skill",
]
