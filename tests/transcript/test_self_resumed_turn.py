"""A turn Claude starts by itself on a ``<task-notification>`` gets its own end event (#961).

The runner's turn ends at Claude's first turn end — often "waiting for CI in the
background". When the background command finishes, Claude Code starts another
turn by itself, and *that* turn carries the real final report. Before #961 no
event marked its end, so a receiver learned of the report only from a safety net
up to 1.5 hours later.

The fixtures are the three misses of 2026-10-28, reduced from the production
transcripts to prompts, queue operations, replies and turn ends (prompt bodies
replaced by placeholders):

* ``i961_a_pytest_wait`` — thread 1564916440262385795: the runner turn ends at
  12:26:26 ("pytest 待ち"), then two self-started turns end at 12:50:11 and
  12:55:15 (the final report). The runner turn itself opened with a
  notification *and* a person's message in the same turn.
* ``i961_b_two_notes_mid_turn`` — thread 1564804476739657814: runner turn ends
  13:25:29; the self-started turn ends 14:04:59, with two more notifications
  queued and absorbed in the middle of it.
* ``i961_c_ci_wait`` — thread 1564916505735340182: runner turn ends 14:38:36
  ("#959 の CI 待ち"); the self-started turn ends 14:53:04.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from c_lord import turn_events
from c_lord.skills import reply_tracker
from c_lord.transcript.mirror import TranscriptMirror
from c_lord.turn_events import StartedBy

from .helpers import clord_transcript

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "transcripts"

THREAD_ID = 961


@pytest.fixture(autouse=True)
def _clean():
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


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _thread() -> SimpleNamespace:
    return SimpleNamespace(id=THREAD_ID, parent_id=1505747831447883806, guild=SimpleNamespace(id=1))


def _fixture(name: str) -> list[dict]:
    return [json.loads(line) for line in (_FIXTURES / f"{name}.jsonl").read_text().splitlines()]


def _is_turn_end(event: dict) -> bool:
    return event.get("type") == "system" and event.get("subtype") == "turn_duration"


async def _replay(tmp_path: Path, events: list[dict]) -> None:
    """Drive the mirror with *events* the way production does.

    c-lord accepts a webhook message (the runner's turn), so the mirror is told
    a turn started; the runner's turn is over at the first turn end. Everything
    after that is Claude's own doing.
    """
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))
    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    def record(text: str) -> None:
        # what the cog's reply / file sinks do once the message is out
        reply_tracker.record_reply_message(THREAD_ID, SimpleNamespace(id=10_000 + len(posted)))
        posted.append(text)

    async def reply_sink(text: str, **_: int) -> None:
        record(text)

    async def file_sink(text: str, _path: str, **_: int) -> None:
        record(text)

    mirror = TranscriptMirror(
        thread_id=THREAD_ID,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        file_sink=file_sink,
        poll_interval=0.02,
        idle_flush_seconds=0,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        info = turn_events.begin_turn(_thread(), StartedBy("webhook", "7", "Captain Hook"))
        mirror.note_turn_started()
        runner_open = True
        for event in events:
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            if _is_turn_end(event):
                await asyncio.sleep(0.15)
                if runner_open:
                    turn_events.end_turn(info)
                    turn_events.finish_turn(info)
                    runner_open = False
                await turn_events.drain()
        await asyncio.sleep(0.15)
        await turn_events.drain()
    finally:
        await mirror.stop()


@pytest.mark.parametrize(
    ("name", "self_ends"),
    [("i961_a_pytest_wait", 2), ("i961_b_two_notes_mid_turn", 1), ("i961_c_ci_wait", 1)],
)
async def test_each_self_started_turn_end_gets_a_completion(tmp_path, log, name, self_ends):
    await _replay(tmp_path, _fixture(name))

    events = _lines(log)
    assert [e["kind"] for e in events] == ["completion"] * (1 + self_ends)
    runner, *own = events
    assert runner["started_by"]["type"] == "webhook"
    assert "reason" not in runner
    for event in own:
        assert event["started_by"] == {"type": "self", "id": None, "name": None}
        assert event["reason"] == "self_resumed_turn"
        # inherited from the turn before, so the receiver can place it
        assert event["thread_id"] == str(THREAD_ID)
        assert event["channel_id"] == "1505747831447883806"
        assert event["guild_id"] == "1"
        # the report the turn posted is the one named (every one here ran tools,
        # so it went out as an answer with its progress.txt)
        assert event["last_message_id"] is not None
    assert len({e["last_message_id"] for e in events}) == len(events)
