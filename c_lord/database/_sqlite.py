"""The one way the repositories in this package talk to SQLite (#1317).

No repository keeps a connection. Each operation opens the database file with
aiosqlite's defaults, does its work and closes it again, so:

* a write that meets a lock held by someone else **raises** — nothing here
  waits longer than SQLite's default, retries or swallows the error. The
  trigger bookkeeping in :mod:`.repository` (#1254 / #1262) is built on that;
* a write is committed before the helper returns; a read never commits.

``aiosqlite.connect`` is looked up at call time, on purpose: tests swap it out
to stage a refused connection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, NamedTuple

import aiosqlite

Params = Sequence[Any]


class Written(NamedTuple):
    """What a committed statement left behind, read before the connection closed."""

    rowcount: int
    lastrowid: int | None

    @property
    def changed(self) -> bool:
        """Whether the statement touched at least one row."""
        return self.rowcount > 0


@asynccontextmanager
async def connection(db_path: str, *, named: bool = False) -> AsyncIterator[aiosqlite.Connection]:
    """A connection for the length of one operation.

    *named* makes rows addressable by column name (``aiosqlite.Row``); without it
    they are plain tuples. For operations that need several statements in one
    transaction — the caller commits.
    """
    async with aiosqlite.connect(db_path) as db:
        if named:
            db.row_factory = aiosqlite.Row
        yield db


async def write(db_path: str, sql: str, params: Params = ()) -> Written:
    """Run one statement and commit it."""
    async with connection(db_path) as db:
        cursor = await db.execute(sql, params)
        await db.commit()
        return Written(cursor.rowcount, cursor.lastrowid)


async def run_script(db_path: str, script: str) -> None:
    """Run a multi-statement script (a schema) and commit it."""
    async with connection(db_path) as db:
        await db.executescript(script)
        await db.commit()


async def read_all(
    db_path: str, sql: str, params: Params = (), *, named: bool = False
) -> list[aiosqlite.Row]:
    """Every row of one query. Nothing is committed."""
    async with connection(db_path, named=named) as db:
        cursor = await db.execute(sql, params)
        return list(await cursor.fetchall())


async def read_one(
    db_path: str, sql: str, params: Params = (), *, named: bool = False
) -> aiosqlite.Row | None:
    """The first row of one query, or ``None``. Nothing is committed."""
    async with connection(db_path, named=named) as db:
        cursor = await db.execute(sql, params)
        return await cursor.fetchone()


async def read_value(db_path: str, sql: str, params: Params = (), *, default: Any = None) -> Any:
    """The first column of the first row, or *default* when there is no row."""
    row = await read_one(db_path, sql, params)
    return default if row is None else row[0]


async def read_dicts(db_path: str, sql: str, params: Params = ()) -> list[dict[str, Any]]:
    """Every row of one query as a ``{column: value}`` dict — whatever columns
    the query returns, so a ``SELECT *`` follows the table as it grows."""
    return [dict(row) for row in await read_all(db_path, sql, params, named=True)]


# -- statements built from names ------------------------------------------------
#
# Table and column names below are always constants of the calling module; values
# only ever travel as parameters.


def insert_statement(table: str, columns: Iterable[str], *, replace: bool = False) -> str:
    """``INSERT [OR REPLACE] INTO table (columns…) VALUES (?…)``."""
    names = tuple(columns)
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    marks = ", ".join("?" for _ in names)
    return f"{verb} INTO {table} ({', '.join(names)}) VALUES ({marks})"


async def insert(
    db_path: str, table: str, values: Mapping[str, Any], *, replace: bool = False
) -> int:
    """Insert one row from ``{column: value}``, commit, and return its rowid
    (``0`` should SQLite report none)."""
    done = await write(
        db_path, insert_statement(table, values, replace=replace), tuple(values.values())
    )
    return done.lastrowid or 0


async def update(
    db_path: str, table: str, changes: Mapping[str, Any], *, where: str, params: Params
) -> Written:
    """Set ``{column: value}`` on the rows matching *where*, and commit."""
    assignments = ", ".join(f"{column} = ?" for column in changes)
    return await write(
        db_path,
        f"UPDATE {table} SET {assignments} WHERE {where}",
        (*changes.values(), *params),
    )
