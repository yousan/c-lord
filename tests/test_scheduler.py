"""Tests for SchedulerCog."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.cogs.scheduler import SchedulerCog
from c_lord.database.task_repo import TaskRepository


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.loop = MagicMock()
    # No settings repo → thread auto-archive resolver uses the 3-day default.
    bot.settings_repo = None
    return bot


def _make_runner() -> MagicMock:
    """Create a mock ClaudeConfig with required attributes."""
    runner = MagicMock()
    runner.model = "sonnet"
    runner.working_dir = None
    runner.timeout_seconds = 300
    return runner


@pytest.fixture
async def repo(tmp_path) -> TaskRepository:
    r = TaskRepository(str(tmp_path / "tasks.db"))
    await r.init_db()
    return r


@pytest.fixture
def cog(repo: TaskRepository) -> SchedulerCog:
    return SchedulerCog(_make_bot(), _make_runner(), repo=repo)


class TestSchedulerCogInit:
    def test_cog_created(self, repo: TaskRepository) -> None:
        cog = SchedulerCog(_make_bot(), _make_runner(), repo=repo)
        assert cog is not None

    def test_master_loop_not_running_at_init(self, repo: TaskRepository) -> None:
        cog = SchedulerCog(_make_bot(), _make_runner(), repo=repo)
        # loop should not be running before cog_load is called
        assert not cog._master_loop.is_running()


class TestSchedulerCogMasterLoop:
    async def test_no_tasks_does_nothing(self, cog: SchedulerCog) -> None:
        """Master loop with empty DB should complete without errors."""
        with patch(
            "c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog._master_loop()
        mock_run.assert_not_called()

    async def test_future_task_not_run(self, cog: SchedulerCog, repo: TaskRepository) -> None:
        """Tasks with next_run_at in the future should not fire."""
        task_id = await repo.create(name="future", prompt="p", interval_seconds=3600, channel_id=1)
        await repo._db_execute(
            "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
            (time.time() + 9999, task_id),
        )
        with patch(
            "c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog._master_loop()
        mock_run.assert_not_called()

    async def test_due_task_triggers_run(self, cog: SchedulerCog, repo: TaskRepository) -> None:
        """Due tasks should cause _run_task to be called via create_task."""
        task_id = await repo.create(
            name="due", prompt="check stuff", interval_seconds=60, channel_id=42
        )
        await repo._db_execute(
            "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
            (time.time() - 1, task_id),
        )
        # Patch _run_task directly — create_task wraps a coroutine, so we need
        # to intercept at this level (not run_claude_in_thread) and then yield
        # control so the event loop can execute the spawned task.
        cog._run_task = AsyncMock()
        await cog._master_loop()
        await asyncio.sleep(0)  # yield to let create_task execute

        cog._run_task.assert_called_once()
        called_task = cog._run_task.call_args[0][0]
        assert called_task["prompt"] == "check stuff"

    async def test_due_task_updates_next_run(self, cog: SchedulerCog, repo: TaskRepository) -> None:
        """After firing, next_run_at should be advanced by interval_seconds."""
        task_id = await repo.create(name="tick", prompt="p", interval_seconds=300, channel_id=1)
        before = time.time()
        await repo._db_execute(
            "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
            (time.time() - 1, task_id),
        )
        cog._run_task = AsyncMock()
        await cog._master_loop()

        task = await repo.get(task_id)
        assert task is not None
        assert task["next_run_at"] >= before + 300 - 1

    async def test_run_task_creates_starter_message_then_thread(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """_run_task should post a starter message then attach a thread to it.

        This ensures the thread appears in the channel list (left sidebar)
        rather than only in the Threads panel (🧵).
        """
        import discord

        task_id = await repo.create(
            name="my-task", prompt="do stuff", interval_seconds=60, channel_id=99
        )
        task = await repo.get(task_id)

        # Build mock channel → starter message → thread chain
        mock_thread = AsyncMock(spec=discord.Thread)
        mock_starter_msg = AsyncMock()
        mock_starter_msg.create_thread = AsyncMock(return_value=mock_thread)
        mock_channel = AsyncMock(spec=discord.TextChannel)
        mock_channel.send = AsyncMock(return_value=mock_starter_msg)

        cog.bot.get_channel = MagicMock(return_value=mock_channel)
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())

        with patch(
            "c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog._run_task(task)

        # Starter message posted to channel
        mock_channel.send.assert_called_once()
        sent_content = mock_channel.send.call_args[0][0]
        assert "my-task" in sent_content

        # Thread created from the starter message (not from the channel)
        mock_starter_msg.create_thread.assert_called_once()
        thread_name = mock_starter_msg.create_thread.call_args[1]["name"]
        assert "my-task" in thread_name
        # Defaults to 3 days (4320 min) when no setting is configured.
        assert mock_starter_msg.create_thread.call_args[1]["auto_archive_duration"] == 4320

        # Claude ran inside the thread — call_args[0][0] is the RunConfig
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0].thread is mock_thread

    async def test_disabled_task_not_run(self, cog: SchedulerCog, repo: TaskRepository) -> None:
        """Disabled tasks should not fire even if overdue."""
        task_id = await repo.create(name="dis", prompt="p", interval_seconds=60, channel_id=1)
        await repo._db_execute(
            "UPDATE scheduled_tasks SET next_run_at = ?, enabled = 0 WHERE id = ?",
            (time.time() - 1, task_id),
        )
        with patch(
            "c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog._master_loop()
        mock_run.assert_not_called()


class TestScheduledRunMirrorOwnership:
    """#719: last week's thread must not fill up with this week's run."""

    async def test_a_new_run_takes_the_mirror_from_last_runs_thread(
        self, repo: TaskRepository, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Replays the production sequence that leaked for a week (#719).

        Run 1 mirrors into thread A.  The bot restarts: ``on_ready`` restores a
        mirror for A (its session row is open), while the scheduler's in-memory
        "previous thread" note is gone.  Run 2 then starts thread B — and used
        to leave A tailing the same transcript, so every message of run 2 was
        posted into a thread that had been finished for a week.
        """
        from c_lord.cogs.transcript_mirror import TranscriptMirrorCog

        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude" / "projects" / "-fixed-checkout").mkdir(parents=True)
        working_dir = "/fixed/checkout"
        session_repo = MagicMock()
        session_repo.list_all = AsyncMock(return_value=[])

        # ── run 1 ────────────────────────────────────────────────────────
        mirrors = TranscriptMirrorCog(_make_bot(), session_repo=session_repo)
        bot = _make_bot()
        bot.transcript_mirror_cog = mirrors
        cog = SchedulerCog(bot, _make_runner(), repo=repo)
        try:
            await cog._start_transcript_mirror(1, 111, working_dir)
            assert set(mirrors._mirrors) == {111}
        finally:
            await mirrors.cog_unload()

        # ── restart: on_ready restores A's mirror, the scheduler forgets ──
        mirrors = TranscriptMirrorCog(_make_bot(), session_repo=session_repo)
        assert mirrors.start_for(111, working_dir) is True
        bot = _make_bot()
        bot.transcript_mirror_cog = mirrors
        cog = SchedulerCog(bot, _make_runner(), repo=repo)

        # ── run 2 ────────────────────────────────────────────────────────
        try:
            await cog._start_transcript_mirror(1, 222, working_dir)
            assert set(mirrors._mirrors) == {222}
        finally:
            await mirrors.cog_unload()
