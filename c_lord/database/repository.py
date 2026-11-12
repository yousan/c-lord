# <<REDACTED 4 lines>>
import itertools
# <<REDACTED 1 lines>>
from collections.abc import Iterable
from dataclasses import dataclass, fields
# <<REDACTED 3 lines>>
from ..utils.logger import log_ctx
# <<REDACTED 2 lines>>


# <<REDACTED 12 lines>>
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
    # #818: the one exception is ``"swept"`` (:data:`SWEPT_REASON`) — the
    # tombstone the 30-day sweep leaves instead of deleting the row. See there.
    closed_reason: str | None = None
    # Issue #572: when the 4-hour sleep stopped this workspace's Claude, or None
    # once any turn has run since. Read **only** to word the resume: a slept
    # workspace comes back silently, a crashed one announces itself (#464).
    # Whether to resume is still decided by "is the pane alive?" alone.
    slept_at: str | None = None
# <<REDACTED 2 lines>>
#: #818: ``closed_reason`` of a row the 30-day sweep tidied away.
#:
#: The sweep used to ``DELETE`` the row, and with it the only link from a
#: Discord thread to its ``working_dir`` and Claude session id. It now keeps the
#: row as a **tombstone**: ``closed_at`` says when, this value says why. No new
#: column and no new state — a tombstone is a stopped row whose stop was the
#: sweep.
#:
#: A tombstone is history, not a session. Every live read in this repository
#: (:meth:`SessionRepository.get`, the lists, :meth:`~SessionRepository.reset`)
#: skips it, so the rest of c-lord sees exactly what it saw when the row was
#: deleted; only :meth:`SessionRepository.get_swept` returns it. Writing the row
#: again (:meth:`SessionRepository.save` — a reattach or a new turn) lifts it.
SWEPT_REASON = "swept"

#: The predicate every live read appends. ``IS NOT`` so that ``NULL`` passes.
#: A fixed fragment (no user input) — keep it in step with :data:`SWEPT_REASON`.
_LIVE = "closed_reason IS NOT 'swept'"

_NOTHING: object = object()  # "no refused trigger write is waiting" (#1254)


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


# <<REDACTED 5 lines>>
        # #1212: a thread's first turn records its trigger before the session
        # row exists (the row comes with the turn's SYSTEM event), so the
        # UPDATE had nothing to write. Held here until ``save`` creates the row.
        self._pending_triggers: dict[int, int] = {}
        # #1262: the trigger of the turn whose prompt reached Claude's pane last,
        # per thread — see :meth:`note_trigger_sent`.
        self._sent_triggers: dict[int, int] = {}
        # #1262: message turns whose trigger is recorded but whose prompt has
        # not reached the pane yet (and whose turn has not ended) — see
        # :meth:`note_trigger_in_flight`.
        self._in_flight: dict[int, set[int]] = {}
        # #1262: a clear the database refused (locked), forgotten with the
        # thread's next ``save`` and hidden from :meth:`get` until then. Exact
        # ids only, so a newer message on the row is never touched.
        self._unapplied_clears: dict[int, frozenset[int]] = {}
        # #1254: a trigger write the database refused (locked), written with the
        # thread's next ``save`` (or at shutdown) and seen by :meth:`get` until
        # then. ``None``: the refused write was then forgotten (a clear named
        # it) — what lands is "nothing on record", as if it had been written and
        # cleared, not the older message it would have replaced.
        self._unwritten_triggers: dict[int, int | None] = {}
        # #1254: the order trigger writes started in, and per thread the latest
        # one that landed or was kept — an older write refused after a newer
        # one landed must not be kept to be written over it.
        self._write_order = itertools.count()
        self._latest_write: dict[int, int] = {}
# <<REDACTED 2 lines>>
        """Get session by Discord thread ID. A swept tombstone (#818) is not one."""
        # <<REDACTED 3 lines>>
                f"SELECT * FROM sessions WHERE thread_id = ? AND {_LIVE}",
                # <<REDACTED 5 lines>>
            record = _record(row)
        # #1254: a trigger write the database refused still holds for this process.
        if thread_id in self._unwritten_triggers:
            record.trigger_message_id = self._unwritten_triggers[thread_id]
        # #1262: a clear the database refused still holds for this process.
        if record.trigger_message_id in self._unapplied_clears.get(thread_id, ()):
            record.trigger_message_id = None
        return record
# <<REDACTED 1 lines>>
    async def get_swept(self, thread_id: int) -> SessionRecord | None:
        """The tombstone the 30-day sweep left for ``thread_id``, or ``None`` — #818.

        The way back from a thread to the session it once had: ``working_dir``,
        ``session_id``, and when (``closed_at``) it was tidied away. ``None`` for
        a live or stopped row — those are :meth:`get`'s.
        """
        # <<REDACTED 3 lines>>
                "SELECT * FROM sessions WHERE thread_id = ? AND closed_reason = ?",
                (thread_id, SWEPT_REASON),
            # <<REDACTED 2 lines>>
            return None if row is None else _record(row)

    # <<REDACTED 21 lines>>
                     -- A turn is starting, so this workspace is awake by
                     -- definition. Clearing it here rather than at each call
                     -- site means no turn path (scheduler, skill, webhook) can
                     -- forget to, and it can only ever be cleared — never set —
                     -- so it cannot contradict `set_slept` (#572).
                     slept_at = NULL,
                     -- #818: writing a swept row again (a reattach, a new turn)
                     -- brings it back to life. Only the tombstone is lifted; a
                     -- stop the user made stays theirs to undo.
                     closed_at = CASE WHEN sessions.closed_reason = ?
                                 THEN NULL ELSE sessions.closed_at END,
                     closed_reason = CASE WHEN sessions.closed_reason = ?
                                     THEN NULL ELSE sessions.closed_reason END,
                     # <<REDACTED 1 lines>>
                (
                    thread_id,
                    session_id,
                    working_dir,
                    model,
                    origin,
                    summary,
                    SWEPT_REASON,
                    SWEPT_REASON,
                ),
            # <<REDACTED 1 lines>>
            clear = self._unapplied_clears.get(thread_id)
            if clear:
                await self._execute_clear(db, thread_id, clear)
            pending = self._pending_triggers.pop(thread_id, None)
            if pending is not None:
                await db.execute(
                    "UPDATE sessions SET trigger_message_id = ? WHERE thread_id = ?",
                    (pending, thread_id),
                )
            unwritten = self._unwritten_triggers.get(thread_id, _NOTHING)
            if unwritten is not _NOTHING:
                await db.execute(
                    "UPDATE sessions SET trigger_message_id = ? WHERE thread_id = ?",
                    (unwritten, thread_id),
                )
            # <<REDACTED 1 lines>>
        if clear and self._unapplied_clears.get(thread_id) == clear:
            self._unapplied_clears.pop(thread_id, None)
        if unwritten is not _NOTHING and self._unwritten_triggers.get(thread_id, _NOTHING) == (
            unwritten
        ):
            self._unwritten_triggers.pop(thread_id, None)
# <<REDACTED 6 lines>>
    async def list_all(self, limit: int = 50) -> list[SessionRecord]:
        # <<REDACTED 8 lines>>
                f"SELECT * FROM sessions WHERE {_LIVE} ORDER BY last_used_at DESC LIMIT ?",
                # <<REDACTED 3 lines>>
            return [_record(row) for row in rows]
# <<REDACTED 2 lines>>
        """Delete a session mapping. Returns True if a row was deleted.

        The thread's in-memory trigger state goes with it (#1254): a kept write,
        a pending one or a waiting clear must not land on the row a later
        session makes for the same thread.
        """
        for held in (
            self._unwritten_triggers,
            self._pending_triggers,
            self._unapplied_clears,
            self._sent_triggers,
            self._in_flight,
            self._latest_write,
        ):
            held.pop(thread_id, None)
        # <<REDACTED 8 lines>>
    async def reset(self, thread_id: int) -> bool:
        """Clear the session_id but keep the row, so the next message starts fresh.

        Used by /clear: the row's existence is what allows on_message to route
        future messages into _handle_thread_reply (issue #117).
        Returns True if a row was updated, False if no row existed.
        # <<REDACTED 3 lines>>
                "UPDATE sessions SET session_id = '', last_used_at = datetime('now', 'localtime')"
                f" WHERE thread_id = ? AND {_LIVE}",
                # <<REDACTED 5 lines>>
    # ── Issue #95: thread naming redesign helpers ─────────────────────

    async def get_by_thread_id(self, thread_id: int) -> SessionRecord | None:
        """Alias for ``get``. Provided for naming consistency."""
        return await self.get(thread_id)

    async def set_topic(self, thread_id: int, topic: str, source: str = "llm") -> None:
        """Set the stable topic and topic_source for a thread."""
        # <<REDACTED 2 lines>>
                "UPDATE sessions SET topic = ?, topic_source = ? WHERE thread_id = ?",
                (topic, source, thread_id),
            )
            await db.commit()

    async def set_state(self, thread_id: int, state: str) -> None:
        """Set the session state (alive/pending/dead)."""
        # <<REDACTED 2 lines>>
                "UPDATE sessions SET state = ? WHERE thread_id = ?",
                (state, thread_id),
            )
            await db.commit()

    async def set_tmux_window_id(self, thread_id: int, window_id: str | None) -> None:
        """Store the tmux internal window-id (e.g. '@7') for the thread."""
        # <<REDACTED 2 lines>>
                "UPDATE sessions SET tmux_window_id = ? WHERE thread_id = ?",
                (window_id, thread_id),
            )
            await db.commit()

    async def lock_topic(self, thread_id: int) -> None:
        """Mark the topic as user-locked (no further auto-rewrite)."""
        # <<REDACTED 1 lines>>
            await db.execute(
                "UPDATE sessions SET auto_topic_locked = 1 WHERE thread_id = ?",
                # <<REDACTED 3 lines>>

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
        # <<REDACTED 2 lines>>
                "UPDATE sessions "
                "   SET issue_ref = ?, "
                "       origin_issue_ref = COALESCE(origin_issue_ref, ?) "
                " WHERE thread_id = ?",
                (issue_ref, issue_ref, thread_id),
            )
            await db.commit()

    async def set_origin_issue_ref(self, thread_id: int, origin_issue_ref: str) -> None:
        """Seed ``origin_issue_ref`` (#593) without touching ``issue_ref`` (#856).

        For a number read off the thread's **name** (``#812 と…``, ``W5 │ #769 …``):
        it says what the thread was opened for, not what it is working on now, so
        it must not move ``issue_ref``. Write-once like :meth:`set_issue_ref` —
        an origin already recorded is never replaced.
        """
        # <<REDACTED 2 lines>>
                "UPDATE sessions SET origin_issue_ref = COALESCE(origin_issue_ref, ?) "
                " WHERE thread_id = ?",
                (origin_issue_ref, thread_id),
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
        # <<REDACTED 2 lines>>
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
        # <<REDACTED 2 lines>>
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

    async def touch(self, thread_id: int) -> None:
        """Mark this workspace as used *now* — #642.

        What :meth:`save` does for a turn, for the paths that bring a workspace
        back up **without** running one (``/tmux-screenshot`` waking a stopped
        thread). ``last_used_at`` is what every reaper reads, so a wake has to
        write it, and has to write it **first**:

        * the 4-hour sweep and #576's LRU cap both select on this column. Waking
          a workspace whose row still says "idle since yesterday" means the very
          next tick kills the window *while Claude is still booting* — observed
          on staging: the pane was killed 47s into a wake, and the wake then
          timed out looking at a window that no longer existed.
        * :class:`c_lord.idle_sleep.IdleSleepLoop` also skips a thread it has
          already acted on at that timestamp, so a wake that never moved it
          would leave the restored Claude resident forever.

        It deliberately does **not** clear ``slept_at``: that column only picks
        the wording of the next resume (#572), and until the pane is actually up
        the honest answer is still "this was slept". The caller clears it once
        the wake succeeds.
        """
        # <<REDACTED 2 lines>>
                "UPDATE sessions SET last_used_at = datetime('now', 'localtime') "
                "WHERE thread_id = ?",
                (thread_id,),
            )
            await db.commit()

    async def update_trigger_message(self, thread_id: int, message_id: int) -> None:
        """Persist the Discord message ID that triggered the current Claude turn.

        Used by ApiServer to thread the /api/reply response back to the
        user's message (Issue #115).

        #1254: when the database refuses the write (locked), the value is kept:
        :meth:`get` returns it, the thread's next :meth:`save` writes it (as does
        :meth:`flush_unapplied_clears` at shutdown), and the error is raised for
        the caller to log. Left on the row, the older message's author would be
        the one pinged after a restart.
        """
        order = next(self._write_order)
        try:
            # <<REDACTED 2 lines>>
                    "UPDATE sessions SET trigger_message_id = ? WHERE thread_id = ?",
                    (message_id, thread_id),
                )
                await db.commit()
        except Exception:
            # Kept only when no write that started after it landed or was kept.
            if self._note_latest_write(thread_id, order):
                self._pending_triggers.pop(thread_id, None)
                self._unwritten_triggers[thread_id] = message_id
                while len(self._unwritten_triggers) > 1000:
                    self._unwritten_triggers.pop(next(iter(self._unwritten_triggers)))
            raise
        if not self._note_latest_write(thread_id, order):
            return  # a newer write is kept and still waiting to land
        self._unwritten_triggers.pop(thread_id, None)
        if cursor.rowcount == 0:
            # #1212: no row yet (first turn) — written by ``save`` when it comes.
            self._pending_triggers[thread_id] = message_id
            while len(self._pending_triggers) > 1000:
                self._pending_triggers.pop(next(iter(self._pending_triggers)))
        else:
            self._pending_triggers.pop(thread_id, None)

    def _note_latest_write(self, thread_id: int, order: int) -> bool:
        """Whether the trigger write started at *order* is the newest one for
        *thread_id* to land or be kept so far (and note it as such) — #1254."""
        if order < self._latest_write.get(thread_id, -1):
            return False
        self._latest_write[thread_id] = order
        while len(self._latest_write) > 1000:
            self._latest_write.pop(next(iter(self._latest_write)))
        return True

    def pending_trigger(self, thread_id: int) -> int | None:
        """The first-turn trigger still waiting for *thread_id*'s row, if any."""
        return self._pending_triggers.get(thread_id)

    def note_trigger_in_flight(self, thread_id: int, message_id: int) -> None:
        """A message turn recorded *message_id*; its prompt is on its way — #1262.

        Until :meth:`note_trigger_sent` (the prompt is in the pane) or
        :meth:`end_trigger_in_flight` (the turn ended without it), a ``/skill``
        whose prompt reaches the pane meanwhile does not take this trigger away:
        this turn's prompt arrives after it, so this turn is the one the pane
        answers. In memory only — after a restart nothing is in flight.
        """
        self._in_flight.setdefault(thread_id, set()).add(message_id)
        while len(self._in_flight) > 1000:
            self._in_flight.pop(next(iter(self._in_flight)))

    def end_trigger_in_flight(self, thread_id: int, message_id: int) -> bool:
        """The turn of *message_id* is over, sent or not (#1262).

        ``True`` when it was still in flight — its prompt never reached the pane
        (:meth:`note_trigger_sent` takes the mark off first).
        """
        waiting = self._in_flight.get(thread_id)
        if waiting is None or message_id not in waiting:
            return False
        waiting.discard(message_id)
        if not waiting:
            del self._in_flight[thread_id]
        return True

    def trigger_in_flight(self, thread_id: int, message_id: int) -> bool:
        """Whether *message_id*'s prompt is still on its way to the pane (#1262)."""
        return message_id in self._in_flight.get(thread_id, ())

    def note_trigger_sent(self, thread_id: int, message_id: int | None) -> None:
        """Remember whose prompt reached Claude's pane last in *thread_id* — #1262.

        Called the moment a turn's prompt is really in the pane (the runner's
        ``on_prompt_sent``): *message_id* is that turn's trigger, ``None`` for a
        turn without one (``/skill``). The pane answers the input it got last, so
        this — not when a trigger was recorded — says which turn is the newer.
        In memory only: after a restart every trigger on record belongs to a turn
        sent long ago.
        """
        if message_id is None:
            self._sent_triggers.pop(thread_id, None)
            return
        self.end_trigger_in_flight(thread_id, message_id)
        self._sent_triggers[thread_id] = message_id
        while len(self._sent_triggers) > 1000:
            self._sent_triggers.pop(next(iter(self._sent_triggers)))

    def sent_trigger(self, thread_id: int) -> int | None:
        """The trigger of the turn whose prompt reached the pane last, if it had one."""
        return self._sent_triggers.get(thread_id)

    async def clear_trigger_message(self, thread_id: int, *, only: Iterable[int]) -> None:
        """Forget the trigger messages *only* for *thread_id* now — #1234 / #1262.

        For a turn that starts without a message of its own (``/skill``): the
        record would otherwise still name an earlier turn's message, and after a
        restart its author — who did not ask for this turn — is who gets pinged
        (#1212). With nothing on record the ping falls back to the owner, and
        the #115 reply is sent unquoted. A pending first-turn trigger among them
        is dropped too (#1235). Exact ids only: a message whose turn has not
        reached Claude yet keeps its trigger.

        When the database refuses the write (locked), the ids are kept: the
        thread's next :meth:`save` forgets them, :meth:`get` hides them until
        then, and the error is raised for the caller to log. (Lost if the process
        stops before that save — a known limit, #1262.)
        """
        among = frozenset(only)
        if not among:
            return
        if self._pending_triggers.get(thread_id) in among:
            del self._pending_triggers[thread_id]
        if self._unwritten_triggers.get(thread_id) in among:
            self._unwritten_triggers[thread_id] = None  # #1254
        waiting = self._unapplied_clears.get(thread_id, frozenset())
        self._unapplied_clears[thread_id] = waiting | among
        while len(self._unapplied_clears) > 1000:
            self._unapplied_clears.pop(next(iter(self._unapplied_clears)))
        # <<REDACTED 1 lines>>
            await self._execute_clear(db, thread_id, among)
            await db.commit()
        left = self._unapplied_clears.get(thread_id, frozenset()) - among
        if left:
            self._unapplied_clears[thread_id] = left
        else:
            self._unapplied_clears.pop(thread_id, None)

    async def restore_sent_trigger(self, thread_id: int, *, replacing: int) -> int | None:
        """Put the last delivered turn's trigger back in place of *replacing* — #1270.

        For a message turn whose prompt never reached the pane: it owns no
        answer, and the turn before it (:meth:`note_trigger_sent`) is still the
        one the pane answers. The row gets that turn's trigger only while it
        still holds *replacing* (nothing newer was recorded). Returns the
        trigger put back, or ``None`` when there was none to put back — no
        delivered turn, or a ``/skill`` reached the pane after it — and the
        caller clears *replacing* instead. Raises when the database refuses.
        """
        sent = self._sent_triggers.get(thread_id)
        if sent is None or sent == replacing:
            return None
        if self._unwritten_triggers.get(thread_id) == replacing:
            self._unwritten_triggers[thread_id] = sent  # #1254: not written yet either
        # <<REDACTED 2 lines>>
                "UPDATE sessions SET trigger_message_id = ?"
                " WHERE thread_id = ? AND trigger_message_id = ?",
                (sent, thread_id, replacing),
            )
            await db.commit()
        return sent

    def clear_is_waiting(self, thread_id: int, message_id: int) -> bool:
        """Whether *message_id* is a refused clear still waiting to land (#1262)."""
        return message_id in self._unapplied_clears.get(thread_id, ())

    async def flush_unapplied_clears(self) -> None:
        """Try to land every refused clear now — at shutdown (#1262).

        They are held in memory, so a process that stops with one waiting would
        bring the forgotten trigger back after the restart. Exact ids only; one
        that still fails is logged and left (the next process cannot know it).
        A refused trigger write (#1254) is written first, the same way.
        """
        for thread_id, value in list(self._unwritten_triggers.items()):
            try:
                # <<REDACTED 2 lines>>
                        "UPDATE sessions SET trigger_message_id = ? WHERE thread_id = ?",
                        (value, thread_id),
                    )
                    await db.commit()
            except Exception:
                logger.warning(
                    "%s could not write the trigger message %s at shutdown (#1254)",
                    log_ctx(thread_id=thread_id),
                    value,
                    exc_info=True,
                )
                continue
            if self._unwritten_triggers.get(thread_id, _NOTHING) == value:
                del self._unwritten_triggers[thread_id]
        for thread_id, among in list(self._unapplied_clears.items()):
            try:
                # <<REDACTED 1 lines>>
                    await self._execute_clear(db, thread_id, among)
                    await db.commit()
            except Exception:
                logger.warning(
                    "%s could not forget the trigger message(s) %s at shutdown (#1262)",
                    log_ctx(thread_id=thread_id),
                    sorted(among),
                    exc_info=True,
                )
                continue
            if self._unapplied_clears.get(thread_id) == among:
                del self._unapplied_clears[thread_id]

    @staticmethod
    async def _execute_clear(
        db: aiosqlite.Connection, thread_id: int, among: frozenset[int]
    ) -> None:
        values = sorted(among)
        marks = ", ".join("?" * len(values))
        await db.execute(
            "UPDATE sessions SET trigger_message_id = NULL"
            f" WHERE thread_id = ? AND trigger_message_id IN ({marks})",
            (thread_id, *values),
        )

    def discard_pending_trigger(self, thread_id: int, message_id: int) -> None:
        """Drop *message_id* if it is still waiting for *thread_id*'s row — #1235.

        Called when the turn it triggered is over. A turn that ended before its
        SYSTEM event never made the row, and the next row ``save`` makes belongs
        to some other turn (a ``/skill``, a scheduled run) — it must not carry
        this message. Only *message_id* itself is dropped: a newer turn's trigger
        that replaced it stays.
        """
        if self._pending_triggers.get(thread_id) == message_id:
            del self._pending_triggers[thread_id]

    async def set_mirror_replied_uuid(self, thread_id: int, uuid: str) -> None:
        """Persist the uuid of the last final answer the mirror delivered.

        Used by TranscriptMirror (live) and TranscriptMirrorCog (restart
        recovery) to dedupe re-delivery of a dropped final answer (Issue #215).
        """
        # <<REDACTED 2 lines>>
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
        # <<REDACTED 2 lines>>
                "UPDATE sessions SET rename_backoff_until = ? WHERE thread_id = ?",
                (deadline, thread_id),
            )
            await db.commit()

    async def list_alive(self) -> list[SessionRecord]:
        """List sessions whose state is 'alive'."""
        # <<REDACTED 3 lines>>
                f"SELECT * FROM sessions WHERE state = 'alive' AND {_LIVE} "
                "ORDER BY last_used_at DESC"
            # <<REDACTED 2 lines>>
            return [_record(row) for row in rows]

    async def open_thread_ids(self) -> set[int]:
        """Thread ids whose workspace is **not** stopped — the live ones.

        The mirror image of :meth:`all_working_dirs`: the #685 sweep decides what
        to archive by *name* (``[停止]``), and reads this set purely to hold back.
        A reopen renames and un-archives in one PATCH, and when the rename half
        loses its ~2-per-10-minutes allowance the retry re-applies only the
        archive flag (``session_close._build_and_apply``) — leaving a **live**
        workspace still called ``[停止]``. Name alone would then archive the
        thread someone is working in; the name is decoration and the row is
        where the state actually lives, so the row wins.

        Only rows are listed. A thread with no row at all is invisible here, and
        must stay archivable by name: 7 of the 186 threads found on 2026-09-04
        had lost their row, and a DB-driven sweep would strand them forever.
        # <<REDACTED 3 lines>>
                "SELECT thread_id FROM sessions WHERE closed_at IS NULL OR TRIM(closed_at) = ''"
            # <<REDACTED 2 lines>>
            return {int(row[0]) for row in rows if row[0] is not None}

    async def all_working_dirs(self) -> set[str]:
        """Every ``working_dir`` any row still claims — closed rows included.

        The orphan sweep (#613) decides what to delete by *absence* from this
        set, so it must err towards listing too much. A stopped workspace is
        still someone's checkout; filtering by state here would hand it to the
        sweep as an orphan.

        Swept tombstones (#818) are the exception: they claim nothing. Before
        #818 their rows were deleted, so a directory the 30-day sweep kept
        (uncommitted work) was already an orphan to #613 — keeping the row as
        history must not quietly change that.
        # <<REDACTED 3 lines>>
                f"SELECT working_dir FROM sessions WHERE working_dir IS NOT NULL AND {_LIVE}"
            # <<REDACTED 2 lines>>
            return {row[0] for row in rows if row[0]}

    async def cleanup_old(self, days: int = 30) -> list[SessionRecord]:
        """Mark sessions unused for N days as swept. Returns the rows it marked.

        #818: the row is **kept** as a tombstone (:data:`SWEPT_REASON` in
        ``closed_reason``, the time in ``closed_at``) rather than deleted. It is
        the only link from a thread to its ``working_dir`` and session id, and
        yousan wants that link to survive: 「スレッドと過去にあったであろう
        セッションの紐付けを、調べようとしたら調べられる」. The heavy part — the
        session dir — is still the caller's to remove.

        Returns the rows rather than a count (#554) because the caller has to
        tell each of those threads what happened, and a count names no thread.
        They are read inside the same transaction as the UPDATE and with the same
        predicate, so the list is exactly what was swept. Rows already swept are
        excluded, or every restart would announce the same sweep again.
        """
        where = f" WHERE julianday('now', 'localtime') - julianday(last_used_at) >= ? AND {_LIVE}"
        # <<REDACTED 2 lines>>
            cursor = await db.execute("SELECT * FROM sessions" + where, (days,))
            swept = [_record(row) for row in await cursor.fetchall()]
            if not swept:
                return []
            await db.execute(
                "UPDATE sessions SET closed_at = datetime('now', 'localtime'), "
                "closed_reason = ?" + where,
                (SWEPT_REASON, days),
            )
            # <<REDACTED 1 lines>>
            return swept
