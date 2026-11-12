"""A menu that parks the turn sends one ``blocked`` event (#917)."""
# <<REDACTED 4 lines>>
import json
from pathlib import Path
# <<REDACTED 2 lines>>
import pytest

from c_lord import turn_events
from c_lord.discord_ui import ask_handler
from c_lord.claude.types import AskOption, AskQuestion
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_handler import bridge_pane_ask


@pytest.fixture(autouse=True)
def _outlet(tmp_path, monkeypatch):
    monkeypatch.setenv(turn_events.ENV_LOG, str(tmp_path / "events.jsonl"))
    turn_events.reset()
    yield
    turn_events.reset()


def _question() -> AskQuestion:
    return AskQuestion(
        question="Which?",
        header="Pick",
        options=[AskOption("A1", "one"), AskOption("A2", "two")],
    )


def _thread(thread_id: int) -> MagicMock:
    thread = MagicMock()
    thread.id = thread_id
    thread.parent_id = 7
    thread.guild.id = 5
    msg = MagicMock()
    msg.id = 8_000 + thread_id
    msg.edit = AsyncMock()
    thread.send = AsyncMock(return_value=msg)
    return thread


def _events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_the_pane_bridge_sends_blocked_when_the_menu_is_posted(tmp_path, monkeypatch):
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_POLL", 0.01)
    monkeypatch.setattr(ask_handler, "_PANE_RESOLVE_MISSES", 2)
    monkeypatch.setattr(ask_handler, "_ANSWER_CONFIRM_TIMEOUT", 0.05)
    monkeypatch.setattr(ask_handler, "_ANSWER_CONFIRM_POLL", 0.01)
    thread = _thread(917_002)
    runner = MagicMock()
    runner.peek_pending_ask = AsyncMock(return_value=_question())
    runner.peek_menu_state = AsyncMock(return_value=(_question(), True))
    runner.answer_menu = AsyncMock()
    runner.answer_menu_text = AsyncMock()
    runner.cancel_menu = AsyncMock()

    async def click() -> None:
        await asyncio.sleep(0.05)
        ask_bus.post_answer(thread.id, ["A2"])

    await asyncio.gather(
        asyncio.wait_for(bridge_pane_ask(thread, _question(), runner), timeout=3.0), click()
    )
    await turn_events.drain()

    assert [e["kind"] for e in _events(tmp_path)] == ["blocked"]
