"""Tests for session repository."""

import pytest

from c_lord.database.models import init_db
from c_lord.database.repository import SessionRepository


@pytest.fixture
async def repo(tmp_path):
    """Create a repository backed by a temporary database."""
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)
    return SessionRepository(db_path)


class TestSessionRepository:
    async def test_save_and_get(self, repo):
        record = await repo.save(thread_id=12345, session_id="session-abc")
        assert record.thread_id == 12345
        assert record.session_id == "session-abc"

        fetched = await repo.get(12345)
        assert fetched is not None
        assert fetched.session_id == "session-abc"

    async def test_get_nonexistent(self, repo):
        result = await repo.get(99999)
        assert result is None

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

    async def test_save_updates_existing(self, repo):
        await repo.save(thread_id=100, session_id="first")
        await repo.save(thread_id=100, session_id="second")

        record = await repo.get(100)
        assert record.session_id == "second"

    async def test_save_with_metadata(self, repo):
        await repo.save(
            thread_id=200,
            session_id="sess-1",
            working_dir="/home/user/project",
            model="opus",
        )
        record = await repo.get(200)
        assert record.working_dir == "/home/user/project"
        assert record.model == "opus"

    async def test_delete(self, repo):
        await repo.save(thread_id=300, session_id="sess-to-delete")
        assert await repo.delete(300) is True
        assert await repo.get(300) is None

    async def test_delete_nonexistent(self, repo):
        assert await repo.delete(99999) is False

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

    async def test_cleanup_old(self, repo):
        # Create a session (it will be "now")
        await repo.save(thread_id=400, session_id="recent")

        # Cleanup with 0 days should delete everything. #554: the deleted rows
        # come back, not a count — the caller has to notify each of those threads.
        deleted = await repo.cleanup_old(days=0)
        assert [r.thread_id for r in deleted] == [400]


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
