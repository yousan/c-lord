"""``TaskRepository`` — the ``scheduled_tasks`` table behind the scheduler.

What the REST API and ``SchedulerCog`` rely on: a task is due from the moment
it is created (or one interval later), ``get_due`` hands out enabled tasks only
and includes the boundary, ``enabled`` is stored as exactly 1 or 0 whatever
truthy / falsy value came in, and ``update`` treats ``None`` — and nothing
else — as "not given".
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from c_lord.database.task_repo import TaskRepository

CHANNEL = 4242


@pytest.fixture
async def tasks(tmp_path: Path) -> TaskRepository:
    repository = TaskRepository(str(tmp_path / "tasks.db"))
    await repository.init_db()
    return repository


async def _add(tasks: TaskRepository, name: str = "nightly", **overrides: Any) -> int:
    """Register a task with unremarkable values; *overrides* replace any of them."""
    values: dict[str, Any] = {"prompt": "check the build", "interval_seconds": 600}
    values.update(overrides)
    interval = values.pop("interval_seconds")
    prompt = values.pop("prompt")
    return await tasks.create(name, prompt, interval, CHANNEL, **values)


async def _stored(tasks: TaskRepository, task_id: int, column: str) -> Any:
    """One column of one task exactly as SQLite holds it (no conversion)."""
    async with aiosqlite.connect(tasks.db_path) as db:
        cursor = await db.execute(f"SELECT {column} FROM scheduled_tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def _schedule_at(tasks: TaskRepository, task_id: int, when: float) -> None:
    await tasks._db_execute(
        "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?", (when, task_id)
    )


class TestInitDb:
    async def test_can_be_run_again_without_losing_tasks(self, tasks: TaskRepository) -> None:
        task_id = await _add(tasks)

        await tasks.init_db()

        assert await tasks.get(task_id) is not None

    async def test_says_where_the_database_is(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = str(tmp_path / "fresh.db")

        with caplog.at_level(logging.INFO, logger="c_lord.database.task_repo"):
            await TaskRepository(path).init_db()

        assert f"Task DB initialized at {path}" in caplog.text


class TestCreate:
    async def test_returns_the_id_the_task_can_be_read_back_by(self, tasks: TaskRepository) -> None:
        task_id = await _add(tasks, "weekly", prompt="tidy up", interval_seconds=120)

        task = await tasks.get(task_id)

        assert isinstance(task_id, int) and task_id > 0
        assert task is not None
        assert (task["id"], task["name"], task["prompt"]) == (task_id, "weekly", "tidy up")
        assert (task["interval_seconds"], task["channel_id"]) == (120, CHANNEL)

    async def test_ids_grow_with_each_task(self, tasks: TaskRepository) -> None:
        first = await _add(tasks, "a")
        second = await _add(tasks, "b")

        assert second > first

    async def test_a_new_task_is_enabled_never_run_and_has_no_working_dir(
        self, tasks: TaskRepository
    ) -> None:
        task = await tasks.get(await _add(tasks))

        assert task is not None
        assert task["enabled"] is True
        assert task["last_run_at"] is None
        assert task["working_dir"] is None

    async def test_is_due_at_once_by_default(self, tasks: TaskRepository) -> None:
        before = time.time()
        task = await tasks.get(await _add(tasks))
        after = time.time()

        assert task is not None
        assert before <= task["next_run_at"] <= after
        assert task["next_run_at"] == task["created_at"]

    async def test_without_run_immediately_the_first_run_is_one_interval_away(
        self, tasks: TaskRepository
    ) -> None:
        task = await tasks.get(await _add(tasks, interval_seconds=900, run_immediately=False))

        assert task is not None
        assert task["next_run_at"] == pytest.approx(task["created_at"] + 900)
        assert task["next_run_at"] > time.time() + 800

    async def test_keeps_the_working_dir_it_was_given(self, tasks: TaskRepository) -> None:
        task = await tasks.get(await _add(tasks, working_dir="/srv/project"))

        assert task is not None
        assert task["working_dir"] == "/srv/project"

    async def test_a_name_already_taken_is_refused_by_sqlite(self, tasks: TaskRepository) -> None:
        """The API turns this exception into a 409 — it must not be swallowed here."""
        await _add(tasks, "only-one")

        with pytest.raises(sqlite3.IntegrityError):
            await _add(tasks, "only-one")

        assert len(await tasks.get_all()) == 1

    async def test_logs_the_new_task(
        self, tasks: TaskRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="c_lord.database.task_repo"):
            task_id = await _add(tasks, "logged", interval_seconds=77)

        assert f"Scheduled task created: id={task_id}, name=logged, interval=77s" in caplog.text


class TestGet:
    async def test_unknown_id_is_none(self, tasks: TaskRepository) -> None:
        assert await tasks.get(31337) is None

    async def test_times_come_back_as_numbers_and_enabled_as_a_bool(
        self, tasks: TaskRepository
    ) -> None:
        task = await tasks.get(await _add(tasks))

        assert task is not None
        assert isinstance(task["next_run_at"], float)
        assert isinstance(task["created_at"], float)
        assert isinstance(task["enabled"], bool)


class TestGetAll:
    async def test_no_tasks_is_an_empty_list(self, tasks: TaskRepository) -> None:
        assert await tasks.get_all() == []

    async def test_lists_every_task_oldest_first(self, tasks: TaskRepository) -> None:
        ids = [await _add(tasks, name) for name in ("first", "second", "third")]
        # Creation order by the clock, not by id: make the middle one the oldest.
        await tasks._db_execute(
            "UPDATE scheduled_tasks SET created_at = 1.0 WHERE id = ?", (ids[1],)
        )

        listed = await tasks.get_all()

        assert [task["name"] for task in listed] == ["second", "first", "third"]

    async def test_a_disabled_task_is_still_listed_and_says_so(self, tasks: TaskRepository) -> None:
        on = await _add(tasks, "on")
        off = await _add(tasks, "off")
        await tasks.set_enabled(off, enabled=False)

        by_id = {task["id"]: task["enabled"] for task in await tasks.get_all()}

        assert by_id == {on: True, off: False}


class TestGetDue:
    async def test_nothing_is_due_while_every_task_lies_ahead(self, tasks: TaskRepository) -> None:
        await _add(tasks, "later", run_immediately=False)

        assert await tasks.get_due() == []

    async def test_a_task_whose_time_has_passed_is_due(self, tasks: TaskRepository) -> None:
        overdue = await _add(tasks, "overdue")
        await _add(tasks, "later", run_immediately=False)
        await _schedule_at(tasks, overdue, time.time() - 30)

        assert [task["id"] for task in await tasks.get_due()] == [overdue]

    async def test_a_task_scheduled_for_exactly_now_is_due(self, tasks: TaskRepository) -> None:
        """The comparison includes the boundary: ``next_run_at == now`` runs."""
        task_id = await _add(tasks)
        await _schedule_at(tasks, task_id, 5000.0)

        assert [task["id"] for task in await tasks.get_due(now=5000.0)] == [task_id]
        assert await tasks.get_due(now=4999.999) == []

    async def test_the_most_overdue_task_comes_first(self, tasks: TaskRepository) -> None:
        late = await _add(tasks, "late")
        later = await _add(tasks, "later")
        latest = await _add(tasks, "latest")
        for task_id, when in ((late, 300.0), (later, 100.0), (latest, 200.0)):
            await _schedule_at(tasks, task_id, when)

        assert [task["id"] for task in await tasks.get_due(now=1000.0)] == [later, latest, late]

    async def test_a_disabled_task_is_never_due(self, tasks: TaskRepository) -> None:
        task_id = await _add(tasks)
        await tasks.set_enabled(task_id, enabled=False)

        assert await tasks.get_due(now=time.time() + 3600) == []

    async def test_now_zero_is_a_time_not_a_missing_argument(self, tasks: TaskRepository) -> None:
        """``get_due(now=0.0)`` asks about the epoch; it must not fall back to
        the current time just because ``0.0`` is falsy."""
        await _add(tasks, "due-today")
        at_epoch = await _add(tasks, "due-at-epoch")
        await _schedule_at(tasks, at_epoch, 0.0)

        assert [task["id"] for task in await tasks.get_due(now=0.0)] == [at_epoch]

    async def test_without_now_the_current_time_is_used(self, tasks: TaskRepository) -> None:
        past = await _add(tasks, "past")
        future = await _add(tasks, "future")
        await _schedule_at(tasks, past, time.time() - 1)
        await _schedule_at(tasks, future, time.time() + 3600)

        assert [task["id"] for task in await tasks.get_due(None)] == [past]

    async def test_due_tasks_carry_enabled_as_a_bool(self, tasks: TaskRepository) -> None:
        await _add(tasks)

        (task,) = await tasks.get_due()

        assert task["enabled"] is True


class TestUpdateNextRun:
    async def test_records_the_run_and_schedules_the_next_from_now(
        self, tasks: TaskRepository
    ) -> None:
        """The next run is one interval from *now*, not from when the task was
        due — a task that ran late does not run again at once to catch up."""
        task_id = await _add(tasks)
        await _schedule_at(tasks, task_id, 10.0)  # due long ago

        before = time.time()
        await tasks.update_next_run(task_id, interval_seconds=250)
        after = time.time()

        task = await tasks.get(task_id)
        assert task is not None
        assert before <= task["last_run_at"] <= after
        assert task["next_run_at"] == pytest.approx(task["last_run_at"] + 250)
        assert await tasks.get_due() == []

    async def test_unknown_task_is_not_an_error(self, tasks: TaskRepository) -> None:
        await tasks.update_next_run(999, interval_seconds=60)

        assert await tasks.get_all() == []


class TestDelete:
    async def test_removes_the_task_and_says_so(self, tasks: TaskRepository) -> None:
        doomed = await _add(tasks, "doomed")
        kept = await _add(tasks, "kept")

        assert await tasks.delete(doomed) is True

        assert await tasks.get(doomed) is None
        assert await tasks.get(kept) is not None

    async def test_unknown_task_is_false(self, tasks: TaskRepository) -> None:
        assert await tasks.delete(999) is False


class TestSetEnabled:
    async def test_switching_off_takes_the_task_out_of_the_due_list(
        self, tasks: TaskRepository
    ) -> None:
        task_id = await _add(tasks)

        assert await tasks.set_enabled(task_id, enabled=False) is True

        task = await tasks.get(task_id)
        assert task is not None and task["enabled"] is False
        assert await tasks.get_due() == []

    async def test_switching_back_on_makes_it_due_again(self, tasks: TaskRepository) -> None:
        task_id = await _add(tasks)
        await tasks.set_enabled(task_id, enabled=False)

        assert await tasks.set_enabled(task_id, enabled=True) is True

        task = await tasks.get(task_id)
        assert task is not None and task["enabled"] is True
        assert [due["id"] for due in await tasks.get_due()] == [task_id]

    async def test_unknown_task_is_false(self, tasks: TaskRepository) -> None:
        assert await tasks.set_enabled(999, enabled=True) is False

    async def test_setting_the_value_it_already_has_still_counts_as_found(
        self, tasks: TaskRepository
    ) -> None:
        task_id = await _add(tasks)

        assert await tasks.set_enabled(task_id, enabled=True) is True

    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            (True, 1),
            (False, 0),
            (2, 1),
            ("0", 1),
            ("yes", 1),
            (None, 0),
            ("", 0),
            (0, 0),
        ],
    )
    async def test_any_value_is_stored_as_exactly_one_or_zero(
        self, tasks: TaskRepository, given: Any, stored: int
    ) -> None:
        """``get_due`` selects ``enabled = 1``. A truthy value stored as itself
        (``2``, ``"0"``) would read back as enabled and yet never run."""
        task_id = await _add(tasks)
        await tasks.set_enabled(task_id, enabled=not stored)  # start from the opposite

        await tasks.set_enabled(task_id, enabled=given)

        assert await _stored(tasks, task_id, "enabled") == stored
        task = await tasks.get(task_id)
        assert task is not None and task["enabled"] is bool(stored)
        assert len(await tasks.get_due()) == stored


class TestUpdate:
    async def test_changes_the_prompt_and_nothing_else(self, tasks: TaskRepository) -> None:
        task_id = await _add(tasks, working_dir="/srv/a")
        before = await tasks.get(task_id)
        assert before is not None

        assert await tasks.update(task_id, prompt="a new instruction") is True

        after = await tasks.get(task_id)
        assert after == {**before, "prompt": "a new instruction"}

    async def test_changes_the_interval_without_moving_the_next_run(
        self, tasks: TaskRepository
    ) -> None:
        task_id = await _add(tasks, interval_seconds=600)
        before = await tasks.get(task_id)
        assert before is not None

        assert await tasks.update(task_id, interval_seconds=30) is True

        after = await tasks.get(task_id)
        assert after == {**before, "interval_seconds": 30}

    async def test_changes_the_working_dir(self, tasks: TaskRepository) -> None:
        task_id = await _add(tasks)

        assert await tasks.update(task_id, working_dir="/srv/b") is True

        assert await _stored(tasks, task_id, "working_dir") == "/srv/b"

    async def test_several_fields_in_one_call(self, tasks: TaskRepository) -> None:
        task_id = await _add(tasks)

        await tasks.update(task_id, prompt="p2", interval_seconds=5, working_dir="/w")

        task = await tasks.get(task_id)
        assert task is not None
        assert (task["prompt"], task["interval_seconds"], task["working_dir"]) == ("p2", 5, "/w")

    async def test_unknown_task_is_false(self, tasks: TaskRepository) -> None:
        assert await tasks.update(999, prompt="nobody home") is False

    async def test_nothing_given_is_false_even_for_a_task_that_exists(
        self, tasks: TaskRepository
    ) -> None:
        task_id = await _add(tasks)
        before = await tasks.get(task_id)

        assert await tasks.update(task_id) is False

        assert await tasks.get(task_id) == before

    async def test_none_leaves_a_field_alone(self, tasks: TaskRepository) -> None:
        """There is no way to clear ``working_dir`` back to NULL through ``update``."""
        task_id = await _add(tasks, working_dir="/srv/keep")

        await tasks.update(task_id, prompt="changed", working_dir=None)

        assert await _stored(tasks, task_id, "working_dir") == "/srv/keep"

    @pytest.mark.parametrize(
        ("field", "falsy"),
        [("prompt", ""), ("interval_seconds", 0), ("working_dir", "")],
    )
    async def test_a_falsy_value_is_a_value_and_is_written(
        self, tasks: TaskRepository, field: str, falsy: Any
    ) -> None:
        """Only ``None`` means "not given": an empty string or ``0`` is stored."""
        task_id = await _add(tasks, working_dir="/srv/a")

        assert await tasks.update(task_id, **{field: falsy}) is True

        assert await _stored(tasks, task_id, field) == falsy


class TestDbExecute:
    async def test_runs_the_statement_and_commits_it(self, tasks: TaskRepository) -> None:
        """``_db_execute`` is the hook other tests use to arrange rows; what it
        writes must be visible to the next connection."""
        task_id = await _add(tasks)

        result = await tasks._db_execute(
            "UPDATE scheduled_tasks SET prompt = ? WHERE id = ?", ("rewritten", task_id)
        )

        assert result is None
        assert await _stored(tasks, task_id, "prompt") == "rewritten"

    async def test_parameters_are_optional(self, tasks: TaskRepository) -> None:
        await _add(tasks)

        await tasks._db_execute("DELETE FROM scheduled_tasks")

        assert await tasks.get_all() == []


class TestLockedDatabase:
    async def test_a_write_against_a_locked_database_raises(
        self, tasks: TaskRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing waits longer, retries or swallows: the caller hears about it."""
        task_id = await _add(tasks)
        real_connect = aiosqlite.connect
        # Same default behaviour, only without SQLite's five-second patience.
        monkeypatch.setattr(
            aiosqlite, "connect", lambda path, **kw: real_connect(path, timeout=0.05, **kw)
        )
        holder = sqlite3.connect(tasks.db_path)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                await tasks.set_enabled(task_id, enabled=False)
        finally:
            holder.rollback()
            holder.close()

        assert await _stored(tasks, task_id, "enabled") == 1
