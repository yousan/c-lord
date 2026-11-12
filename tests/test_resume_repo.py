"""``PendingResumeRepository`` — the marks a restart leaves for the next process.

One mark per thread, good for a few minutes. Reading the marks throws away the
ones that have gone stale (a restart that never came back) and leaves the rest
for the caller to delete as it handles them.
"""

from __future__ import annotations

import logging
import os
import re
import time
import zoneinfo
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from c_lord.database.models import init_db
from c_lord.database.resume_repo import (
    DEFAULT_TTL_MINUTES,
    PendingResume,
    PendingResumeRepository,
)

LOCAL_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
STAMP = "%Y-%m-%d %H:%M:%S"


@pytest.fixture
async def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "sessions.db")
    await init_db(path)
    return path


@pytest.fixture
def marks(db_path: str) -> PendingResumeRepository:
    return PendingResumeRepository(db_path)


async def _run(marks: PendingResumeRepository, sql: str, params: tuple = ()) -> None:
    async with aiosqlite.connect(marks._db_path) as db:
        await db.execute(sql, params)
        await db.commit()


async def _age(marks: PendingResumeRepository, thread_id: int, modifier: str) -> None:
    """Move a mark's ``created_at`` relative to SQLite's own local clock."""
    await _run(
        marks,
        "UPDATE pending_resumes SET created_at = datetime('now', 'localtime', ?) "
        "WHERE thread_id = ?",
        (modifier, thread_id),
    )


async def _threads_in_table(marks: PendingResumeRepository) -> list[int]:
    async with aiosqlite.connect(marks._db_path) as db:
        cursor = await db.execute("SELECT thread_id FROM pending_resumes ORDER BY thread_id")
        return [row[0] for row in await cursor.fetchall()]


class TestMark:
    async def test_a_marked_thread_is_pending(self, marks: PendingResumeRepository) -> None:
        row_id = await marks.mark(1001)

        (pending,) = await marks.get_pending()

        assert isinstance(row_id, int) and row_id > 0
        assert pending.id == row_id
        assert pending.thread_id == 1001

    async def test_a_bare_mark_gets_the_documented_defaults(
        self, marks: PendingResumeRepository
    ) -> None:
        await marks.mark(1001)

        (pending,) = await marks.get_pending()

        assert isinstance(pending, PendingResume)
        assert pending.session_id is None
        assert pending.reason == "self_restart"
        assert pending.resume_prompt is None
        assert LOCAL_TIMESTAMP.fullmatch(pending.created_at)

    async def test_everything_given_is_kept(self, marks: PendingResumeRepository) -> None:
        await marks.mark(
            2002,
            session_id="0f3c-aa11",
            reason="auto_upgrade",
            resume_prompt="pick up where you left off",
        )

        (pending,) = await marks.get_pending()

        assert pending.thread_id == 2002
        assert pending.session_id == "0f3c-aa11"
        assert pending.reason == "auto_upgrade"
        assert pending.resume_prompt == "pick up where you left off"

    async def test_marking_a_thread_again_replaces_its_mark(
        self, marks: PendingResumeRepository
    ) -> None:
        """At most one mark per thread — the thread must not be resumed twice."""
        first = await marks.mark(3003, session_id="old", resume_prompt="old words")
        second = await marks.mark(3003, session_id="new", reason="manual")

        (pending,) = await marks.get_pending()

        assert second != first
        assert pending.id == second
        assert (pending.session_id, pending.reason) == ("new", "manual")
        # Replaced, not merged: what the first mark said is gone.
        assert pending.resume_prompt is None

    async def test_re_marking_restarts_the_clock(self, marks: PendingResumeRepository) -> None:
        await marks.mark(3003)
        await _age(marks, 3003, "-4 minutes")

        await marks.mark(3003)

        async with aiosqlite.connect(marks._db_path) as db:
            cursor = await db.execute(
                "SELECT created_at > datetime('now', 'localtime', '-1 minutes') "
                "FROM pending_resumes WHERE thread_id = 3003"
            )
            assert await cursor.fetchone() == (1,)
        assert [m.thread_id for m in await marks.get_pending()] == [3003]

    async def test_logs_the_mark(
        self, marks: PendingResumeRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="c_lord.database.resume_repo"):
            row_id = await marks.mark(4004, reason="deploy")

        assert f"Marked thread 4004 for resume (reason=deploy, row_id={row_id})" in caplog.text


class TestGetPending:
    async def test_nothing_marked_is_an_empty_list(self, marks: PendingResumeRepository) -> None:
        assert await marks.get_pending() == []

    async def test_every_fresh_mark_comes_back_oldest_first(
        self, marks: PendingResumeRepository
    ) -> None:
        for thread_id, age in ((10, "-1 minutes"), (20, "-3 minutes"), (30, "-2 minutes")):
            await marks.mark(thread_id)
            await _age(marks, thread_id, age)

        pending = await marks.get_pending()

        assert [m.thread_id for m in pending] == [20, 30, 10]

    async def test_reading_does_not_consume_the_marks(self, marks: PendingResumeRepository) -> None:
        """The caller deletes each mark as it resumes the thread; a crash in
        between must leave the rest to be found again."""
        await marks.mark(10)

        await marks.get_pending()

        assert [m.thread_id for m in await marks.get_pending()] == [10]

    async def test_a_stale_mark_is_dropped_from_the_table(
        self, marks: PendingResumeRepository
    ) -> None:
        await marks.mark(10)
        await marks.mark(20)
        await _age(marks, 10, f"-{DEFAULT_TTL_MINUTES + 1} minutes")

        pending = await marks.get_pending()

        assert [m.thread_id for m in pending] == [20]
        assert await _threads_in_table(marks) == [20]

    async def test_the_default_lifetime_is_five_minutes(
        self, marks: PendingResumeRepository
    ) -> None:
        await marks.mark(10)
        await marks.mark(20)
        await _age(marks, 10, "-4 minutes")
        await _age(marks, 20, "-6 minutes")

        assert DEFAULT_TTL_MINUTES == 5
        assert [m.thread_id for m in await marks.get_pending()] == [10]

    async def test_a_mark_exactly_at_the_limit_is_stale(
        self, marks: PendingResumeRepository
    ) -> None:
        """The limit itself is already too old (``<=``, not ``<``)."""
        await marks.mark(10)
        await _age(marks, 10, f"-{DEFAULT_TTL_MINUTES} minutes")

        assert await marks.get_pending() == []

    async def test_the_lifetime_can_be_set(self, db_path: str) -> None:
        patient = PendingResumeRepository(db_path, ttl_minutes=60)
        await patient.mark(10)
        await patient.mark(20)
        await _age(patient, 10, "-30 minutes")
        await _age(patient, 20, "-90 minutes")

        assert [m.thread_id for m in await patient.get_pending()] == [10]

    async def test_a_lifetime_of_zero_keeps_nothing(self, db_path: str) -> None:
        impatient = PendingResumeRepository(db_path, ttl_minutes=0)
        await impatient.mark(10)

        assert await impatient.get_pending() == []
        assert await _threads_in_table(impatient) == []


@pytest.fixture
def new_york_clock() -> Iterator[zoneinfo.ZoneInfo]:
    """Run with the process clock in a zone that has daylight saving time."""
    try:
        zone = zoneinfo.ZoneInfo("America/New_York")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone database on this machine")
    before = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        yield zone
    finally:
        if before is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = before
        time.tzset()


class TestExpiryAcrossDaylightSaving:
    async def test_the_cut_off_is_the_wall_clock_time_ttl_minutes_ago(
        self, db_path: str, new_york_clock: zoneinfo.ZoneInfo
    ) -> None:
        """The TTL is taken off the clock *first* and the result is then put
        into local time — not the other way round.

        The two orders agree until a daylight-saving change lies between "now"
        and the cut-off; then "local time minus N minutes" is an hour off the
        wall-clock time it actually was N minutes ago. Measured here with a TTL
        long enough to reach back over the last change: a mark half an hour
        older than the true cut-off must go and one half an hour younger must
        stay. Either mistake of an hour gets one of the two wrong.
        """
        day = 86400.0
        now = time.time()

        def offset(at: float) -> timedelta | None:
            return datetime.fromtimestamp(at, new_york_clock).utcoffset()

        back = day
        while offset(now - back) == offset(now):
            back += day
            assert back < 370 * day, "this zone never changes its offset"
        back += 3 * day  # well clear of the change itself
        ttl_minutes = int(back // 60)
        cut_off = datetime.fromtimestamp(now - ttl_minutes * 60, new_york_clock)

        marks = PendingResumeRepository(db_path, ttl_minutes=ttl_minutes)
        for thread_id, created in (
            (1, cut_off - timedelta(minutes=30)),
            (2, cut_off + timedelta(minutes=30)),
        ):
            await _run(
                marks,
                "INSERT INTO pending_resumes (thread_id, created_at) VALUES (?, ?)",
                (thread_id, created.strftime(STAMP)),
            )

        assert [m.thread_id for m in await marks.get_pending()] == [2]


class TestDelete:
    async def test_removes_the_mark_with_that_id(self, marks: PendingResumeRepository) -> None:
        handled = await marks.mark(10)
        await marks.mark(20)

        await marks.delete(handled)

        assert [m.thread_id for m in await marks.get_pending()] == [20]

    async def test_an_id_that_is_not_there_is_not_an_error(
        self, marks: PendingResumeRepository
    ) -> None:
        await marks.mark(10)

        await marks.delete(987654)

        assert [m.thread_id for m in await marks.get_pending()] == [10]

    async def test_the_id_is_not_mistaken_for_a_thread(
        self, marks: PendingResumeRepository
    ) -> None:
        row_id = await marks.mark(555)

        await marks.delete(555)  # a thread id, not this mark's row id

        assert row_id != 555
        assert [m.thread_id for m in await marks.get_pending()] == [555]


class TestDeleteByThread:
    async def test_removes_that_threads_mark_only(self, marks: PendingResumeRepository) -> None:
        await marks.mark(10)
        await marks.mark(20)

        await marks.delete_by_thread(10)

        assert [m.thread_id for m in await marks.get_pending()] == [20]

    async def test_a_thread_without_a_mark_is_not_an_error(
        self, marks: PendingResumeRepository
    ) -> None:
        await marks.mark(10)

        await marks.delete_by_thread(999)

        assert [m.thread_id for m in await marks.get_pending()] == [10]
