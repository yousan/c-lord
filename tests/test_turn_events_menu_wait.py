"""No ``completion`` while the turn's menu is still waiting for an answer (#1170).

Production, 2026-10-31 23:06 (``fixtures/prod_logs/i1170_completion_while_menu_waits.log``):
the mirror posted an AskUserQuestion menu (``blocked``), the runner's own
bridge stood down (#535), and five minutes later its inactivity backstop saw
the pane was a menu waiting for an answer — not a hang (#751) — and ended the
run with no error. ``_run_helper`` took that as an ordinary end and sent
``completion`` (``last_message_id: null``) for a turn that was parked on a
question nobody had answered. The dispatcher was told "done"; and when an
answer did come, the turn's real end would have had nothing left to send.

The spec (``docs/specs/turn-end-events.md``) says ``blocked`` is followed by
``completion`` once the answer is in and the turn ends. So a run that ended
waiting on a menu holds its completion until the transcript says the turn
ended — and a new request drops it, as for background agents.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord import turn_events
from c_lord.skills import reply_tracker
from c_lord.turn_events import StartedBy

PROD_LOG = (
    Path(__file__).parent / "fixtures" / "prod_logs" / "i1170_completion_while_menu_waits.log"
).read_text()
PROD_EVENTS = [json.loads(m) for m in re.findall(r"^event (\{.*\})$", PROD_LOG, re.M)]
THREAD_ID = 1566090874880794704
BLOCKED_MESSAGE_ID = int(PROD_EVENTS[0]["last_message_id"])
DISPATCHER = StartedBy(
    "webhook", PROD_EVENTS[0]["started_by"]["id"], PROD_EVENTS[0]["started_by"]["name"]
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        turn_events.ENV_LOG,
        turn_events.ENV_URL,
        turn_events.ENV_SECRET,
        turn_events.ENV_DEFER_MAX,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(turn_events, "_RETRY_DELAYS", (0.0, 0.0))
    turn_events.reset()
    reply_tracker.reset_tracker()
    yield
    turn_events.reset()
    reply_tracker.reset_tracker()


@pytest.fixture
def log(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "turn-events.jsonl"
    monkeypatch.setenv(turn_events.ENV_LOG, str(path))
    return path


def _kinds(path: Path) -> list[str]:
    if not path.exists():
        return []
    # Deliveries run as separate tasks, so only the set of kinds is fixed.
    return sorted(json.loads(line)["kind"] for line in path.read_text().splitlines())


def _thread() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.parent_id = 1549214930706632754
    thread.guild = SimpleNamespace(id=1285582352596209694)
    thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return thread


def _config(thread: MagicMock, *, awaiting_answer: bool):
    """The production run: the mirror posts the menu, then the backstop ends the run."""
    from c_lord.claude.tmux_runner import TmuxClaudeRunner
    from c_lord.claude.types import MessageType, StreamEvent
    from c_lord.cogs.run_config import RunConfig

    runner = MagicMock(spec=TmuxClaudeRunner)

    async def run(prompt, session_id=None):  # noqa: ARG001
        yield StreamEvent(message_type=MessageType.SYSTEM, session_id="s1")
        # 23:10:54 — the mirror bridged the menu first.
        turn_events.emit_blocked(thread, message_id=BLOCKED_MESSAGE_ID)
        # 23:16:01 — the #751 backstop: a menu waiting for its answer, no error.
        runner.ended_awaiting_answer = awaiting_answer
        yield StreamEvent(message_type=MessageType.RESULT, is_complete=True, error=None)

    runner.run = run
    runner.preempted = False
    runner.working_dir = None
    return RunConfig(thread=thread, runner=runner, prompt="go", started_by=DISPATCHER)


async def _run_production_turn(monkeypatch, thread: MagicMock) -> None:
    from c_lord.cogs import _run_helper

    monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
    await _run_helper.run_claude_with_config(_config(thread, awaiting_answer=True))
    await turn_events.drain()


def test_the_fixture_is_the_production_sequence() -> None:
    assert "inactivity backstop: pane is a menu waiting" in PROD_LOG
    assert "run_claude: exit (outcome=ok" in PROD_LOG
    # What production sent: blocked, then a completion with no reply in it.
    assert [e["kind"] for e in PROD_EVENTS] == ["blocked", "completion"]
    assert PROD_EVENTS[1]["last_message_id"] is None


async def test_no_completion_while_the_menu_waits(log, monkeypatch) -> None:
    """AC1: the run ended on an unanswered menu — only ``blocked`` goes out."""
    await _run_production_turn(monkeypatch, _thread())
    assert _kinds(log) == ["blocked"]


async def test_the_turn_end_after_the_answer_sends_it(log, monkeypatch) -> None:
    """AC2: answered, Claude finishes the turn; its end in the transcript sends one completion."""
    await _run_production_turn(monkeypatch, _thread())
    turn_events.note_turn_end(THREAD_ID, 0)
    await turn_events.drain()

    assert _kinds(log) == ["blocked", "completion"]
    (event,) = [
        e for e in map(json.loads, log.read_text().splitlines()) if e["kind"] == "completion"
    ]
    assert event["started_by"]["name"] == "clord-dispatch"
    assert event.get("reason") is None


async def test_a_new_request_drops_it(log, monkeypatch) -> None:
    """AC3: unanswered, the next request (23:28) dismissed the menu — its own end speaks."""
    thread = _thread()
    await _run_production_turn(monkeypatch, thread)
    turn_events.begin_turn(thread, DISPATCHER)
    turn_events.note_turn_end(THREAD_ID, 0)  # the dismissed turn's end in the transcript
    await turn_events.drain()

    assert _kinds(log) == ["blocked"]


async def test_the_hold_does_not_time_out(log, monkeypatch) -> None:
    """AC4: an answer may take hours; the 1h agent cap is not a reason to say "done"."""
    monkeypatch.setenv(turn_events.ENV_DEFER_MAX, "0.01")
    await _run_production_turn(monkeypatch, _thread())
    import asyncio

    await asyncio.sleep(0.05)
    await turn_events.drain()

    assert _kinds(log) == ["blocked"]


async def test_an_ordinary_end_is_unchanged(log, monkeypatch) -> None:
    """A run that did not end on a menu still sends its completion at once."""
    from c_lord.cogs import _run_helper

    monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
    await _run_helper.run_claude_with_config(_config(_thread(), awaiting_answer=False))
    await turn_events.drain()

    assert _kinds(log) == ["blocked", "completion"]


async def test_an_answer_whose_end_beats_the_run_end_still_completes(log, monkeypatch) -> None:
    """W57 review 1: answered and ended before ``finish_turn`` — completion goes out."""
    from c_lord.cogs import _run_helper

    monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
    config = _config(_thread(), awaiting_answer=True)
    original = config.runner.run

    async def run(prompt, session_id=None):
        async for event in original(prompt, session_id=session_id):
            if event.is_complete:
                turn_events.note_turn_end(THREAD_ID, 0)  # the answer's turn end lands first
            yield event

    config.runner.run = run
    await _run_helper.run_claude_with_config(config)
    await turn_events.drain()
    assert _kinds(log) == ["blocked", "completion"]


async def test_answered_with_agents_out_gets_the_agent_time_limit(log, monkeypatch) -> None:
    """W57 review 2: once answered, a wait on agents is #917's — it expires."""
    import asyncio

    monkeypatch.setenv(turn_events.ENV_DEFER_MAX, "0.01")
    await _run_production_turn(monkeypatch, _thread())
    turn_events.note_turn_end(THREAD_ID, 1)  # answered; one agent never reports
    await asyncio.sleep(0.05)
    await turn_events.drain()
    assert _kinds(log) == ["blocked", "completion"]


def _with_stale_end(config, outstanding: int):  # noqa: ANN001, ANN202
    """The interrupted previous turn's end reaches the mirror after this run began (#1195)."""
    original = config.runner.run

    async def run(prompt, session_id=None):  # noqa: ANN001, ANN202
        first = True
        async for event in original(prompt, session_id=session_id):
            if first:
                turn_events.note_turn_end(THREAD_ID, outstanding)
                first = False
            yield event

    config.runner.run = run
    return config


async def test_the_previous_turns_late_end_is_not_the_answer(log, monkeypatch) -> None:
    """#1195: an end from before the menu was posted cannot be the answer's end."""
    from c_lord.cogs import _run_helper

    monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
    config = _with_stale_end(_config(_thread(), awaiting_answer=True), 0)
    await _run_helper.run_claude_with_config(config)
    await turn_events.drain()
    assert _kinds(log) == ["blocked"]


async def test_the_previous_turns_late_end_with_agents_does_not_time_out(log, monkeypatch) -> None:
    """#1195: nor does its agents' wait put the menu's hold on #917's clock."""
    import asyncio

    from c_lord.cogs import _run_helper

    monkeypatch.setenv(turn_events.ENV_DEFER_MAX, "0.01")
    monkeypatch.setattr(_run_helper, "_post_context_usage", AsyncMock())
    config = _with_stale_end(_config(_thread(), awaiting_answer=True), 1)
    await _run_helper.run_claude_with_config(config)
    await asyncio.sleep(0.05)
    await turn_events.drain()
    assert _kinds(log) == ["blocked"]
