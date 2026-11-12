"""Scheduled tasks (the ``scheduled_tasks`` table).

What the REST API registers and :class:`~c_lord.cogs.scheduler.SchedulerCog`
runs. c-lord only keeps *when*: the prompt is the whole of *what*. Times in this
table are Unix epoch seconds (``time.time()``), unlike the local-time strings of
the session database — the table lives in a database file of its own and is
created by :meth:`TaskRepository.init_db`, not by ``models.init_db``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import _sqlite

logger = logging.getLogger(__name__)

TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,
    prompt           TEXT    NOT NULL,
    interval_seconds INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    working_dir      TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    next_run_at      REAL    NOT NULL,
    last_run_at      REAL,
    created_at       REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_next_run ON scheduled_tasks(next_run_at, enabled);
"""

_TABLE = "scheduled_tasks"
_BY_ID = "id = ?"

# Columns whose stored form differs from the one callers work with.
_FROM_DB = {"enabled": bool}


def _flag(value: object) -> int:
    """Any truthy value is stored as 1 — ``get_due`` matches ``enabled = 1`` exactly."""
    return 1 if value else 0


_TO_DB = {"enabled": _flag}


def _converted(values: dict[str, Any], how: dict[str, Any]) -> dict[str, Any]:
    """*values* with the columns named in *how* converted, the rest untouched."""
    return {name: how[name](value) if name in how else value for name, value in values.items()}


def _given(**fields: Any) -> dict[str, Any]:
    """The fields a caller actually passed — ``None`` means "leave it alone".

    The keys are the keyword names written at the call site, which is what keeps
    column names out of the hands of whoever supplies the values.
    """
    return {name: value for name, value in fields.items() if value is not None}


class TaskRepository:
    """Async CRUD for scheduled tasks."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init_db(self) -> None:
        """Create the table and its index if they are not there yet."""
        await _sqlite.run_script(self.db_path, TASK_SCHEMA)
        logger.info("Task DB initialized at %s", self.db_path)

    async def _db_execute(self, sql: str, params: tuple = ()) -> None:
        """Run one statement as given and commit it."""
        await _sqlite.write(self.db_path, sql, params)

    async def _tasks(self, tail: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Tasks selected by *tail*, every column, in the caller's form."""
        stored = await _sqlite.read_dicts(self.db_path, f"SELECT * FROM {_TABLE} {tail}", params)
        return [_converted(task, _FROM_DB) for task in stored]

    async def _set(self, task_id: int, **columns: Any) -> bool:
        """Write *columns* on one task. ``True`` when the task exists."""
        changes = _converted(columns, _TO_DB)
        done = await _sqlite.update(self.db_path, _TABLE, changes, where=_BY_ID, params=(task_id,))
        return done.changed

    async def get(self, task_id: int) -> dict | None:
        """One task by id, or ``None``."""
        return next(iter(await self._tasks(f"WHERE {_BY_ID}", (task_id,))), None)

    async def get_all(self) -> list[dict]:
        """Every task, enabled or not, oldest first."""
        return await self._tasks("ORDER BY created_at")

    async def get_due(self, now: float | None = None) -> list[dict]:
        """Enabled tasks whose time has come (``next_run_at`` up to and including
        *now*), most overdue first. *now* defaults to the current time; ``0.0``
        is a time like any other."""
        at = time.time() if now is None else now
        return await self._tasks(
            "WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at", (at,)
        )

    async def create(
        self,
        name: str,
        prompt: str,
        interval_seconds: int,
        channel_id: int,
        *,
        working_dir: str | None = None,
        run_immediately: bool = True,
    ) -> int:
        """Register a task and return its id.

        It is due at once, or one interval from now when *run_immediately* is
        false. A name already taken raises SQLite's integrity error — the API
        turns that into a 409.
        """
        created = time.time()
        task_id = await _sqlite.insert(
            self.db_path,
            _TABLE,
            {
                "name": name,
                "prompt": prompt,
                "interval_seconds": interval_seconds,
                "channel_id": channel_id,
                "working_dir": working_dir,
                "next_run_at": created + (0 if run_immediately else interval_seconds),
                "created_at": created,
            },
        )
        logger.info(
            "Scheduled task created: id=%s, name=%s, interval=%ss",
            task_id,
            name,
            interval_seconds,
        )
        return task_id

    async def update_next_run(self, task_id: int, interval_seconds: int) -> None:
        """Note a run that happened now and schedule the next one interval from
        **now** (not from when it was due)."""
        ran = time.time()
        await self._set(task_id, last_run_at=ran, next_run_at=ran + interval_seconds)

    async def delete(self, task_id: int) -> bool:
        """Remove a task. ``True`` when it existed."""
        done = await _sqlite.write(self.db_path, f"DELETE FROM {_TABLE} WHERE {_BY_ID}", (task_id,))
        return done.changed

    async def set_enabled(self, task_id: int, *, enabled: bool) -> bool:
        """Switch a task on or off. ``True`` when the task exists."""
        return await self._set(task_id, enabled=enabled)

    async def update(
        self,
        task_id: int,
        *,
        prompt: str | None = None,
        interval_seconds: int | None = None,
        working_dir: str | None = None,
    ) -> bool:
        """Change the given fields of a task; ``None`` leaves a field alone.

        With nothing to change the database is not touched and the answer is
        ``False``; otherwise ``True`` when the task exists. The next run time is
        not moved.
        """
        changes = _given(prompt=prompt, interval_seconds=interval_seconds, working_dir=working_dir)
        return bool(changes) and await self._set(task_id, **changes)
