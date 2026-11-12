"""SchedulerCog: which tasks a tick starts, and what one scheduled run does.

The task table is a real ``TaskRepository`` on a temp SQLite file — "is it due,
was its next run pushed forward" is the behaviour under test, so it is asked of
the real thing. Discord and Claude are the mocked edges.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.scheduler import MASTER_LOOP_INTERVAL_SECONDS, SchedulerCog
from c_lord.database.task_repo import TaskRepository
from c_lord.turn_events import StartedBy

_PATCH_RUN = "c_lord.cogs.scheduler.run_claude_with_config"
_PATCH_TMUX_RUNNER = "c_lord.claude.tmux_runner.TmuxClaudeRunner"
_CHANNEL_ID = 4001
_THREAD_ID = 9001


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    # No settings repo → thread auto-archive resolver uses the 3-day default.
    bot.settings_repo = None
    bot.owner_id = None
    return bot


def _make_runner() -> MagicMock:
    """Create a mock ClaudeConfig with required attributes."""
    runner = MagicMock()
    runner.model = "sonnet"
    runner.working_dir = None
    runner.timeout_seconds = 300
    runner.effort = None
    return runner


@pytest.fixture
async def repo(tmp_path) -> TaskRepository:
    r = TaskRepository(str(tmp_path / "tasks.db"))
    await r.init_db()
    return r


@pytest.fixture
def cog(repo: TaskRepository) -> SchedulerCog:
    return SchedulerCog(_make_bot(), _make_runner(), repo=repo)


async def _add_task(repo: TaskRepository, name: str = "nightly", **overrides) -> dict:
    """Store a task (due right away unless told otherwise) and return its row."""
    fields = {
        "name": name,
        "prompt": f"Do {name}",
        "interval_seconds": 3600,
        "channel_id": _CHANNEL_ID,
    }
    fields.update(overrides)
    task_id = await repo.create(**fields)
    row = await repo.get(task_id)
    assert row is not None
    return row


async def _tick(cog: SchedulerCog) -> None:
    """One pass of the master loop, then wait for every run it launched."""
    await cog._master_loop()
    await _settle(cog)


async def _settle(cog: SchedulerCog) -> None:
    while cog._launched:
        await asyncio.gather(*list(cog._launched), return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callbacks drop them


def _wire_channel(cog: SchedulerCog) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Give the bot a text channel and a tmux manager; return (channel, starter, thread)."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = _THREAD_ID
    thread.parent_id = _CHANNEL_ID
    thread.send = AsyncMock()
    starter = MagicMock(spec=discord.Message)
    starter.create_thread = AsyncMock(return_value=thread)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = _CHANNEL_ID
    channel.send = AsyncMock(return_value=starter)
    cog.bot.get_channel = MagicMock(return_value=channel)
    cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
    return channel, starter, thread


class TestSchedulerCogInit:
    """A freshly built cog holds its collaborators and runs nothing yet."""

    def test_the_cog_keeps_what_it_was_given(self, repo: TaskRepository) -> None:
        bot, runner = _make_bot(), _make_runner()
        cog = SchedulerCog(bot, runner, repo=repo)
        assert cog.bot is bot
        assert cog.runner is runner
        assert cog.repo is repo

    def test_the_loop_does_not_run_until_the_cog_is_loaded(self, cog: SchedulerCog) -> None:
        assert not cog._master_loop.is_running()

    def test_the_loop_ticks_every_thirty_seconds(self, cog: SchedulerCog) -> None:
        assert MASTER_LOOP_INTERVAL_SECONDS == 30
        assert cog._master_loop.seconds == 30

    async def test_loading_the_cog_starts_the_loop(self, cog: SchedulerCog) -> None:
        with patch.object(cog._master_loop, "start") as start:
            await cog.cog_load()
        start.assert_called_once_with()

    def test_unloading_the_cog_stops_the_loop(self, cog: SchedulerCog) -> None:
        with patch.object(cog._master_loop, "cancel") as cancel:
            cog.cog_unload()
        cancel.assert_called_once_with()

    async def test_the_first_tick_waits_for_the_bot_to_be_ready(self, cog: SchedulerCog) -> None:
        """A run needs its channel, and channels are not there before the bot connects."""
        await cog._before_master_loop()
        cog.bot.wait_until_ready.assert_awaited_once()


class TestSchedulerCogMasterLoop:
    """What one tick of the master loop starts — and what it leaves alone."""

    async def test_an_empty_table_starts_nothing(self, cog: SchedulerCog) -> None:
        with patch.object(cog, "_run_task", new=AsyncMock()) as run_task:
            await _tick(cog)
        run_task.assert_not_called()

    async def test_a_task_that_is_not_due_yet_is_left_alone(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        await _add_task(repo, run_immediately=False)
        with patch.object(cog, "_run_task", new=AsyncMock()) as run_task:
            await _tick(cog)
        run_task.assert_not_called()

    async def test_a_due_task_is_run(self, cog: SchedulerCog, repo: TaskRepository) -> None:
        task = await _add_task(repo, "nightly")
        with patch.object(cog, "_run_task", new=AsyncMock()) as run_task:
            await _tick(cog)
        run_task.assert_awaited_once()
        started = run_task.call_args.args[0]
        assert started["id"] == task["id"]
        assert started["name"] == "nightly"
        assert started["prompt"] == "Do nightly"

    async def test_every_due_task_is_run_in_one_tick(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        first = await _add_task(repo, "first")
        second = await _add_task(repo, "second")
        await _add_task(repo, "later", run_immediately=False)
        with patch.object(cog, "_run_task", new=AsyncMock()) as run_task:
            await _tick(cog)
        assert [c.args[0]["id"] for c in run_task.call_args_list] == [first["id"], second["id"]]

    async def test_a_due_task_is_announced_in_the_log(
        self, cog: SchedulerCog, repo: TaskRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        task = await _add_task(repo)
        with (
            patch.object(cog, "_run_task", new=AsyncMock()),
            caplog.at_level(logging.INFO, logger="c_lord.cogs.scheduler"),
        ):
            await _tick(cog)
        assert f"SchedulerCog: 1 task(s) due (ids=[{task['id']}])" in caplog.text

    async def test_running_a_task_pushes_its_next_run_one_interval_ahead(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        task = await _add_task(repo, interval_seconds=3600)
        with patch.object(cog, "_run_task", new=AsyncMock()):
            await _tick(cog)

        after = await repo.get(task["id"])
        assert after is not None
        assert after["next_run_at"] >= task["next_run_at"] + 3600
        assert after["last_run_at"] is not None
        assert await repo.get_due() == []

    async def test_the_next_run_is_pushed_ahead_before_the_run_starts(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """A run that outlives a tick must not look due to the next tick.

        Were the time only updated when the run *ends*, a crash mid-run (or a
        bot restart) would leave the task due and start it again at once.
        """
        await _add_task(repo)
        due_when_the_run_began: list[list[dict]] = []

        async def run(_task: dict) -> None:
            due_when_the_run_began.append(await repo.get_due())

        with patch.object(cog, "_run_task", new=run):
            await _tick(cog)

        assert due_when_the_run_began == [[]]

    async def test_a_task_still_running_is_skipped_and_stays_due(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """Skipping must not consume the slot: the task comes up again next tick."""
        task = await _add_task(repo)
        cog._running.add(task["id"])

        with patch.object(cog, "_run_task", new=AsyncMock()) as run_task:
            await _tick(cog)
            run_task.assert_not_called()
            unchanged = await repo.get(task["id"])
            assert unchanged is not None
            assert unchanged["next_run_at"] == task["next_run_at"]
            assert unchanged["last_run_at"] is None

            # Its run ends; the very next tick picks it up.
            cog._running.discard(task["id"])
            await _tick(cog)
            run_task.assert_awaited_once()

    async def test_a_long_run_is_not_started_again_by_the_next_tick(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """End to end, with the real ``_run_task`` doing the bookkeeping."""
        # interval 0: the task is due again on every tick, run or no run.
        await _add_task(repo, interval_seconds=0)
        _wire_channel(cog)
        entered, release = asyncio.Event(), asyncio.Event()
        runs = 0

        async def held_run(_config: object) -> str:
            nonlocal runs
            runs += 1
            entered.set()
            await release.wait()
            return "sess-1"

        with patch(_PATCH_TMUX_RUNNER), patch(_PATCH_RUN, new=held_run):
            await cog._master_loop()
            await entered.wait()
            await cog._master_loop()
            await asyncio.sleep(0)
            assert runs == 1
            release.set()
            await _settle(cog)

            # Once it has finished, it may run again.
            release.set()
            await _tick(cog)
        assert runs == 2

    async def test_a_disabled_task_is_not_run(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        task = await _add_task(repo)
        await repo.set_enabled(task["id"], enabled=False)
        with patch.object(cog, "_run_task", new=AsyncMock()) as run_task:
            await _tick(cog)
        run_task.assert_not_called()

    async def test_run_task_creates_starter_message_then_thread(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """The thread hangs off a message in the channel, so the run shows in its timeline."""
        task = await _add_task(repo, "nightly")
        mock_channel, mock_starter_msg, thread = _wire_channel(cog)
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())

        with (
            patch(_PATCH_TMUX_RUNNER),
            patch(
                "c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock
            ) as mock_run,
        ):
            await cog._run_task(task)

        mock_channel.send.assert_awaited_once_with("🔄 **[Scheduled]** `nightly`")
        mock_starter_msg.create_thread.assert_awaited_once()
        assert mock_starter_msg.create_thread.call_args[1]["name"] == "[Scheduled] nightly"
        # Defaults to 3 days (4320 min) when no setting is configured.
        assert mock_starter_msg.create_thread.call_args[1]["auto_archive_duration"] == 4320
        assert mock_run.call_args[0][0].thread is thread


class TestRunTask:
    """One scheduled run, from the channel lookup to the exit log line."""

    async def test_claude_gets_the_tasks_prompt_in_a_fresh_session(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        task = await _add_task(repo, "nightly", working_dir="/srv/checkout")
        _wire_channel(cog)

        with (
            patch(_PATCH_TMUX_RUNNER) as runner_cls,
            patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run,
        ):
            await cog._run_task(task)

        config = mock_run.call_args[0][0]
        assert config.prompt == "Do nightly"
        assert config.session_id is None
        assert config.runner is runner_cls.return_value
        assert config.working_dir == "/srv/checkout"
        assert config.started_by == StartedBy("scheduler", str(task["id"]), "nightly")

    async def test_the_run_is_saved_as_a_session_so_replies_work(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """#687: without a session row every reply in the run's thread was dropped."""
        task = await _add_task(repo)
        _wire_channel(cog)

        with patch(_PATCH_TMUX_RUNNER), patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run:
            await cog._run_task(task)

        config = mock_run.call_args[0][0]
        assert config.repo is cog.bot.session_repo
        assert config.registry is cog.bot.session_registry

    async def test_the_runner_never_stops_for_permission_prompts(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """Nobody is there to approve one in an unattended run."""
        task = await _add_task(repo, working_dir="/srv/checkout")
        _wire_channel(cog)
        tmux = cog._resolve_tmux_manager.return_value

        with patch(_PATCH_TMUX_RUNNER) as runner_cls, patch(_PATCH_RUN, new_callable=AsyncMock):
            await cog._run_task(task)

        kwargs = runner_cls.call_args.kwargs
        assert kwargs["tmux_manager"] is tmux
        assert kwargs["thread_id"] == _THREAD_ID
        assert kwargs["model"] == "sonnet"
        assert kwargs["working_dir"] == "/srv/checkout"
        assert kwargs["timeout_seconds"] == 300
        assert kwargs["dangerously_skip_permissions"] is True

    async def test_a_task_without_a_directory_uses_the_configured_default(
        self, repo: TaskRepository
    ) -> None:
        runner = _make_runner()
        runner.working_dir = "/srv/default"
        cog = SchedulerCog(_make_bot(), runner, repo=repo)
        task = await _add_task(repo)
        _wire_channel(cog)

        with patch(_PATCH_TMUX_RUNNER) as runner_cls, patch(_PATCH_RUN, new_callable=AsyncMock):
            await cog._run_task(task)

        assert runner_cls.call_args.kwargs["working_dir"] == "/srv/default"

    async def test_a_channel_that_no_longer_exists_ends_the_run_quietly(
        self, cog: SchedulerCog, repo: TaskRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        task = await _add_task(repo)
        cog.bot.get_channel = MagicMock(return_value=None)

        with (
            patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run,
            caplog.at_level(logging.WARNING, logger="c_lord.cogs.scheduler"),
        ):
            await cog._run_task(task)

        mock_run.assert_not_called()
        assert "channel not found" in caplog.text

    async def test_a_channel_that_cannot_hold_threads_ends_the_run_quietly(
        self, cog: SchedulerCog, repo: TaskRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        task = await _add_task(repo)
        voice = MagicMock(spec=discord.VoiceChannel)
        voice.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=voice)

        with (
            patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run,
            caplog.at_level(logging.WARNING, logger="c_lord.cogs.scheduler"),
        ):
            await cog._run_task(task)

        mock_run.assert_not_called()
        voice.send.assert_not_called()
        assert "not a TextChannel" in caplog.text

    async def test_an_unbound_channel_gets_no_starter_message(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """No tmux manager → nothing can run, so nothing is announced in the channel."""
        task = await _add_task(repo)
        channel, _, _ = _wire_channel(cog)
        cog._resolve_tmux_manager = AsyncMock(return_value=None)

        with patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run:
            await cog._run_task(task)

        channel.send.assert_not_called()
        mock_run.assert_not_called()

    async def test_the_tmux_manager_is_resolved_for_the_channel_not_a_thread(
        self, repo: TaskRepository
    ) -> None:
        """#600: a scheduled task is bound to a channel, so no thread id is passed on."""
        from c_lord.cogs.channel_repo import ChannelRepoCog

        channel_cog = MagicMock(spec=ChannelRepoCog)
        channel_cog.resolve_tmux_manager = AsyncMock(return_value="the-manager")
        bot = _make_bot()
        bot.get_cog = MagicMock(return_value=channel_cog)
        cog = SchedulerCog(bot, _make_runner(), repo=repo)

        assert await cog._resolve_tmux_manager(_CHANNEL_ID, thread_id=None) == "the-manager"
        channel_cog.resolve_tmux_manager.assert_awaited_once_with(_CHANNEL_ID, thread_id=None)

    async def test_without_a_channel_repo_cog_there_is_no_tmux_manager(
        self, repo: TaskRepository
    ) -> None:
        bot = _make_bot()
        bot.get_cog = MagicMock(return_value=None)
        cog = SchedulerCog(bot, _make_runner(), repo=repo)
        assert await cog._resolve_tmux_manager(_CHANNEL_ID, thread_id=None) is None

    async def test_a_run_is_bracketed_by_enter_and_exit_in_the_log(
        self, cog: SchedulerCog, repo: TaskRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        task = await _add_task(repo, "nightly")
        _wire_channel(cog)

        with (
            patch(_PATCH_TMUX_RUNNER),
            patch(_PATCH_RUN, new_callable=AsyncMock),
            caplog.at_level(logging.INFO, logger="c_lord.cogs.scheduler"),
        ):
            await cog._run_task(task)

        messages = [r.getMessage() for r in caplog.records]
        enter = next(i for i, m in enumerate(messages) if "_run_task: enter" in m)
        leave = next(i for i, m in enumerate(messages) if "_run_task: exit" in m)
        assert enter < leave
        assert f"task={task['id']}" in messages[enter]

    async def test_a_run_that_blows_up_is_logged_and_does_not_escape(
        self, cog: SchedulerCog, repo: TaskRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nobody awaits a scheduled run — an escaping error would vanish unseen."""
        task = await _add_task(repo, "nightly")
        _wire_channel(cog)

        with (
            patch(_PATCH_TMUX_RUNNER),
            patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run,
            caplog.at_level(logging.INFO, logger="c_lord.cogs.scheduler"),
        ):
            mock_run.side_effect = RuntimeError("boom")
            await cog._run_task(task)  # must not raise

        assert "task failed (name=nightly)" in caplog.text
        assert "_run_task: exit" in caplog.text

    @pytest.mark.parametrize("fails", [True, False], ids=["run-raises", "run-returns"])
    async def test_a_finished_run_no_longer_counts_as_running(
        self, cog: SchedulerCog, repo: TaskRepository, fails: bool
    ) -> None:
        """Or the task would be skipped on every tick from then on."""
        task = await _add_task(repo)
        _wire_channel(cog)
        running_during: list[bool] = []

        async def run(_config: object) -> str:
            running_during.append(task["id"] in cog._running)
            if fails:
                raise RuntimeError("boom")
            return "sess-1"

        with patch(_PATCH_TMUX_RUNNER), patch(_PATCH_RUN, new=run):
            await cog._run_task(task)

        assert running_during == [True]
        assert task["id"] not in cog._running

    async def test_a_run_that_ended_in_an_error_embed_is_an_error_in_the_log(
        self, cog: SchedulerCog, repo: TaskRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#621: a failed run must not leave a log that reads like a clean exit."""
        task = await _add_task(repo, "nightly")
        _wire_channel(cog)

        async def failing_run(config) -> None:
            config.outcome.error = "Claude exited without producing a response"

        with (
            patch(_PATCH_TMUX_RUNNER),
            patch(_PATCH_RUN, new=failing_run),
            caplog.at_level(logging.INFO, logger="c_lord.cogs.scheduler"),
        ):
            await cog._run_task(task)

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "task run failed (name=nightly)" in m and "without producing a response" in m
            for m in errors
        )


class TestWhereAScheduledRunWorks:
    """Which directory a run uses: the task's own, else the configured default.

    One answer has to reach four places — the tmux window Claude starts in,
    the runner, the saved session (replies resume from it) and the transcript
    mirror (it finds the transcript by it) — or the run and its thread disagree.
    """

    @staticmethod
    async def _run(
        repo: TaskRepository, *, task_dir: str | None, default_dir: str | None
    ) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
        """Run one task; return (tmux manager, runner class, RunConfig, mirror cog)."""
        runner = _make_runner()
        runner.working_dir = default_dir
        cog = SchedulerCog(_make_bot(), runner, repo=repo)
        overrides = {"working_dir": task_dir} if task_dir is not None else {}
        task = await _add_task(repo, **overrides)
        _wire_channel(cog)
        tmux = cog._resolve_tmux_manager.return_value

        with (
            patch(_PATCH_TMUX_RUNNER) as runner_cls,
            patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run,
        ):
            await cog._run_task(task)

        return tmux, runner_cls, mock_run.call_args[0][0], cog.bot.transcript_mirror_cog

    async def test_the_tasks_own_directory_wins_over_the_default(
        self, repo: TaskRepository, tmp_path
    ) -> None:
        own, default = str(tmp_path / "task-checkout"), str(tmp_path / "default")

        tmux, runner_cls, config, mirror = await self._run(repo, task_dir=own, default_dir=default)

        tmux.create_session.assert_called_once_with(_THREAD_ID, own)
        assert runner_cls.call_args.kwargs["working_dir"] == own
        assert config.working_dir == own
        mirror.start_for.assert_called_once_with(_THREAD_ID, own)

    async def test_without_one_the_default_is_used_everywhere(
        self, repo: TaskRepository, tmp_path
    ) -> None:
        default = str(tmp_path / "default")

        tmux, runner_cls, config, mirror = await self._run(repo, task_dir=None, default_dir=default)

        tmux.create_session.assert_called_once_with(_THREAD_ID, default)
        assert runner_cls.call_args.kwargs["working_dir"] == default
        assert config.working_dir == default
        mirror.start_for.assert_called_once_with(_THREAD_ID, default)

    async def test_with_neither_the_window_opens_where_the_bot_runs(
        self, repo: TaskRepository
    ) -> None:
        """No directory anywhere: the window still opens (in ``.``), and no mirror
        is started — there is no project directory to find a transcript under."""
        tmux, runner_cls, config, mirror = await self._run(repo, task_dir=None, default_dir=None)

        tmux.create_session.assert_called_once_with(_THREAD_ID, ".")
        assert runner_cls.call_args.kwargs["working_dir"] is None
        assert config.working_dir is None
        mirror.start_for.assert_not_called()


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
