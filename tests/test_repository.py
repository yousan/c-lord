"""``SessionRepository`` — which Claude session each Discord thread continues."""

from __future__ import annotations

import aiosqlite
import pytest

from c_lord.database.models import init_db
from c_lord.database.repository import SessionRepository


@pytest.fixture
async def repo(tmp_path):
    """A repository on a database of its own (not the mock from conftest)."""
    path = str(tmp_path / "sessions.db")
    await init_db(path)
    return SessionRepository(path)


async def _backdate(repo, thread_id, column, value):
    async with aiosqlite.connect(repo.db_path) as db:
        await db.execute(
            f"UPDATE sessions SET {column} = ? WHERE thread_id = ?", (value, thread_id)
        )
        await db.commit()


class TestSessionRepository:
    async def test_a_saved_session_is_found_by_its_thread(self, repo):
        saved = await repo.save(thread_id=101, session_id="aaaa-1111")

        found = await repo.get(101)

        assert found is not None
        assert (found.thread_id, found.session_id) == (101, "aaaa-1111")
        # ``save`` hands back the row as stored, not an echo of its arguments.
        assert saved == found
        assert found.origin == "discord"
        assert found.working_dir is None and found.model is None and found.summary is None
        assert found.created_at and found.last_used_at

    async def test_a_thread_never_saved_has_no_session(self, repo):
        await repo.save(thread_id=101, session_id="aaaa-1111")

        assert await repo.get(202) is None

    async def test_issue_ref_defaults_none_and_roundtrips(self, repo):
        # #414: new column defaults to NULL and is read/written via set_issue_ref.
        await repo.save(thread_id=42, session_id="s")
        assert (await repo.get(42)).issue_ref is None

        await repo.set_issue_ref(42, "404")
        assert (await repo.get(42)).issue_ref == "404"

        # Clearing back to None is supported.
        await repo.set_issue_ref(42, None)
        assert (await repo.get(42)).issue_ref is None

    async def test_closed_at_defaults_none_and_roundtrips(self, repo):
        # #512: an intentionally closed (/close-workspace'd) session is recorded so
        # it can be told apart from a session whose pane merely crashed (#270).
        await repo.save(thread_id=43, session_id="s2")
        assert (await repo.get(43)).closed_at is None

        await repo.set_closed(43, True)
        record = await repo.get(43)
        assert record.closed_at is not None
        assert record.closed_at != ""

        await repo.set_closed(43, False)
        assert (await repo.get(43)).closed_at is None

    async def test_saving_again_moves_the_thread_to_the_new_session(self, repo):
        """One row per thread: a second save replaces the session id in place
        and counts as use, without touching when the row was created."""
        await repo.save(thread_id=201, session_id="first")
        await _backdate(repo, 201, "created_at", "2020-01-01 00:00:00")
        await _backdate(repo, 201, "last_used_at", "2020-01-01 00:00:00")

        returned = await repo.save(thread_id=201, session_id="second")

        found = await repo.get(201)
        assert found.session_id == "second" == returned.session_id
        assert found.created_at == "2020-01-01 00:00:00"
        assert found.last_used_at != "2020-01-01 00:00:00"
        assert [r.thread_id for r in await repo.list_all()] == [201]

    async def test_saving_without_a_value_keeps_the_one_on_record(self, repo):
        """``None`` means "no news", not "forget it" — a resumed turn that does
        not know the model must not blank it."""
        await repo.save(201, "first", "/work/tree", "opus", "api", "what it is about")

        await repo.save(thread_id=201, session_id="second")

        found = await repo.get(201)
        assert found.session_id == "second"
        assert (found.working_dir, found.model) == ("/work/tree", "opus")
        assert found.summary == "what it is about"

    async def test_metadata_given_at_save_is_stored(self, repo):
        await repo.save(
            thread_id=301,
            session_id="bbbb-2222",
            working_dir="/srv/checkout",
            model="sonnet",
            origin="webhook",
            summary="nightly build",
        )

        found = await repo.get(301)

        assert found.working_dir == "/srv/checkout"
        assert found.model == "sonnet"
        assert found.origin == "webhook"
        assert found.summary == "nightly build"

    async def test_a_later_save_can_replace_the_metadata(self, repo):
        await repo.save(301, "s", working_dir="/old", model="sonnet")

        await repo.save(301, "s", working_dir="/new", model="opus")

        found = await repo.get(301)
        assert (found.working_dir, found.model) == ("/new", "opus")

    async def test_list_all_is_most_recently_used_first_and_limited(self, repo):
        for thread_id, used in ((1, "2024-01-01"), (2, "2024-03-01"), (3, "2024-02-01")):
            await repo.save(thread_id=thread_id, session_id=f"s{thread_id}")
            await _backdate(repo, thread_id, "last_used_at", f"{used} 00:00:00")

        assert [r.thread_id for r in await repo.list_all()] == [2, 3, 1]
        assert [r.thread_id for r in await repo.list_all(limit=2)] == [2, 3]

    async def test_list_all_of_nothing_is_empty(self, repo):
        assert await repo.list_all() == []

    async def test_delete_removes_the_session_and_says_so(self, repo):
        await repo.save(thread_id=401, session_id="doomed")
        await repo.save(thread_id=402, session_id="kept")

        assert await repo.delete(401) is True

        assert await repo.get(401) is None
        assert (await repo.get(402)).session_id == "kept"

    async def test_delete_of_a_thread_never_saved_is_false(self, repo):
        assert await repo.delete(123456) is False

    # ── Issue #117: reset (clear) ─────────────────────────────────────

    async def test_reset_clears_session_id(self, repo):
        """reset() keeps the row but empties session_id so next message starts fresh."""
        await repo.save(thread_id=500, session_id="sess-active")
        result = await repo.reset(500)
        assert result is True
        record = await repo.get(500)
        # Row still exists (on_message can find it)
        assert record is not None
        # session_id is falsy — treated as "no active session" by callers
        assert not record.session_id

    async def test_reset_nonexistent(self, repo):
        """reset() on a row that does not exist returns False."""
        assert await repo.reset(99999) is False

    async def test_reset_preserves_other_fields(self, repo):
        """reset() keeps working_dir and model intact."""
        await repo.save(thread_id=600, session_id="sess-1", working_dir="/proj", model="opus")
        await repo.reset(600)
        record = await repo.get(600)
        assert record is not None
        assert record.working_dir == "/proj"
        assert record.model == "opus"
        assert record.last_used_at

    async def test_cleanup_old_returns_the_sessions_it_swept(self, repo):
        await repo.save(thread_id=400, session_id="idle")
        # Cleanup with 0 days should delete everything. #554: the deleted rows
        # come back, not a count — the caller has to notify each of those threads.
        deleted = await repo.cleanup_old(days=0)
        assert await repo.get(400) is None
        assert [r.thread_id for r in deleted] == [400]

    async def test_cleanup_old_leaves_recently_used_sessions(self, repo):
        await repo.save(thread_id=410, session_id="fresh")
        await repo.save(thread_id=411, session_id="stale")
        await _backdate(repo, 411, "last_used_at", "2020-01-01 00:00:00")

        swept = await repo.cleanup_old(days=30)

        assert [r.thread_id for r in swept] == [411]
        assert (await repo.get(410)).session_id == "fresh"
        assert await repo.get(411) is None

    async def test_cleanup_old_with_nothing_to_sweep_is_empty(self, repo):
        await repo.save(thread_id=420, session_id="fresh")

        assert await repo.cleanup_old() == []
        assert await repo.get(420) is not None

    async def test_cleanup_old_does_not_restamp_rows_swept_before(self, repo):
        """A later sweep marks the newly stale row only.

        The row swept earlier keeps the time it was swept at — that time is
        what its thread is told, and it must not creep forward with every sweep
        that happens to find something else. (That such a row is not *returned*
        again is in tests/test_sweep_tombstone.py.)
        """
        await repo.save(thread_id=430, session_id="swept-long-ago")
        await repo.cleanup_old(days=0)
        await _backdate(repo, 430, "closed_at", "2021-03-04 05:06:07")
        await repo.save(thread_id=431, session_id="stale-now")

        swept = await repo.cleanup_old(days=0)

        assert [r.thread_id for r in swept] == [431]
        assert (await repo.get_swept(430)).closed_at == "2021-03-04 05:06:07"
        assert (await repo.get_swept(431)).closed_at != "2021-03-04 05:06:07"

    async def test_cleanup_old_also_sweeps_a_stopped_session(self, repo):
        """Stopped is not swept: a workspace someone stopped and then left for
        N days is tidied like any other, and from then on reads as swept."""
        await repo.save(thread_id=440, session_id="stopped-then-left")
        await repo.set_closed(440, True, reason="manual")

        swept = await repo.cleanup_old(days=0)

        assert [r.thread_id for r in swept] == [440]
        tomb = await repo.get_swept(440)
        assert tomb is not None and tomb.closed_reason == "swept"


class TestTouch:
    """``touch`` — used by paths that wake a workspace without running a turn (#642)."""

    async def test_moves_last_used_and_leaves_the_sleep_mark(self, repo):
        await repo.save(thread_id=77, session_id="s")
        await repo.set_slept(77, True)
        before = await repo.get(77)
        assert before is not None and before.slept_at is not None

        # Backdate so the bump is observable regardless of clock resolution.
        import aiosqlite

        async with aiosqlite.connect(repo.db_path) as db:
            await db.execute(
                "UPDATE sessions SET last_used_at = '2020-01-01 00:00:00' WHERE thread_id = 77"
            )
            await db.commit()

        await repo.touch(77)

        after = await repo.get(77)
        assert after is not None
        # The sweeps key on this value: an un-bumped row is both killed mid-wake
        # and never revisited afterwards.
        assert after.last_used_at != "2020-01-01 00:00:00"
        # ``slept_at`` words the next resume and is the caller's to clear once
        # the pane is actually back up.
        assert after.slept_at is not None

    async def test_missing_row_is_not_an_error(self, repo):
        await repo.touch(999999)


class TestOpenThreadIds:
    """``open_thread_ids`` — the live-workspace guard the #685 sweep reads."""

    async def test_lists_only_rows_that_are_not_stopped(self, repo):
        await repo.save(thread_id=1, session_id="live")
        await repo.save(thread_id=2, session_id="stopped")
        await repo.set_closed(2, True)

        assert await repo.open_thread_ids() == {1}

    async def test_a_reopened_row_is_live_again(self, repo):
        await repo.save(thread_id=3, session_id="s")
        await repo.set_closed(3, True)
        await repo.set_closed(3, False)

        assert await repo.open_thread_ids() == {3}

    async def test_empty_table_is_an_empty_set(self, repo):
        assert await repo.open_thread_ids() == set()
