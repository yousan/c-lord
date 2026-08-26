"""#554: the 30-day sweep says what it deleted, in the thread it deleted it from.

c-lord deletes every ``sessions`` row untouched for 30 days, on every startup,
and has always done so in complete silence — one log line with a *count* and no
thread ids. So the first a user hears of it is a month later, when they open an
old thread, ask for the next step, and are told there is no session:

    古い C-lord セッションを続けようとしたところセッションが無い、
    って言われちゃった。消した覚えは無いはず。Discord 上にそういう事も
    書いてないし

The decision (2026-08-26, yousan) is to keep deleting and **say so in the
thread**. What makes the notice worth reading is that it is not one sentence for
every case: the row is gone, but the git clone usually is not, and the transcript
usually is. ``W3 │ Qiita`` is the real example — session dir intact with the
half-written article still in it, transcript gone. Telling that thread "your work
is gone" would be false, and telling the other kind "just carry on" equally so,
so the notice is composed from what is actually on disk.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.database.models import init_db
from c_lord.database.repository import SessionRecord, SessionRepository
from c_lord.session_cleanup import (
    Survivors,
    inspect_survivors,
    notice_for,
)


def _record(thread_id: int = 700, working_dir: str | None = "/tmp/x") -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id="sess-abc",
        working_dir=working_dir,
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-05-18 10:00:00",
        last_used_at="2026-06-18 11:00:00",
    )


# ── AC1: the sweep names what it deleted ─────────────────────────────────────


class TestCleanupOldReturnsRows:
    @pytest.fixture
    async def repo(self, tmp_path) -> SessionRepository:
        db_path = str(tmp_path / "s.db")
        await init_db(db_path)
        return SessionRepository(db_path)

    async def test_returns_the_deleted_rows_not_just_a_count(self, repo) -> None:
        """AC1: a count cannot be notified — you cannot post to a number."""
        await repo.save(thread_id=400, session_id="old", working_dir="/tmp/a")
        await repo.save(thread_id=401, session_id="old2", working_dir="/tmp/b")

        deleted = await repo.cleanup_old(days=0)

        assert {r.thread_id for r in deleted} == {400, 401}
        assert {r.working_dir for r in deleted} == {"/tmp/a", "/tmp/b"}

    async def test_rows_are_actually_gone(self, repo) -> None:
        await repo.save(thread_id=400, session_id="old")
        await repo.cleanup_old(days=0)
        assert await repo.get(400) is None

    async def test_young_rows_are_kept_and_not_reported(self, repo) -> None:
        await repo.save(thread_id=400, session_id="fresh")
        assert await repo.cleanup_old(days=30) == []
        assert await repo.get(400) is not None


# ── AC3: the notice is composed from what actually survived ──────────────────


class TestInspectSurvivors:
    def test_session_dir_present_is_detected(self, tmp_path) -> None:
        (tmp_path / "repo").mkdir()
        s = inspect_survivors(_record(working_dir=str(tmp_path / "repo")), projects_root=tmp_path)
        assert s.session_dir is True

    def test_missing_session_dir_is_detected(self, tmp_path) -> None:
        s = inspect_survivors(_record(working_dir=str(tmp_path / "gone")), projects_root=tmp_path)
        assert s.session_dir is False

    def test_no_working_dir_at_all(self, tmp_path) -> None:
        s = inspect_survivors(_record(working_dir=None), projects_root=tmp_path)
        assert s.session_dir is False
        assert s.transcript is False

    def test_transcript_present_is_detected(self, tmp_path) -> None:
        work = tmp_path / "repo"
        work.mkdir()
        # Claude Code's own layout: <projects_root>/<slug>/<session>.jsonl
        slug = str(work).replace("/", "-").replace(".", "-")
        proj = tmp_path / slug
        proj.mkdir()
        (proj / "sess.jsonl").write_text("{}\n", encoding="utf-8")

        s = inspect_survivors(_record(working_dir=str(work)), projects_root=tmp_path)
        assert s.session_dir is True
        assert s.transcript is True

    def test_never_raises_on_an_unreadable_path(self, tmp_path) -> None:
        """A cleanup notice must not be the thing that breaks startup."""
        s = inspect_survivors(_record(working_dir="\0not-a-path"), projects_root=tmp_path)
        assert s == Survivors(session_dir=False, transcript=False)


class TestNoticeWording:
    def test_says_the_records_were_tidied_and_why(self) -> None:
        text = notice_for(_record(), Survivors(session_dir=True, transcript=False), days=30)
        assert "30" in text
        assert "整理" in text

    def test_work_intact_conversation_lost_is_said_plainly(self) -> None:
        """The Qiita case: the article is still on disk, the memory of it is not.

        This is the split that makes the notice worth posting — a single
        one-size sentence would be wrong for this thread in both directions.
        """
        text = notice_for(_record(), Survivors(session_dir=True, transcript=False), days=30)
        assert "作業ディレクトリ" in text
        assert "残っています" in text
        assert "会話" in text

    def test_everything_gone_does_not_promise_leftovers(self) -> None:
        text = notice_for(_record(), Survivors(session_dir=False, transcript=False), days=30)
        assert "作業ディレクトリ" not in text or "残っています" not in text

    def test_full_recovery_case_mentions_the_conversation_survived(self) -> None:
        text = notice_for(_record(), Survivors(session_dir=True, transcript=True), days=30)
        assert "会話" in text

    def test_every_combination_produces_a_next_step(self) -> None:
        """AC4: a notice that only reports a loss leaves the reader stuck."""
        for sd in (True, False):
            for tr in (True, False):
                text = notice_for(_record(), Survivors(session_dir=sd, transcript=tr), days=30)
                assert text.strip()
                assert "/clord" in text, (sd, tr, text)

    def test_the_three_cases_do_not_share_one_wording(self) -> None:
        """AC3: 出し分け — if these collapse, the check is decorative."""
        both = notice_for(_record(), Survivors(True, True), days=30)
        dir_only = notice_for(_record(), Survivors(True, False), days=30)
        neither = notice_for(_record(), Survivors(False, False), days=30)
        assert len({both, dir_only, neither}) == 3


# ── AC2 / AC5 / AC6 / AC7: the notices actually reach the threads ────────────


def _make_cog(*, days: int = 30, delay: float = 0.0):
    from c_lord.cogs.session_cleanup import SessionCleanupCog

    bot = MagicMock()
    bot.fetch_channel = AsyncMock()
    cog = SessionCleanupCog(bot, days=days)
    cog._post_delay = delay  # keep the tests off the wall clock
    return bot, cog


def _thread(thread_id: int = 700) -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = thread_id
    t.send = AsyncMock()
    return t


class TestAnnounce:
    async def test_posts_one_notice_per_deleted_thread(self, tmp_path) -> None:
        """AC2: the thread the row belonged to is where the answer has to be."""
        bot, cog = _make_cog()
        threads = {700: _thread(700), 701: _thread(701)}
        bot.fetch_channel = AsyncMock(side_effect=lambda tid: threads[tid])

        cog.announce([_record(700, None), _record(701, None)])
        await cog.flush()

        for t in threads.values():
            t.send.assert_awaited_once()
            assert "整理しました" in str(t.send.await_args.args[0])

    async def test_nothing_deleted_posts_nothing(self) -> None:
        bot, cog = _make_cog()
        cog.announce([])
        await cog.flush()
        bot.fetch_channel.assert_not_awaited()

    async def test_a_dead_thread_does_not_stop_the_others(self) -> None:
        """AC5: threads get deleted by users; a 404 on one must not eat the rest,
        and must not take startup with it."""
        bot, cog = _make_cog()
        good = _thread(701)

        async def fetch(tid):
            if tid == 700:
                raise discord.NotFound(MagicMock(status=404), "gone")
            return good

        bot.fetch_channel = AsyncMock(side_effect=fetch)

        cog.announce([_record(700, None), _record(701, None)])
        await cog.flush()  # must not raise

        good.send.assert_awaited_once()

    async def test_a_send_failure_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bot, cog = _make_cog()
        t = _thread(700)
        t.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
        bot.fetch_channel = AsyncMock(return_value=t)

        with caplog.at_level(logging.WARNING):
            cog.announce([_record(700, None)])
            await cog.flush()

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "700" in joined, joined

    async def test_logs_the_thread_ids_it_deleted(self, caplog: pytest.LogCaptureFixture) -> None:
        """AC7: 'Cleaned up 3 old sessions' was unactionable — which three?"""
        bot, cog = _make_cog()
        bot.fetch_channel = AsyncMock(return_value=_thread(700))

        with caplog.at_level(logging.INFO):
            cog.announce([_record(700, None)])

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "700" in joined, joined

    async def test_announcing_does_not_block_the_caller(self) -> None:
        """AC5/AC6: startup hands the batch over and moves on; the posting runs
        behind it, so a slow Discord cannot delay the bot coming up."""
        bot, cog = _make_cog(delay=5.0)
        bot.fetch_channel = AsyncMock(return_value=_thread(700))

        cog.announce([_record(700, None)])  # returns immediately, no await

        assert cog.pending == 1

    async def test_posts_are_spaced_out(self) -> None:
        """AC6: a backlog sweep can delete a lot of rows at once (159 orphaned
        session dirs on yousan's instance). Posting them back to back is how you
        get rate limited; nothing is dropped, it just goes out slowly."""
        bot, cog = _make_cog(delay=0.05)
        bot.fetch_channel = AsyncMock(side_effect=lambda tid: _thread(tid))

        cog.announce([_record(i, None) for i in range(700, 705)])
        loop = asyncio.get_running_loop()
        start = loop.time()
        await cog.flush()
        elapsed = loop.time() - start

        assert elapsed >= 0.05 * 4, elapsed
        assert bot.fetch_channel.await_count == 5
