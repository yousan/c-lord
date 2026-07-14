"""ChannelRepository — CRUD for channel_repo_bindings table.

Maps Discord channel IDs to source repositories, enabling per-channel
session directory management via /clord-init.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)

CHANNEL_REPO_SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_repo_bindings (
    channel_id         INTEGER PRIMARY KEY,
    source_repo        TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def normalize_repo_url(url: str) -> str:
    """Normalize a (possibly derived) repository URL to a clonable ``owner/repo.git``.

    Users frequently paste GitHub/GitLab/Bitbucket *derived* URLs (a PR, issue,
    file blob, tree, etc.) instead of the repository URL itself. ``git clone``
    cannot consume those. This shrinks any such URL back to the repository root
    and appends ``.git`` so the result is directly clonable (#88).

    Examples::

        'https://github.com/owner/repo/pull/2'        → 'https://github.com/owner/repo.git'
        'https://github.com/owner/repo/issues/5'      → 'https://github.com/owner/repo.git'
        'https://github.com/owner/repo/blob/main/x.py' → 'https://github.com/owner/repo.git'
        'https://github.com/owner/repo'               → 'https://github.com/owner/repo.git'
        'https://github.com/owner/repo.git'           → 'https://github.com/owner/repo.git'
        'git@github.com:owner/repo'                   → 'git@github.com:owner/repo.git'
        'git@github.com:owner/repo.git'               → 'git@github.com:owner/repo.git'
        'https://gitlab.com/owner/repo/-/merge_requests/3' → 'https://gitlab.com/owner/repo.git'

    Non-HTTP(S) inputs that are not ``git@host:`` SSH URLs (e.g. local paths) are
    returned unchanged apart from whitespace stripping, since their structure is
    unknown. Empty input is returned unchanged.
    """
    url = url.strip()
    if not url:
        return url

    # SSH shorthand: git@host:owner/repo[.git] — only ensure a .git suffix.
    if url.startswith("git@") and ":" in url:
        return url if url.endswith(".git") else f"{url}.git"

    if url.startswith(("http://", "https://")):
        scheme, rest = url.split("://", 1)
        rest = rest.strip("/")
        parts = rest.split("/")
        # Need at least host/owner/repo to identify the repository root.
        if len(parts) < 3:
            return url
        host, owner, repo = parts[0], parts[1], parts[2]
        if repo.endswith(".git"):
            repo = repo[:-4]
        return f"{scheme}://{host}/{owner}/{repo}.git"

    # Unknown form (local path, etc.) — leave as-is.
    return url


def derive_session_name(source_repo: str) -> str:
    """Derive a tmux session name from a repository URL or path.

    Examples::

        'https://github.com/org/my-project.git' → 'my-project'
        'git@github.com:org/my-project.git'     → 'my-project'
        '/home/user/repos/my-project'           → 'my-project'
        'git@github.com:org/NiyaReco.love.git'  → 'NiyaReco_love'

    tmux forbids ``.`` and ``:`` in session names — its target syntax
    ``session:window.pane`` uses them as separators — and silently rewrites
    them to ``_`` when a session is created. We mirror that rewrite here so the
    name c-lord later hands to ``tmux -t`` matches the one tmux actually stored.
    Otherwise a dotted repo name (e.g. ``NiyaReco.love``) makes every window
    op target a non-existent session and Claude never starts (#474).
    """
    name = source_repo.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    # Match tmux's own `.`/`:` → `_` substitution (see session_check_name).
    name = name.replace(".", "_").replace(":", "_")
    return name or "clord"


class ChannelRepository:
    """Async CRUD for channel_repo_bindings table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init_db(self) -> None:
        """Initialize the channel_repo_bindings schema."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(CHANNEL_REPO_SCHEMA)
            await db.commit()
        logger.info("Channel repo DB initialized at %s", self.db_path)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get(self, channel_id: int) -> dict | None:
        """Return a binding for the given channel, or None."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM channel_repo_bindings WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def list_all(self) -> list[dict]:
        """Return all channel-repo bindings."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM channel_repo_bindings ORDER BY created_at")
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def save(
        self,
        channel_id: int,
        source_repo: str,
    ) -> None:
        """Insert or replace a channel-repo binding."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO channel_repo_bindings
                   (channel_id, source_repo,
                    created_at, updated_at)
                   VALUES (?, ?, datetime('now', 'localtime'),
                           datetime('now', 'localtime'))""",
                (channel_id, source_repo),
            )
            await db.commit()
        logger.info("Saved channel binding: channel=%d repo=%s", channel_id, source_repo)

    async def delete(self, channel_id: int) -> bool:
        """Delete a binding. Returns True if a row was deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM channel_repo_bindings WHERE channel_id = ?",
                (channel_id,),
            )
            await db.commit()
            return cursor.rowcount > 0
