"""#818: the 30-day sweep leaves a tombstone instead of deleting the row.

yousan (2026-09-08): 「DB 行は消さないようにしてほしい。スレッドと過去にあった
であろうセッションの紐付けを、調べようとしたら調べられる、ぐらいにしておいてほしい」.

The row is the only thing that ties a Discord thread to its ``working_dir`` and
Claude session id. The sweep still tidies the disk; it now keeps the row and
marks it with the columns a stop already uses — ``closed_at`` (when) and
``closed_reason = "swept"`` (why). No new column, no new state.

A tombstone is **history, not a session**: every live view (``get``, the lists,
the sweeps) must keep seeing exactly what it saw when the row was deleted, and
only the paths that answer "what happened to this thread?" read it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.database.models import init_db
from c_lord.database.repository import SWEPT_REASON, SessionRecord, SessionRepository
from c_lord.session_resume import swept_notice


@pytest.fixture
async def repo(tmp_path) -> SessionRepository:
    db_path = str(tmp_path / "s.db")
    await init_db(db_path)
    return SessionRepository(db_path)


async def _sweep_one(repo: SessionRepository, thread_id: int = 400) -> None:
    await repo.save(thread_id=thread_id, session_id=f"sess-{thread_id}", working_dir="/w/a/b")
    await repo.cleanup_old(days=0)


# ── AC1 / AC2: the row stays, and says when and why ──────────────────────────


class TestTheRowIsKept:
    async def test_row_survives_the_sweep(self, repo) -> None:
        await _sweep_one(repo)
        async with aiosqlite.connect(repo.db_path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM sessions WHERE thread_id = 400")
            row = await cur.fetchone()
            assert row is not None and row[0] == 1

    async def test_records_when_and_why(self, repo) -> None:
        await _sweep_one(repo)
        tomb = await repo.get_swept(400)
        assert tomb is not None
        assert tomb.closed_reason == SWEPT_REASON == "swept"
        assert tomb.closed_at  # a timestamp

    async def test_thread_still_leads_to_its_workdir_and_session(self, repo) -> None:
        """AC2: thread id → working_dir / session id, after the sweep."""
        await _sweep_one(repo)
        tomb = await repo.get_swept(400)
        assert tomb is not None
        assert tomb.working_dir == "/w/a/b"
        assert tomb.session_id == "sess-400"

    async def test_sweep_still_reports_what_it_tidied(self, repo) -> None:
        """#554's notice keeps working: the caller still gets the swept rows."""
        await repo.save(thread_id=400, session_id="a", working_dir="/w/a/b")
        swept = await repo.cleanup_old(days=0)
        assert [r.thread_id for r in swept] == [400]
        assert swept[0].working_dir == "/w/a/b"

    async def test_a_tombstone_is_not_swept_twice(self, repo) -> None:
        """Otherwise every restart would post the 🧹 notice into the thread again."""
        await _sweep_one(repo)
        assert await repo.cleanup_old(days=0) == []

    async def test_get_swept_ignores_live_and_stopped_rows(self, repo) -> None:
        await repo.save(thread_id=1, session_id="live")
        await repo.save(thread_id=2, session_id="stopped")
        await repo.set_closed(2, True, reason="idle")
        assert await repo.get_swept(1) is None
        assert await repo.get_swept(2) is None
        assert await repo.get_swept(999) is None


# ── AC3: live views do not see it ────────────────────────────────────────────


class TestLiveViewsSkipIt:
    async def test_get_does_not_return_it(self, repo) -> None:
        """Every caller of ``get`` treats a row as a session to continue."""
        await _sweep_one(repo)
        assert await repo.get(400) is None

    async def test_lists_skip_it(self, repo) -> None:
        """Session Status, the mirror's on_ready walk and the sweeps read these."""
        await _sweep_one(repo, 400)
        await repo.save(thread_id=401, session_id="live")
        assert [r.thread_id for r in await repo.list_all()] == [401]
        assert [r.thread_id for r in await repo.list_alive()] == [401]
        assert await repo.open_thread_ids() == {401}

    async def test_orphan_sweep_still_sees_its_dir_as_orphaned(self, repo) -> None:
        """A dir the sweep kept (dirty) was an orphan before #818; it stays one."""
        await _sweep_one(repo)
        assert "/w/a/b" not in await repo.all_working_dirs()

    async def test_reset_leaves_it_alone(self, repo) -> None:
        await _sweep_one(repo)
        assert await repo.reset(400) is False
        tomb = await repo.get_swept(400)
        assert tomb is not None and tomb.session_id == "sess-400"


class TestReconnectingRevivesIt:
    async def test_save_clears_the_tombstone(self, repo) -> None:
        """A reattach (#700) or a new turn writes the row again — it is live."""
        await _sweep_one(repo)
        record = await repo.save(thread_id=400, session_id="sess-400")
        assert record.closed_at is None
        assert record.closed_reason is None
        assert await repo.get_swept(400) is None

    async def test_save_does_not_reopen_a_stopped_row(self, repo) -> None:
        """Only the tombstone is lifted; a user's stop is theirs to undo."""
        await repo.save(thread_id=5, session_id="s")
        await repo.set_closed(5, True, reason="manual")
        record = await repo.save(thread_id=5, session_id="s")
        assert record.closed_at is not None
        assert record.closed_reason == "manual"


# ── AC4: a message into a swept thread is answered ───────────────────────────


def _tomb(thread_id: int = 700) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id="sess-abc",
        working_dir="/nowhere",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-05-18 10:00:00",
        last_used_at="2026-06-18 11:00:00",
        closed_at="2026-07-19 09:30:00",
        closed_reason="swept",
    )


class TestSweptNotice:
    def test_says_it_was_tidied_and_when(self) -> None:
        text = swept_notice(_tomb())
        assert "片付け済み" in text
        assert "2026-07-19" in text

    def test_says_the_message_did_not_run(self) -> None:
        assert "届いていません" in swept_notice(_tomb())

    def test_names_a_way_forward(self) -> None:
        assert "/clord prompt:" in swept_notice(_tomb())


CHANNEL_ID = 999
THREAD_ID = 700


def _cog(tomb: SessionRecord | None):
    bot = MagicMock()
    bot.channel_id = CHANNEL_ID
    bot.settings_repo = None
    bot.user = MagicMock()
    bot.user.id = 777
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.get_swept = AsyncMock(return_value=tomb)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner)


def _message():
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.parent_id = CHANNEL_ID
    thread.owner_id = 12345  # a human made it — no other trace of c-lord
    thread.send = AsyncMock()
    message = MagicMock(spec=discord.Message)
    message.channel = thread
    message.content = "続きお願い"
    message.attachments = []
    message.author = MagicMock()
    message.author.bot = False
    message.webhook_id = None
    message.type = discord.MessageType.default
    message.add_reaction = AsyncMock()
    return message, thread


class TestMessageIntoSweptThread:
    async def test_is_answered_with_the_swept_notice(self) -> None:
        """AC4: the tombstone alone proves the thread was ours — no silent drop."""
        cog = _cog(_tomb())
        message, thread = _message()

        await cog._handle_untracked_thread(message, thread)

        thread.send.assert_awaited_once()
        assert "片付け済み" in thread.send.await_args.args[0]
        message.add_reaction.assert_awaited_once()

    async def test_no_tombstone_keeps_the_old_behaviour(self) -> None:
        """A human thread with no trace and no row is still left alone (#556)."""
        cog = _cog(None)
        message, thread = _message()

        await cog._handle_untracked_thread(message, thread)

        thread.send.assert_not_awaited()

    async def test_a_repo_without_get_swept_does_not_break_the_path(self) -> None:
        """Consumers may hand in their own repository (zero-config)."""
        cog = _cog(None)
        del cog.repo.get_swept
        message, thread = _message()

        await cog._handle_untracked_thread(message, thread)

        thread.send.assert_not_awaited()


class TestClordInSweptThread:
    async def test_is_not_refused_as_a_foreign_thread(self) -> None:
        """「c-lord のスレッドではない」 would be false for a thread c-lord swept."""
        cog = _cog(_tomb())
        _, thread = _message()
        respond = AsyncMock()

        import c_lord.cogs.claude_chat as mod

        orig = mod.foreign_owner_notice_for
        mod.foreign_owner_notice_for = AsyncMock(return_value=None)  # type: ignore[assignment]
        try:
            carried_on = await cog._handle_clord_without_session(thread, CHANNEL_ID, respond)
        finally:
            mod.foreign_owner_notice_for = orig  # type: ignore[assignment]

        assert carried_on is False
        thread.send.assert_awaited_once()
        assert "片付け済み" in thread.send.await_args.args[0]
