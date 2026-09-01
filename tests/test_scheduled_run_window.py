"""Issue #621 — a scheduled run must have a tmux window to start Claude in.

``SchedulerCog._run_task`` built a ``TmuxClaudeRunner`` and called
``run_claude_with_config`` without ever creating the window that runner types
into, so *every* scheduled task died two seconds in with a single ❌ embed —
and that embed blamed a dead pane and told the user to ``/restart-claude``,
which cannot fix a window that was never created.

The tests below pin all four halves of that bug:

* the window is created (and the transcript mirror started) before Claude runs,
* the failure is loud in the log rather than a clean-looking ``exit``,
* "there is no window" and "the pane will not take input" get different wording,
* neither of those wordings sends the user to a command that will not help.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.claude.types import MessageType
from c_lord.cogs.scheduler import SchedulerCog
from c_lord.database.task_repo import TaskRepository

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.loop = MagicMock()
    bot.settings_repo = None
    return bot


def _make_runner_config() -> MagicMock:
    cfg = MagicMock()
    cfg.model = "sonnet"
    cfg.working_dir = None
    cfg.timeout_seconds = 300
    cfg.effort = None
    return cfg


@pytest.fixture
async def repo(tmp_path) -> TaskRepository:
    r = TaskRepository(str(tmp_path / "tasks.db"))
    await r.init_db()
    return r


@pytest.fixture
def cog(repo: TaskRepository) -> SchedulerCog:
    return SchedulerCog(_make_bot(), _make_runner_config(), repo=repo)


def _wire_channel(cog: SchedulerCog) -> tuple[MagicMock, MagicMock]:
    """Give the cog a channel → starter message → thread chain. Returns (thread, tmux)."""
    thread = AsyncMock(spec=discord.Thread)
    thread.id = 777
    starter = AsyncMock()
    starter.create_thread = AsyncMock(return_value=thread)
    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 99
    channel.send = AsyncMock(return_value=starter)
    cog.bot.get_channel = MagicMock(return_value=channel)

    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w7")
    tmux.session_exists = MagicMock(return_value=True)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
    return thread, tmux


async def _make_task(repo: TaskRepository, *, working_dir: str | None) -> dict:
    task_id = await repo.create(
        name="audit",
        prompt="do the weekly audit",
        interval_seconds=604800,
        channel_id=99,
        working_dir=working_dir,
    )
    task = await repo.get(task_id)
    assert task is not None
    return task


# ---------------------------------------------------------------------------
# AC2 — the window is created before Claude is started
# ---------------------------------------------------------------------------


class TestSchedulerCreatesWindow:
    async def test_run_task_creates_the_tmux_window(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """The regression: ``_run_task`` never called ``create_session`` at all."""
        thread, tmux = _wire_channel(cog)
        task = await _make_task(repo, working_dir="/home/yousan/c-lord-audit")

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock):
            await cog._run_task(task)

        tmux.create_session.assert_called_once_with(thread.id, "/home/yousan/c-lord-audit")

    async def test_window_is_created_before_claude_runs(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """Order matters: a window created after the run is a window Claude never saw."""
        _thread, tmux = _wire_channel(cog)
        task = await _make_task(repo, working_dir="/tmp/wd")

        order: list[str] = []
        tmux.create_session.side_effect = lambda *_: order.append("create_session") or "w7"

        async def _run(_config):
            order.append("run_claude")

        with patch("c_lord.cogs.scheduler.run_claude_with_config", side_effect=_run):
            await cog._run_task(task)

        assert order == ["create_session", "run_claude"]

    async def test_window_uses_the_runner_default_when_the_task_has_no_working_dir(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """The window and the runner must land in the same directory."""
        thread, tmux = _wire_channel(cog)
        cog.runner.working_dir = "/srv/default"
        task = await _make_task(repo, working_dir=None)

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock) as run:
            await cog._run_task(task)

        tmux.create_session.assert_called_once_with(thread.id, "/srv/default")
        assert run.call_args[0][0].runner.working_dir == "/srv/default"


# ---------------------------------------------------------------------------
# AC1 — Claude's answer has a path back to the thread
# ---------------------------------------------------------------------------


class TestSchedulerStartsTranscriptMirror:
    async def test_mirror_started_for_the_scheduled_thread(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """In jsonl bridge mode (the default) nothing else posts Claude's answer."""
        thread, _tmux = _wire_channel(cog)
        mirror_cog = MagicMock()
        mirror_cog.start_for = MagicMock(return_value=True)
        cog.bot.transcript_mirror_cog = mirror_cog  # type: ignore[attr-defined]
        task = await _make_task(repo, working_dir="/home/yousan/c-lord-audit")

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock):
            await cog._run_task(task)

        mirror_cog.start_for.assert_called_once_with(thread.id, "/home/yousan/c-lord-audit")

    async def test_previous_run_of_the_same_task_stops_mirroring(
        self, cog: SchedulerCog, repo: TaskRepository
    ) -> None:
        """Two runs share one working_dir, so the old thread would echo the new run.

        Every run of a task tails the same ``~/.claude/projects/<slug>``. Left
        running, last week's mirror would replay this week's whole turn into
        last week's thread.
        """
        thread, _tmux = _wire_channel(cog)
        mirror_cog = MagicMock()
        mirror_cog.start_for = MagicMock(return_value=True)
        mirror_cog.stop_for = AsyncMock()
        cog.bot.transcript_mirror_cog = mirror_cog  # type: ignore[attr-defined]
        task = await _make_task(repo, working_dir="/home/yousan/c-lord-audit")

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock):
            await cog._run_task(task)
            mirror_cog.stop_for.assert_not_called()  # nothing to stop on the first run

            thread.id = 888  # the next run gets a brand-new thread
            await cog._run_task(task)

        mirror_cog.stop_for.assert_awaited_once_with(777)


# ---------------------------------------------------------------------------
# AC4 — a scheduled run that fails says so in the log
# ---------------------------------------------------------------------------


class TestSchedulerLogsFailures:
    async def test_missing_window_is_logged_as_error(
        self, cog: SchedulerCog, repo: TaskRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        thread, tmux = _wire_channel(cog)
        tmux.session_exists.return_value = False  # creation silently did nothing
        task = await _make_task(repo, working_dir="/tmp/wd")

        with (
            caplog.at_level(logging.WARNING, logger="c_lord.cogs.scheduler"),
            patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock) as run,
        ):
            await cog._run_task(task)

        assert not run.called, "Claude must not be started without a window"
        assert any(r.levelno >= logging.ERROR for r in caplog.records), (
            "a scheduled run that never started must not look like a clean exit"
        )
        thread.send.assert_awaited()  # and the thread is not left empty

    async def test_failed_run_is_logged_as_error(
        self, cog: SchedulerCog, repo: TaskRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The run posted an ❌ embed but the scheduler logged only ``exit``."""
        _thread, _tmux = _wire_channel(cog)
        task = await _make_task(repo, working_dir="/tmp/wd")

        async def _run(config):
            config.outcome.error = "Claude の起動に失敗しました — ..."

        with (
            caplog.at_level(logging.WARNING, logger="c_lord.cogs.scheduler"),
            patch("c_lord.cogs.scheduler.run_claude_with_config", side_effect=_run),
        ):
            await cog._run_task(task)

        assert any(r.levelno >= logging.ERROR for r in caplog.records), (
            "a failed scheduled run must leave an ERROR behind"
        )


# ---------------------------------------------------------------------------
# AC3 — "no window" and "dead pane" are different problems, told apart
# ---------------------------------------------------------------------------


class TestMissingWindowWording:
    def test_missing_window_text_names_the_real_problem(self) -> None:
        from c_lord.claude.tmux_runner import _delivery_failure, _missing_window

        text = _missing_window("Claude の起動", 1543873803061301401)
        assert "ウィンドウ" in text, "must say the window is what is missing"
        assert "/restart-claude" not in text, (
            "/restart-claude restarts an existing pane — it cannot create a window"
        )
        # The #527 wording's two false claims. Naming a pane is fine — and the
        # text does, to rebut the theory the old message installed; *asserting*
        # the pane died is what misled the reader.
        assert "入力を受け付けませんでした" not in text
        assert "応答しない状態です" not in text
        assert text != _delivery_failure("Claude の起動", "hello")

    async def test_runner_reports_a_missing_window_as_such(self) -> None:
        """The pane-level truth reaches Discord, not a guess about it."""
        from c_lord.claude.tmux_runner import TmuxClaudeRunner

        tmux = MagicMock()
        tmux.is_claude_running.return_value = False
        tmux.start_claude.return_value = False
        tmux.session_exists.return_value = False  # never created — the #621 case

        runner = TmuxClaudeRunner(tmux_manager=tmux, thread_id=42, model="sonnet")
        errors = [
            ev.error async for ev in runner.run("hi") if ev.message_type is MessageType.RESULT
        ]

        assert errors and errors[0] is not None
        assert "ウィンドウ" in errors[0]
        assert "/restart-claude" not in errors[0]

    async def test_runner_still_reports_a_dead_pane_as_a_dead_pane(self) -> None:
        """The window exists, so #527's wording (and its advice) is still right."""
        from c_lord.claude.tmux_runner import TmuxClaudeRunner

        tmux = MagicMock()
        tmux.is_claude_running.return_value = False
        tmux.start_claude.return_value = False
        tmux.session_exists.return_value = True

        runner = TmuxClaudeRunner(tmux_manager=tmux, thread_id=42, model="sonnet")
        errors = [
            ev.error async for ev in runner.run("hi") if ev.message_type is MessageType.RESULT
        ]

        assert errors and errors[0] is not None
        assert "/restart-claude" in errors[0]
