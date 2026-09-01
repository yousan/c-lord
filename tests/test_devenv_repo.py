"""Persistence for workspace ↔ dev-environment links — Issue #573.

Discovery (``c_lord.devenv``) can only see containers that still exist and only
answers "which containers belong to this session dir". That is not enough for
the problem #573 is about: on the production host the *workspace* vanished
while the containers stayed, so the question people actually ask is the reverse
one — "this container is holding port 55322; whose was it?".

Answering that after the fact needs a record written while the link was still
observable, which is what this repository stores.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from c_lord.database.devenv_repo import DevEnvRepository
from c_lord.devenv import DevContainer


def _c(name: str, *, ports: tuple[int, ...] = (), status: str = "running") -> DevContainer:
    return DevContainer(
        container_id=f"id-{name}",
        name=name,
        status=status,
        ports=ports,
        project="proj",
        source="mount",
    )


@pytest.fixture
async def repo():
    with tempfile.TemporaryDirectory() as d:
        r = DevEnvRepository(str(Path(d) / "t.db"))
        await r.init_db()
        yield r


@pytest.mark.asyncio
class TestRecordAndLookup:
    async def test_records_and_reads_back(self, repo: DevEnvRepository) -> None:
        await repo.record(111, "/s/111", [_c("web", ports=(8080,)), _c("db", ports=(5432,))])

        rows = await repo.for_thread(111)

        assert sorted(r.container_name for r in rows) == ["db", "web"]
        assert {p for r in rows for p in r.ports} == {8080, 5432}

    async def test_reverse_lookup_survives_the_workspace(self, repo: DevEnvRepository) -> None:
        """The whole point: the container outlives the workspace, and we can
        still name the thread that started it."""
        await repo.record(111, "/s/111", [_c("supabase_db", ports=(55322,))])

        assert await repo.thread_for_container("supabase_db") == 111
        assert await repo.thread_for_port(55322) == 111

    async def test_recording_again_updates_instead_of_duplicating(
        self, repo: DevEnvRepository
    ) -> None:
        """Discovery runs every turn — it must not grow a row each time."""
        await repo.record(111, "/s/111", [_c("web", ports=(8080,), status="running")])
        await repo.record(111, "/s/111", [_c("web", ports=(8080,), status="exited")])

        rows = await repo.for_thread(111)

        assert len(rows) == 1
        assert rows[0].status == "exited"

    async def test_containers_that_disappeared_are_marked_gone(
        self, repo: DevEnvRepository
    ) -> None:
        """A container removed outside c-lord must stop being reported as live,
        without erasing the record of who owned it."""
        await repo.record(111, "/s/111", [_c("web"), _c("db")])
        await repo.record(111, "/s/111", [_c("web")])

        rows = {r.container_name: r for r in await repo.for_thread(111)}

        assert rows["web"].status == "running"
        assert rows["db"].status == "gone"
        assert await repo.thread_for_container("db") == 111

    async def test_unknown_lookups_return_none(self, repo: DevEnvRepository) -> None:
        assert await repo.thread_for_container("nope") is None
        assert await repo.thread_for_port(1) is None
        assert await repo.for_thread(999) == []

    async def test_threads_do_not_leak_into_each_other(self, repo: DevEnvRepository) -> None:
        await repo.record(111, "/s/111", [_c("a")])
        await repo.record(222, "/s/222", [_c("b")])

        assert [r.container_name for r in await repo.for_thread(111)] == ["a"]
        assert [r.container_name for r in await repo.for_thread(222)] == ["b"]

    async def test_recording_nothing_marks_all_previous_gone(self, repo: DevEnvRepository) -> None:
        await repo.record(111, "/s/111", [_c("a")])
        await repo.record(111, "/s/111", [])

        rows = await repo.for_thread(111)
        assert [r.status for r in rows] == ["gone"]
