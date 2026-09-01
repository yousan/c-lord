"""Persistence for workspace ↔ dev-environment links — Issue #573.

:mod:`c_lord.devenv` can only see containers that still exist, and only answers
"which containers belong to this session dir". The question #573 is actually
about is the reverse one, asked *after* things go wrong: on the production host
the workspace vanished while 12 containers kept running and holding ports
55321-55327, and nothing could say whose they were.

Answering that needs a record written while the link was still observable. This
repository is that record. It is written from discovery, so it never depends on
Claude having remembered anything (#491).

A container that disappears is marked ``gone`` rather than deleted: the port it
used to hold is exactly what someone will be asking about later, so the
ownership record has to outlive the container.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

from ..devenv import DevContainer

logger = logging.getLogger(__name__)

DEVENV_SCHEMA = """
CREATE TABLE IF NOT EXISTS dev_environments (
    thread_id INTEGER NOT NULL,
    container_name TEXT NOT NULL,
    container_id TEXT,
    project TEXT,
    session_dir TEXT,
    ports TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    source TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (thread_id, container_name)
);

CREATE INDEX IF NOT EXISTS idx_devenv_container ON dev_environments(container_name);
CREATE INDEX IF NOT EXISTS idx_devenv_thread ON dev_environments(thread_id);
"""

#: Status stored for a container that discovery no longer sees. Distinct from
#: docker's own ``exited``: ``exited`` means stopped but still present (it can
#: be started again and still holds its name), ``gone`` means removed entirely.
STATUS_GONE = "gone"


@dataclass(frozen=True)
class DevEnvRecord:
    """One remembered container, including ones that no longer exist."""

    thread_id: int
    container_name: str
    container_id: str | None
    project: str | None
    session_dir: str | None
    ports: tuple[int, ...]
    status: str
    source: str | None
    first_seen_at: str
    last_seen_at: str


def _encode_ports(ports: tuple[int, ...]) -> str:
    return ",".join(str(p) for p in ports)


def _decode_ports(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()
    return tuple(int(p) for p in raw.split(",") if p.strip().isdigit())


def _row(r: aiosqlite.Row) -> DevEnvRecord:
    return DevEnvRecord(
        thread_id=r["thread_id"],
        container_name=r["container_name"],
        container_id=r["container_id"],
        project=r["project"],
        session_dir=r["session_dir"],
        ports=_decode_ports(r["ports"]),
        status=r["status"],
        source=r["source"],
        first_seen_at=r["first_seen_at"],
        last_seen_at=r["last_seen_at"],
    )


class DevEnvRepository:
    """Async CRUD for the ``dev_environments`` table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init_db(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(DEVENV_SCHEMA)
            await db.commit()

    async def record(
        self, thread_id: int, session_dir: str, containers: list[DevContainer]
    ) -> None:
        """Store what discovery just saw for *thread_id*.

        Idempotent: discovery runs every turn, so re-recording the same
        container updates its row instead of adding one. Rows for containers
        that were not seen this time are marked :data:`STATUS_GONE` rather than
        deleted — the ownership record is the thing worth keeping.
        """
        seen = [c.name for c in containers]
        async with aiosqlite.connect(self.db_path) as db:
            for c in containers:
                await db.execute(
                    """
                    INSERT INTO dev_environments (
                        thread_id, container_name, container_id, project,
                        session_dir, ports, status, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id, container_name) DO UPDATE SET
                        container_id = excluded.container_id,
                        project      = excluded.project,
                        session_dir  = excluded.session_dir,
                        ports        = excluded.ports,
                        status       = excluded.status,
                        source       = excluded.source,
                        last_seen_at = datetime('now', 'localtime')
                    """,
                    (
                        thread_id,
                        c.name,
                        c.container_id,
                        c.project,
                        session_dir,
                        _encode_ports(c.ports),
                        c.status,
                        c.source,
                    ),
                )

            # Anything previously recorded for this thread but absent now was
            # removed outside c-lord. Keep the row, drop the liveness claim.
            if seen:
                placeholders = ",".join("?" for _ in seen)
                await db.execute(
                    f"UPDATE dev_environments SET status = ? "
                    f"WHERE thread_id = ? AND container_name NOT IN ({placeholders})",
                    (STATUS_GONE, thread_id, *seen),
                )
            else:
                await db.execute(
                    "UPDATE dev_environments SET status = ? WHERE thread_id = ?",
                    (STATUS_GONE, thread_id),
                )
            await db.commit()

    async def for_thread(self, thread_id: int) -> list[DevEnvRecord]:
        """Every container ever recorded for *thread_id*, ``gone`` ones included."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM dev_environments WHERE thread_id = ? ORDER BY container_name",
                (thread_id,),
            ) as cur:
                return [_row(r) for r in await cur.fetchall()]

    async def thread_for_container(self, container_name: str) -> int | None:
        """Which thread started *container_name*, or None if c-lord never saw it."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT thread_id FROM dev_environments WHERE container_name = ? "
                "ORDER BY last_seen_at DESC LIMIT 1",
                (container_name,),
            ) as cur:
                row = await cur.fetchone()
                return int(row["thread_id"]) if row else None

    async def thread_for_port(self, port: int) -> int | None:
        """Which thread is holding (or last held) host *port*.

        Ports are stored as a comma-joined list, so this scans rather than
        indexes — the table has one row per container, which stays small.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT thread_id, ports FROM dev_environments ORDER BY last_seen_at DESC"
            ) as cur:
                for row in await cur.fetchall():
                    if port in _decode_ports(row["ports"]):
                        return int(row["thread_id"])
        return None

    async def all_records(self) -> list[DevEnvRecord]:
        """Every remembered container across all threads."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM dev_environments ORDER BY thread_id, container_name"
            ) as cur:
                return [_row(r) for r in await cur.fetchall()]
