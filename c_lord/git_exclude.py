"""Keep files c-lord puts into a checkout out of the user's commits (#528, #779).

Claude works inside the checkout and runs ``git add -A``. Anything c-lord
writes there — Discord uploads (#528), the injected ``discord-read`` skill
(#779) — shows up untracked and is one ``git add -A`` away from a commit, which
for the skill meant this host's ``.env`` path landing in a public repository.

``.git/info/exclude`` is the clone's own ignore list: it hides a path from
``git status`` / ``git add`` without touching the user's ``.gitignore``. It only
applies to untracked files — a path the repository already tracks has to be
removed from it (#779 did that for c-lord itself).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def add_git_exclude(work_dir: str | os.PathLike[str], entry: str, reason: str) -> None:
    """Append *entry* to ``<work_dir>/.git/info/exclude`` once.

    *reason* becomes the ``# c-lord: …`` comment line above it, so a human
    reading the file can tell who put the line there and why. Idempotent — an
    entry already present is not written again. Best effort: no ``.git``
    directory (not a repository, or a worktree whose ``.git`` is a file) and
    any I/O failure are logged and swallowed, never raised — hiding a file is
    never worth failing the turn over.
    """
    git_dir = Path(work_dir) / ".git"
    exclude = git_dir / "info" / "exclude"
    try:
        if not git_dir.is_dir():
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if entry in existing.splitlines():
            return
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        exclude.write_text(
            f"{existing}{prefix}# c-lord: {reason}\n{entry}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not git-exclude %s in %s: %s", entry, work_dir, exc)
