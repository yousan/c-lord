"""Session repository for thread-to-session mapping."""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class SessionRecord:
    """A stored session mapping."""

    thread_id: int
    session_id: str
    working_dir: str | None
    model: str | None
    origin: str
    summary: str | None
    created_at: str
    last_used_at: str
    # Issue #95: thread naming redesign
    topic: str | None = None
    state: str | None = "alive"
    tmux_window_id: str | None = None
    auto_topic_locked: int = 0
    topic_source: str | None = None
    # Issue #115: trigger message for reply threading
    trigger_message_id: int | None = None
    # Issue #215: uuid of the last final answer the mirror delivered
    mirror_replied_uuid: str | None = None
    # Issue #281: persisted state-sync rename rate-limit deadline (wall-clock
    # "YYYY-MM-DD HH:MM:SS"); survives restarts so the rename window is honoured.
    rename_backoff_until: str | None = None
    # Issue #414: the Issue/PR number this thread is working on, shown in the
    # thread name as "#<n>". Auto-detected from the git branch / first message.
    issue_ref: str | None = None
    # Issue #593: the Issue/PR number this thread was *opened for*. Written once
    # and never moved, so the sidebar keeps a stable handle on the thread even
    # after the work moves to a spun-off Issue. ``None`` on rows that never had a
    # number — nothing is invented for them.
    origin_issue_ref: str | None = None
    # Issue #512: timestamp of an intentional stop, or None when the workspace
    # is open. Distinct from a merely dead tmux pane (#270).
    closed_at: str | None = None
    # Issue #574: who stopped it — "manual" or "idle". Read **only** to word the
    # notice. The state itself is still decided by closed_at alone, so a second
    # column can never contradict the first (the #538 failure mode).
    closed_reason: str | None = None
    # Issue #572: when the 4-hour sleep stopped this workspace's Claude, or None
    # once any turn has run since. Read **only** to word the resume: a slept
    # workspace comes back silently, a crashed one announces itself (#464).
    # Whether to resume is still decided by "is the pane alive?" alone.
    slept_at: str | None = None


def _record(row) -> SessionRecord:
    """Build a :class:`SessionRecord` from a ``SELECT *`` row.

    Columns the dataclass does not know about are **dropped** rather than passed
    through. Migrations here only ever add columns, so a database is always at or
    ahead of the code that opens it — and a bot running yesterday's code against
    a database today's code migrated used to die on every single read with
    ``TypeError: unexpected keyword argument``. That is not a hypothetical: it is
    what a staging clone does the moment it is switched back to ``main`` after
    verifying a branch that added a column (#576, observed 2026-08-31 with
    ``slept_at``), and it is what a rollback of a release would do in production.

    Dropping unknown columns makes the older code simply not see the new field,
    which is exactly what it did before the column existed.
    """
    known = {f.name for f in fields(SessionRecord)}
    return SessionRecord(**{k: v for k, v in dict(row).items() if k in known})


class SessionRepository:
    """CRUD operations for session records."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def get(self, thread_id: int) -> SessionRecord | None:
        """Get session by Discord thread ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE thread_id = ?",
                (thread_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return _record(row)

    async def save(
        self,
        thread_id: int,
        session_id: str,
        working_dir: str | None = None,
        model: str | None = None,
        origin: str = "discord",
        summary: str | None = None,
    ) -> SessionRecord:
        """Create or update a session mapping."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO sessions
                     (thread_id, session_id, working_dir, model, origin, summary)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(thread_id) DO UPDATE SET
                     session_id = excluded.session_id,
                     working_dir = COALESCE(excluded.working_dir, sessions.working_dir),
                     model = COALESCE(excluded.model, sessions.model),
                     origin = COALESCE(excluded.origin, sessions.origin),
                     summary = COALESCE(excluded.summary, sessions.summary),
                     -- A turn is starting, so this workspace is awake by
                     -- definition. Clearing it here rather than at each call
                     -- site means no turn path (scheduler, skill, webhook) can
                     -- forget to, and it can only ever be cleared — never set —
                     -- so it cannot contradict `set_slept` (#572).
                     slept_at = NULL,
                     last_used_at = datetime('now', 'localtime')""",
                (thread_id, session_id, working_dir, model, origin, summary),
            )
            await db.commit()

        record = await self.get(thread_id)
        if record is None:
            raise RuntimeError(f"Failed to retrieve session after save for thread {thread_id}")
        return record

    async def list_all(self, limit: int = 50) -> list[SessionRecord]:
        """List all sessions ordered by most recently used.

        Args:
            limit: Maximum number of records to return.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM sessions ORDER BY last_used_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [_record(row) for row in rows]

    async def delete(self, thread_id: int) -> bool:
        """Delete a session mapping. Returns True if a row was deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM sessions WHERE thread_id = ?",
                (thread_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def reset(self, thread_id: int) -> bool:
        """Clear the session_id but keep the row, so the next message starts fresh.

        Used by /clear: the row's existence is what allows on_message to route
        future messages into _handle_thread_reply (issue #117).
        Returns True if a row was updated, False if no row existed.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE sessions SET session_id = '', last_used_at = datetime('now', 'localtime')"
                " WHERE thread_id = ?",
                (thread_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    # ── Issue #95: thread naming redesign helpers ─────────────────────

    async def get_by_thread_id(self, thread_id: int) -> SessionRecord | None:
        """Alias for ``get``. Provided for naming consistency."""
        return await self.get(thread_id)

    async def set_topic(self, thread_id: int, topic: str, source: str = "llm") -> None:
        """Set the stable topic and topic_source for a thread."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET topic = ?, topic_source = ? WHERE thread_id = ?",
                (topic, source, thread_id),
            )
            await db.commit()

    async def set_state(self, thread_id: int, state: str) -> None:
        """Set the session state (alive/pending/dead)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET state = ? WHERE thread_id = ?",
                (state, thread_id),
            )
            await db.commit()

    async def set_tmux_window_id(self, thread_id: int, window_id: str | None) -> None:
        """Store the tmux internal window-id (e.g. '@7') for the thread."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET tmux_window_id = ? WHERE thread_id = ?",
                (window_id, thread_id),
            )
            await db.commit()

    async def lock_topic(self, thread_id: int) -> None:
        """Mark the topic as user-locked (no further auto-rewrite)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET auto_topic_locked = 1 WHERE thread_id = ?",
                (thread_id,),
            )
            await db.commit()

    async def set_issue_ref(self, thread_id: int, issue_ref: str | None) -> None:
        """Persist the Issue/PR number the thread is working on (Issue #414).

        ``issue_ref`` is a bare digit string (e.g. ``"404"``) or ``None`` to clear.

        The **first** number a thread is ever given also becomes its
        ``origin_issue_ref`` (#593) — the identity shown in the sidebar. The
        ``COALESCE`` is what makes that write-once: later branch switches move
        ``issue_ref`` alone, and clearing ``issue_ref`` never clears the origin.
        Keeping it here rather than at the call sites means every path that
        records a number (naming pass, pending drain, API) seeds the origin.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions "
                "   SET issue_ref = ?, "
                "       origin_issue_ref = COALESCE(origin_issue_ref, ?) "
                " WHERE thread_id = ?",
                (issue_ref, issue_ref, thread_id),
            )
            await db.commit()

    async def set_closed(self, thread_id: int, closed: bool, *, reason: str = "manual") -> None:
        """Mark the workspace stopped (停止) or reopen it (#512, #574).

        ``closed=True`` stamps ``closed_at`` with the local wall clock;
        ``closed=False`` clears it back to ``NULL``.

        This is what distinguishes a deliberate ``/workspace-stop`` from a tmux
        pane that merely died: a dead pane still auto-resumes on the next message
        via ``--continue`` (#270), whereas a stopped workspace holds the message
        and shows the reopen notice instead.

        *reason* (``"manual"`` / ``"idle"``) is stored alongside so the notice can
        say **why** it happened. It is never consulted to decide *whether* the
        workspace is stopped — ``closed_at`` alone answers that, so the two
        cannot drift apart.
        """
        async with aiosqlite.connect(self.db_path) as db:
            if closed:
                await db.execute(
                    "UPDATE sessions SET closed_at = datetime('now', 'localtime'), "
                    "closed_reason = ? WHERE thread_id = ?",
                    (reason, thread_id),
                )
            else:
                await db.execute(
                    "UPDATE sessions SET closed_at = NULL, closed_reason = NULL "
                    "WHERE thread_id = ?",
                    (thread_id,),
                )
            await db.commit()

    async def set_slept(self, thread_id: int, slept: bool) -> None:
        """Record (or clear) the 4-hour sleep for this workspace (#572).

        Sleep stops the workspace's Claude and nothing else — no ``closed_at``,
        no rename, no notice — because the whole point is that the user does not
        notice. That leaves the next message facing a dead pane, which is
        indistinguishable from a crash; and a crash *must* be announced,
        otherwise the resumed turn re-emits the prior output and reads as the bot
        replaying garbage (#464). This column is what tells the two apart.

        It words the resume and nothing else. Whether to resume at all is still
        decided by "is the pane alive?", exactly as before, so a stale value here
        can never resume (or refuse to resume) anything — the same discipline as
        ``closed_reason``.
        """
        async with aiosqlite.connect(self.db_path) as db:
            if slept:
                await db.execute(
                    "UPDATE sessions SET slept_at = datetime('now', 'localtime') "
                    "WHERE thread_id = ?",
                    (thread_id,),
                )
            else:
                await db.execute(
                    "UPDATE sessions SET slept_at = NULL WHERE thread_id = ?",
                    (thread_id,),
                )
            await db.commit()

    async def update_trigger_message(self, thread_id: int, message_id: int) -> None:
        """Persist the Discord message ID that triggered the current Claude turn.

        Used by ApiServer to thread the /api/reply response back to the
        user's message (Issue #115).
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET trigger_message_id = ? WHERE thread_id = ?",
                (message_id, thread_id),
            )
            await db.commit()

    async def set_mirror_replied_uuid(self, thread_id: int, uuid: str) -> None:
        """Persist the uuid of the last final answer the mirror delivered.

        Used by TranscriptMirror (live) and TranscriptMirrorCog (restart
        recovery) to dedupe re-delivery of a dropped final answer (Issue #215).
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET mirror_replied_uuid = ? WHERE thread_id = ?",
                (uuid, thread_id),
            )
            await db.commit()

    async def set_rename_backoff_until(self, thread_id: int, deadline: str | None) -> None:
        """Persist the state-sync rename rate-limit deadline (Issue #281).

        ``deadline`` is a wall-clock "YYYY-MM-DD HH:MM:SS" string (or None to
        clear). Persisting it lets a bot restart honour Discord's per-channel
        rename window instead of forgetting the in-memory backoff and re-PATCHing
        within the window (429).
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET rename_backoff_until = ? WHERE thread_id = ?",
                (deadline, thread_id),
            )
            await db.commit()

    async def list_alive(self) -> list[SessionRecord]:
        """List sessions whose state is 'alive'."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE state = 'alive' ORDER BY last_used_at DESC"
            )
            rows = await cursor.fetchall()
            return [_record(row) for row in rows]

    async def all_working_dirs(self) -> set[str]:
        """Every ``working_dir`` any row still claims — closed rows included.

        The orphan sweep (#613) decides what to delete by *absence* from this
        set, so it must err towards listing too much. A stopped workspace is
        still someone's checkout; filtering by state here would hand it to the
        sweep as an orphan.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT working_dir FROM sessions WHERE working_dir IS NOT NULL"
            )
            rows = await cursor.fetchall()
            return {row[0] for row in rows if row[0]}

    async def cleanup_old(self, days: int = 30) -> list[SessionRecord]:
        """Delete sessions unused for N days. Returns the rows that were deleted.

        Returns the rows rather than a count (#554) because the caller has to
        tell each of those threads what happened, and a count names no thread.
        They are read inside the same transaction as the DELETE and with the
        same predicate, so the list is exactly what went — no row can be deleted
        without being reported, and none reported without being deleted.

        The row carries ``working_dir``, which is the only handle left on the
        session dir and the transcript once the row is gone. Reading it after the
        delete would be too late; :func:`c_lord.session_cleanup.inspect_survivors`
        needs it to say what survived.
        """
        where = " WHERE julianday('now', 'localtime') - julianday(last_used_at) >= ?"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM sessions" + where, (days,))
            doomed = [_record(row) for row in await cursor.fetchall()]
            if not doomed:
                return []
            await db.execute("DELETE FROM sessions" + where, (days,))
            await db.commit()
            return doomed
