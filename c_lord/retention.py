"""How long a workspace's conversation survives — Issue #575.

c-lord's automatic deletion is pinned to **Claude Code's own transcript
retention**, not to a number of its own choosing.

**Why.** #540 originally specified "delete the working directory at 90 days,
keep the conversation". Implementing it ran into two facts:

1. Claude Code deletes transcripts by itself. Measured on the production host
   (3,506 files): 113 in the 21–30 day band, **zero older than 30 days** — a
   clean cut exactly at 30. The key is ``cleanupPeriodDays``; it is present in
   the Claude Code 2.1.245 binary next to the string *"Transcript retention
   cleanup"*, though the public settings page does not list it.
2. c-lord itself deleted ``sessions`` rows at 30 days on every startup (#554).
   The row is the only handle tying a Discord thread to its working directory,
   so deleting it stranded the directory — the direct cause of the 118 GB that
   had accumulated.

Together those made 90 days unreachable: nothing survives to day 90 to be
deleted. The threshold was a number with no mechanism behind it.

**Decision (2026-08-27, yousan).** c-lord does **not** change
``cleanupPeriodDays`` — it governs all of Claude Code, and a Discord front-end
has no business rewriting it. Instead c-lord *matches* it, and says so when it
deletes. The two sweeps become one line: when Claude forgets the conversation,
the folder it belonged to is cleared away too.

This module only ever **reads**.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: The Claude Code settings key that governs transcript retention.
SETTING_KEY = "cleanupPeriodDays"

#: Used when the key is absent or unusable.
#:
#: 30 is Claude Code's own default. Confirmed by measurement rather than by
#: documentation — the public settings page does not list the key, so the number
#: comes from the observed cut-off on a host that has never set it.
RETENTION_FALLBACK_DAYS = 30

#: Where an organisation-managed policy lives on Linux. Highest precedence in
#: Claude Code's own settings order, so it wins here too.
DEFAULT_MANAGED_PATH = "/etc/claude-code/managed-settings.json"


def _read_setting(path: Path) -> int | None:
    """The retention value in *path*, or None when it is absent or unusable.

    Every failure is None rather than an exception: a settings file that is
    missing, unreadable, malformed, or holds nonsense must never stop the bot —
    and must never be read as "retention is zero", which would mean deleting
    everything.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("retention: %s is not valid JSON — ignoring", path)
        return None
    if not isinstance(data, dict):
        return None

    value = data.get(SETTING_KEY)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning(
            "retention: %s=%r in %s is not a number — ignoring", SETTING_KEY, value, path
        )
        return None
    days = int(value)
    if days <= 0:
        logger.warning(
            "retention: %s=%r in %s is not positive — ignoring", SETTING_KEY, value, path
        )
        return None
    return days


def claude_transcript_retention_days(*, managed_path: str | None = None) -> int:
    """Days Claude Code keeps a transcript before deleting it.

    Consulted in Claude Code's own precedence order, highest first: the
    organisation-managed file, then the user's ``~/.claude/settings.json``. An
    organisation's retention policy must not be silently undercut by a user
    setting, which is why managed wins.

    Project-scoped settings are deliberately **not** consulted: the sweep runs
    over ``~/.claude/projects`` as a whole, so it is a per-user property, and
    reading a per-project value here would report a number that does not govern
    anything.
    """
    for path in (
        Path(managed_path or DEFAULT_MANAGED_PATH),
        Path(os.path.expanduser("~")) / ".claude" / "settings.json",
    ):
        days = _read_setting(path)
        if days is not None:
            return days
    return RETENTION_FALLBACK_DAYS
