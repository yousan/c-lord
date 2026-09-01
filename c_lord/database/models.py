"""SQLite database schema and initialization."""

from __future__ import annotations

import contextlib
import logging

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    thread_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    working_dir TEXT,
    model TEXT,
    origin TEXT NOT NULL DEFAULT 'discord',
    summary TEXT,
    topic TEXT,
    state TEXT DEFAULT 'alive',
    tmux_window_id TEXT,
    auto_topic_locked INTEGER NOT NULL DEFAULT 0,
    topic_source TEXT,
    rename_backoff_until TEXT,
    closed_at TEXT,
    closed_reason TEXT,
    slept_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_used_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_last_used ON sessions(last_used_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_session_id ON sessions(session_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_asks (
    thread_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    questions_json TEXT NOT NULL,
    question_idx INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS lounge_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL DEFAULT 'AI',
    message TEXT NOT NULL,
    posted_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_lounge_posted_at ON lounge_messages(posted_at);

-- Sessions that should be resumed after a bot restart.
-- Rows expire automatically via TTL checks in PendingResumeRepository.
-- A Claude session that is about to restart the bot writes a row here first;
-- on_ready reads and deletes it to resume the session.
CREATE TABLE IF NOT EXISTS pending_resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL UNIQUE,
    session_id TEXT,           -- optional: used for "claude --resume" continuity
    reason TEXT NOT NULL DEFAULT 'self_restart',
    resume_prompt TEXT,        -- message to post + send to Claude on resume
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Per-channel repository bindings (1 channel = 1 repo)
CREATE TABLE IF NOT EXISTS channel_repo_bindings (
    channel_id         INTEGER PRIMARY KEY,
    source_repo        TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Per-thread repository bindings (overrides channel binding for that thread)
CREATE TABLE IF NOT EXISTS thread_repo_bindings (
    thread_id   INTEGER PRIMARY KEY,
    source_repo TEXT    NOT NULL,
    channel_id  INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- #633: menus the #359 watchdog has already posted to a thread. In memory this
-- ledger was wiped by every bot restart, so a stranded menu was re-posted on
-- each new process (188 re-bridges of one thread; the same embed six times over
-- three days). A row is deleted only when a sweep sees the pane with no menu.
CREATE TABLE IF NOT EXISTS menu_bridges (
    thread_id        INTEGER NOT NULL,
    fingerprint      TEXT    NOT NULL,
    posts            INTEGER NOT NULL DEFAULT 0,
    first_bridged_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_bridged_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (thread_id, fingerprint)
);
"""

# Migrations for existing databases that lack new columns.
_MIGRATIONS = [
    # #633: durable "already bridged" ledger for the menu watchdog.
    (
        "CREATE TABLE IF NOT EXISTS menu_bridges ("
        "thread_id INTEGER NOT NULL, "
        "fingerprint TEXT NOT NULL, "
        "posts INTEGER NOT NULL DEFAULT 0, "
        "first_bridged_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')), "
        "last_bridged_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')), "
        "PRIMARY KEY (thread_id, fingerprint))"
    ),
    "ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'discord'",
    "ALTER TABLE sessions ADD COLUMN summary TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_session_id ON sessions(session_id)",
    # Lounge table added in v1.x — safe to run on existing DBs
    (
        "CREATE TABLE IF NOT EXISTS lounge_messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "label TEXT NOT NULL DEFAULT 'AI', "
        "message TEXT NOT NULL, "
        "posted_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')))"
    ),
    "CREATE INDEX IF NOT EXISTS idx_lounge_posted_at ON lounge_messages(posted_at)",
    # pending_resumes added in v1.3 — safe to run on existing DBs
    (
        "CREATE TABLE IF NOT EXISTS pending_resumes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "thread_id INTEGER NOT NULL UNIQUE, "
        "session_id TEXT, "
        "reason TEXT NOT NULL DEFAULT 'self_restart', "
        "resume_prompt TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')))"
    ),
    # channel_repo_bindings added for /clord-init (per-channel repo binding)
    (
        "CREATE TABLE IF NOT EXISTS channel_repo_bindings ("
        "channel_id INTEGER PRIMARY KEY, "
        "source_repo TEXT NOT NULL, "
        "clone_branch TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')))"
    ),
    # tmux_session_name column for per-channel tmux session isolation
    "ALTER TABLE channel_repo_bindings ADD COLUMN tmux_session_name TEXT",
    # Drop unused columns (requires SQLite 3.35.0+; suppressed on older versions)
    "ALTER TABLE channel_repo_bindings DROP COLUMN clone_branch",
    "ALTER TABLE channel_repo_bindings DROP COLUMN tmux_session_name",
    # thread_repo_bindings added for /clord-thread-init (per-thread repo override)
    (
        "CREATE TABLE IF NOT EXISTS thread_repo_bindings ("
        "thread_id INTEGER PRIMARY KEY, "
        "source_repo TEXT NOT NULL, "
        "channel_id INTEGER, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')))"
    ),
    # Issue #95: thread naming redesign — stable topic + volatile state
    "ALTER TABLE sessions ADD COLUMN topic TEXT",
    "ALTER TABLE sessions ADD COLUMN state TEXT DEFAULT 'alive'",
    "ALTER TABLE sessions ADD COLUMN tmux_window_id TEXT",
    "ALTER TABLE sessions ADD COLUMN auto_topic_locked INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN topic_source TEXT",
    # Issue #115: silent posts + reply threading
    "ALTER TABLE sessions ADD COLUMN trigger_message_id INTEGER",
    # Issue #215: uuid of the last final answer the mirror delivered, so a
    # restart can detect & re-deliver a final answer dropped while the bot
    # was down (mirror not tailing).
    "ALTER TABLE sessions ADD COLUMN mirror_replied_uuid TEXT",
    # Issue #281: persist the state-sync rename rate-limit deadline (wall-clock)
    # so a bot restart honours Discord's ~10-min per-channel rename window
    # instead of forgetting it (in-memory backoff resets on restart → 429).
    "ALTER TABLE sessions ADD COLUMN rename_backoff_until TEXT",
    # Issue #414: the Issue/PR number the thread is working on, auto-detected
    # from the session's git branch or first message and shown in the thread
    # name as "#<n>". Persisted so it is stable across restarts.
    "ALTER TABLE sessions ADD COLUMN issue_ref TEXT",
    # Issue #593: the Issue/PR number the thread was *opened for* — its identity
    # in the sidebar. Written once (the first time any number is known) and never
    # moved again, unlike issue_ref which follows the git branch every turn. The
    # two were one column, so switching to a spun-off Issue's branch erased the
    # number the thread was findable by.
    "ALTER TABLE sessions ADD COLUMN origin_issue_ref TEXT",
    # Backfill for rows written before the column existed: adopt the number they
    # currently carry. Their display does not change (origin == current renders
    # as one number) and the *next* branch switch is tracked. Idempotent — it
    # only ever fills NULLs, so re-running it on every startup is a no-op.
    "UPDATE sessions SET origin_issue_ref = issue_ref "
    "WHERE origin_issue_ref IS NULL AND issue_ref IS NOT NULL",
    # Issue #512: when the user closed this session on purpose
    # (/close-workspace). NULL = open. Persisting it is what lets c-lord tell an
    # intentional 終了 apart from a pane that merely died (bot restart, tmux-server
    # death) — the latter still auto-resumes via --continue (#270), the former
    # holds the message and asks the user to reopen.
    "ALTER TABLE sessions ADD COLUMN closed_at TEXT",
    # #574: why it was stopped ("manual" / "idle"). Wording only — the
    # state itself is still decided by closed_at alone, so the two can
    # never disagree.
    "ALTER TABLE sessions ADD COLUMN closed_reason TEXT",
    # #572: when the 4-hour sleep stopped this workspace's Claude. NULL once any
    # turn has run since. Read **only** to word the resume — a slept workspace
    # comes back without a word, a crashed one says so (#464). Whether to resume
    # at all is still decided by "is the pane alive?" alone, so this column can
    # never contradict the thing it describes (the closed_reason rule).
    "ALTER TABLE sessions ADD COLUMN slept_at TEXT",
]


async def init_db(db_path: str) -> None:
    """Initialize the database with the schema.

    For fresh databases the full SCHEMA is applied. For existing databases
    the migration statements add any missing columns idempotently.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            with contextlib.suppress(Exception):
                await db.execute(stmt)
        await db.commit()
    logger.info("Database initialized at %s", db_path)
