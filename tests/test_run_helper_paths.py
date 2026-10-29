"""``_run_helper``: the ways out of a run that the main suites did not reach.

``run_claude_with_config`` logs ``enter`` / ``exit`` around every turn (#293) and
reports how the turn ended (#917). These tests drive the less common exits — a
runner that raises, a cancel that is the user's stop, an earlier run that never
logged ``exit`` — and the helpers around them, and check what the thread and the
log are told.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord import turn_events
from c_lord.claude.tmux_runner import (
    NO_RESPONSE_ERROR_PREFIX,
    STOPPED_BY_USER_ERROR,
    TmuxClaudeRunner,
)
from c_lord.claude.types import MessageType, StreamEvent, UsageLimit
from c_lord.cogs import _run_helper
from c_lord.cogs.run_config import RunConfig, RunOutcome

THREAD_ID = 5151
_LIMIT = UsageLimit(scope="weekly limit", resets_at=None, line="limit")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for var in (turn_events.ENV_LOG, turn_events.ENV_URL, turn_events.ENV_SECRET):
        monkeypatch.delenv(var, raising=False)
    turn_events.reset()
    _run_helper._IN_FLIGHT.clear()
    monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
    yield
    _run_helper._IN_FLIGHT.clear()
    turn_events.reset()


def _config(events: list[StreamEvent] | None = None, **kwargs: object) -> RunConfig:
    async def run(prompt: str, session_id: str | None = None):
        for event in events or []:
            yield event

    runner = MagicMock(spec=TmuxClaudeRunner)
    runner.run = run
    runner.preempted = False
    runner.stopped = False
    runner.working_dir = None
    runner.peek_pending_ask = AsyncMock(return_value=None)
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.parent_id = 777
    thread.guild = SimpleNamespace(id=555)
    thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return RunConfig(thread=thread, runner=runner, prompt="go", **kwargs)  # type: ignore[arg-type]


def _done(error: str | None = None) -> list[StreamEvent]:
    return [
        StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"),
        StreamEvent(message_type=MessageType.RESULT, is_complete=True, error=error),
    ]


def _exit_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "run_claude: exit" in r.getMessage()]


class TestFailureReason:
    """#917: the turn-end event carries a code for the failure, never its text."""

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (RunOutcome(), None),
            (RunOutcome(error="x", usage_limit=_LIMIT), "usage_limit"),
            (RunOutcome(error="x", login_required=True), "login_required"),
            (RunOutcome(error="x", model_unavailable="opus"), "model_unavailable"),
            (RunOutcome(error="x", api_error="advice"), "api_error"),
            (RunOutcome(error=STOPPED_BY_USER_ERROR), "stopped"),
            (RunOutcome(error="Timed out after 300 seconds"), "timeout"),
            (RunOutcome(error="x", no_response=True), "no_response"),
            (RunOutcome(error=f"{NO_RESPONSE_ERROR_PREFIX} pane"), "no_response"),
            (RunOutcome(error="something else"), "error"),
        ],
    )
    def test_each_failure_gets_its_code(self, outcome: RunOutcome, expected: str | None) -> None:
        config = _config()
        config.outcome = outcome

        assert _run_helper._failure_reason(config, "error") == expected

    def test_a_crash_is_a_crash_whatever_the_outcome_says(self) -> None:
        assert _run_helper._failure_reason(_config(), "crashed") == "crashed"


class TestRunnerRaises:
    async def test_the_thread_gets_an_error_embed_and_the_lamp_turns_red(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        status = MagicMock()
        status.set_error = AsyncMock()
        config = _config(status=status)

        async def boom(prompt: str, session_id: str | None = None):
            raise RuntimeError("tmux gone")
            yield  # pragma: no cover

        config.runner.run = boom  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO, logger="c_lord.cogs._run_helper"):
            await _run_helper.run_claude_with_config(config)

        embed = config.thread.send.await_args.kwargs["embed"]  # type: ignore[union-attr]
        assert "An unexpected error occurred." in (embed.description or "")
        status.set_error.assert_awaited_once()
        assert config.outcome.error == "An unexpected error occurred."
        (line,) = _exit_lines(caplog)
        assert "outcome=error" in line

    async def test_a_dead_discord_connection_does_not_turn_the_error_into_a_crash(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        status = MagicMock()
        status.set_error = AsyncMock(side_effect=OSError("closed"))
        config = _config(status=status)
        config.thread.send.side_effect = OSError("closed")  # type: ignore[attr-defined]

        async def boom(prompt: str, session_id: str | None = None):
            raise RuntimeError("tmux gone")
            yield  # pragma: no cover

        config.runner.run = boom  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO, logger="c_lord.cogs._run_helper"):
            await _run_helper.run_claude_with_config(config)

        (line,) = _exit_lines(caplog)
        assert "outcome=error" in line


class TestImageTempfiles:
    async def test_downloaded_images_are_deleted_after_the_turn(self, tmp_path: Path) -> None:
        kept = tmp_path / "a.png"
        kept.write_bytes(b"png")
        config = _config(_done(), image_paths=[str(kept), str(tmp_path / "already-gone.png")])

        await _run_helper.run_claude_with_config(config)

        assert not kept.exists()


class TestCancelledTurn:
    """#977: a stop that had to cancel the task is the user's stop, not a cancel."""

    async def _cancel_mid_turn(self, config: RunConfig) -> None:
        started = asyncio.Event()

        async def hang(prompt: str, session_id: str | None = None):
            started.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover

        config.runner.run = hang  # type: ignore[method-assign]
        task = asyncio.create_task(_run_helper.run_claude_with_config(config))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_a_stopped_runner_logs_outcome_stopped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _config()
        config.runner.stopped = True  # type: ignore[attr-defined]

        with caplog.at_level(logging.INFO, logger="c_lord.cogs._run_helper"):
            await self._cancel_mid_turn(config)

        (line,) = _exit_lines(caplog)
        assert "outcome=stopped" in line

    async def test_a_preempted_runner_stays_cancelled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _config()
        config.runner.stopped = True  # type: ignore[attr-defined]
        config.runner.preempted = True  # type: ignore[attr-defined]

        with caplog.at_level(logging.INFO, logger="c_lord.cogs._run_helper"):
            await self._cancel_mid_turn(config)

        (line,) = _exit_lines(caplog)
        assert "outcome=cancelled" in line
        assert THREAD_ID not in _run_helper._IN_FLIGHT


class TestTurnEndHandOffFailures:
    """The end of a turn never raises out of the ``finally`` (#815, #917)."""

    async def test_a_progress_line_that_cannot_end_is_logged_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(
            _run_helper, "note_run_ended", AsyncMock(side_effect=RuntimeError("mirror gone"))
        )

        with caplog.at_level(logging.WARNING, logger="c_lord.cogs._run_helper"):
            result = await _run_helper.run_claude_with_config(_config(_done()))

        assert result == "s1"
        assert any("could not end the progress line" in r.getMessage() for r in caplog.records)

    async def test_an_event_outlet_that_raises_is_logged_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(turn_events, "end_turn", MagicMock(side_effect=RuntimeError("x")))

        with caplog.at_level(logging.WARNING, logger="c_lord.cogs._run_helper"):
            result = await _run_helper.run_claude_with_config(_config(_done()))

        assert result == "s1"
        assert any("event outlet (#917)" in r.getMessage() for r in caplog.records)


class TestOrphanLedger:
    """#293: an earlier run that never logged ``exit`` is reported when the next starts."""

    async def test_an_ended_orphan_is_reported_once_and_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        dead = asyncio.get_running_loop().create_future()
        dead.set_result(None)
        orphan = _run_helper._InFlightRun(task=dead, started=0.0)  # type: ignore[arg-type]
        _run_helper._IN_FLIGHT[THREAD_ID] = [orphan]

        with caplog.at_level(logging.WARNING, logger="c_lord.cogs._run_helper"):
            await _run_helper.run_claude_with_config(_config(_done()))
            await _run_helper.run_claude_with_config(_config(_done()))

        orphans = [r.getMessage() for r in caplog.records if "orphan" in r.getMessage()]
        assert len(orphans) == 1
        assert "its task has already ended" in orphans[0]
        assert THREAD_ID not in _run_helper._IN_FLIGHT

    async def test_a_live_orphan_is_reported_and_left_alone(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        live = asyncio.get_running_loop().create_future()
        orphan = _run_helper._InFlightRun(task=live, started=0.0)  # type: ignore[arg-type]
        _run_helper._IN_FLIGHT[THREAD_ID] = [orphan]

        with caplog.at_level(logging.WARNING, logger="c_lord.cogs._run_helper"):
            await _run_helper.run_claude_with_config(_config(_done()))

        assert any("it is still running" in r.getMessage() for r in caplog.records)
        assert _run_helper._IN_FLIGHT[THREAD_ID] == [orphan]
        live.cancel()


class TestWithdrawInFlight:
    """#1010: a workspace stop withdraws every live run — except a displaced one."""

    def _run(self, *, done: bool = False, preempted: bool = False) -> _run_helper._InFlightRun:
        task = MagicMock()
        task.done.return_value = done
        runner = SimpleNamespace(preempted=preempted, withdraw=MagicMock())
        return _run_helper._InFlightRun(task=task, started=0.0, runner=runner)

    def test_only_live_undisplaced_runs_are_withdrawn(self) -> None:
        live = self._run()
        finished = self._run(done=True)
        displaced = self._run(preempted=True)
        _run_helper._IN_FLIGHT[THREAD_ID] = [finished, displaced, live]

        assert _run_helper.withdraw_in_flight(THREAD_ID) is True

        live.runner.withdraw.assert_called_once_with(silent=False)  # type: ignore[union-attr]
        finished.runner.withdraw.assert_not_called()  # type: ignore[union-attr]
        displaced.runner.withdraw.assert_not_called()  # type: ignore[union-attr]

    def test_nothing_to_withdraw_says_so(self) -> None:
        _run_helper._IN_FLIGHT[THREAD_ID] = [self._run(preempted=True)]

        assert _run_helper.withdraw_in_flight(THREAD_ID) is False


class TestFooterSettings:
    async def test_an_unreadable_setting_leaves_the_item_on(self) -> None:
        settings = MagicMock()
        settings.get = AsyncMock(side_effect=RuntimeError("db locked"))

        assert await _run_helper._context_footer_enabled(settings, "cost") is True

    async def test_a_cost_read_that_raises_leaves_the_cost_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from c_lord.claude.context_usage import ContextUsage

        monkeypatch.undo()  # the real _post_context_usage, not the autouse stub
        _run_helper._context_window_cache.clear()
        monkeypatch.setattr(
            _run_helper, "read_latest_usage", lambda _p: ContextUsage(input_tokens=10_000)
        )
        monkeypatch.setattr(_run_helper, "latest_session_jsonl", lambda _d: Path("/tmp/x.jsonl"))
        config = _config()
        config.runner.working_dir = "/tmp/w"  # type: ignore[misc]
        config.runner.probe_context_window = AsyncMock(return_value=200_000)  # type: ignore[attr-defined]

        async def no_cost() -> float:
            raise RuntimeError("pane gone")

        config.runner.get_cost_from_pane = no_cost  # type: ignore[attr-defined]

        await _run_helper._post_context_usage(config, "sess-cost")

        line = config.thread.send.await_args.args[0]  # type: ignore[union-attr]
        assert "$" not in line
        assert "5%" in line
