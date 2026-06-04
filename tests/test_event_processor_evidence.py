"""Auto-evidence integration: EventProcessor captures a screenshot on completion (#243)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.types import MessageType, StreamEvent
from c_lord.cogs.event_processor import EventProcessor
from c_lord.cogs.run_config import RunConfig
from c_lord.discord_ui import evidence


@pytest.fixture(autouse=True)
def _reset_debounce():
    evidence._last_capture.clear()
    yield
    evidence._last_capture.clear()


def _thread() -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = 555
    t.name = "evi"
    t.parent_id = 1
    t.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return t


def _config(thread: MagicMock, **kwargs) -> RunConfig:
    runner = MagicMock()
    runner.interrupt = AsyncMock()
    return RunConfig(thread=thread, runner=runner, prompt="p", **kwargs)


def _complete() -> StreamEvent:
    return StreamEvent(message_type=MessageType.RESULT, is_complete=True, session_id="s1")


class TestAutoEvidenceOnComplete:
    async def test_capture_called_on_complete(self, monkeypatch) -> None:
        spy = AsyncMock(return_value="/x/.evidence/a.png")
        monkeypatch.setattr(evidence, "capture_evidence", spy)
        monkeypatch.delenv("CLORD_AUTO_SCREENSHOT", raising=False)

        thread = _thread()
        p = EventProcessor(_config(thread, working_dir="/x"))
        await p.process(_complete())

        spy.assert_awaited_once()
        args, kwargs = spy.await_args
        assert args[0] is thread
        assert args[1] == "/x" or kwargs.get("working_dir") == "/x"

    async def test_disabled_env_skips_capture(self, monkeypatch) -> None:
        spy = AsyncMock()
        monkeypatch.setattr(evidence, "capture_evidence", spy)
        monkeypatch.setenv("CLORD_AUTO_SCREENSHOT", "0")

        p = EventProcessor(_config(_thread(), working_dir="/x"))
        await p.process(_complete())

        spy.assert_not_awaited()

    async def test_debounce_skips_rapid_second(self, monkeypatch) -> None:
        spy = AsyncMock()
        monkeypatch.setattr(evidence, "capture_evidence", spy)
        monkeypatch.delenv("CLORD_AUTO_SCREENSHOT", raising=False)

        thread = _thread()
        p = EventProcessor(_config(thread, working_dir="/x"))
        await p.process(_complete())
        await p.process(_complete())

        assert spy.await_count == 1  # second completion debounced

    async def test_capture_failure_does_not_break_complete(self, monkeypatch) -> None:
        # capture_evidence already swallows errors, but the processor must also
        # be defensive: a raising capture must not propagate out of _on_complete.
        monkeypatch.setattr(evidence, "capture_evidence", AsyncMock(side_effect=RuntimeError("x")))
        monkeypatch.delenv("CLORD_AUTO_SCREENSHOT", raising=False)

        p = EventProcessor(_config(_thread(), working_dir="/x"))
        await p.process(_complete())  # must not raise
