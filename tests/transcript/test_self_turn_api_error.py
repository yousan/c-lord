"""A turn Claude started by itself that stops on an API error is a failure (#1131).

c-lord's own turns get ❌ and the Japanese advice from their runner (#908). A turn
a background notification or a scheduled wakeup started has no runner: on
2026-10-29 17:47 (W9) the thread only got ``-# ⚠️ API Error: …`` and the
turn-end event — the release of the request's held completion — said
``completion``.
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
THREAD_ID = 1131
AGENT = "a5ee4bb1c0ffee001"


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


def _api_error() -> dict:
    # The real row (W9 1565189852708999288, 2026-10-29 17:47:20 JST).
    return json.loads((_FIXTURES / "i1131_api_error_row.json").read_text(encoding="utf-8"))


def _row(kind: str, ts: str, **extra) -> dict:
    return {"type": kind, "timestamp": ts, "isSidechain": False, **extra}


def _text(ts: str, text: str) -> dict:
    return _row(
        "assistant", ts, message={"role": "assistant", "content": [{"type": "text", "text": text}]}
    )


def _notification(ts: str, task_id: str) -> list[dict]:
    """The queued notification (it releases the agent) and the prompt it becomes."""
    content = (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        "<status>completed</status>\n<summary>done</summary>\n</task-notification>"
    )
    return [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": ts, "content": content},
        _row("user", ts, message={"role": "user", "content": content}),
    ]


def _end(ts: str) -> dict:
    return _row("system", ts, subtype="turn_duration")


class _Run:
    def __init__(self) -> None:
        self.posted: list[str] = []
        self.notices: list[str] = []

    async def sink(self, text: str) -> None:
        self.posted.append(text)

    async def reply_sink(self, text: str, **_: object) -> None:
        reply_tracker.record_reply_message(THREAD_ID, SimpleNamespace(id=20_000 + len(self.posted)))
        self.posted.append(text)

    async def file_sink(self, text: str, _path: str, **_: object) -> None:
        await self.reply_sink(text)

    async def api_error_sink(self, text: str) -> None:
        self.notices.append(text)


async def _replay(tmp_path: Path, person: list[dict], later: list[dict], *, agents: int) -> _Run:
    """A person's turn (*person*) that leaves *agents* out, then Claude's own (*later*)."""
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))
    run = _Run()
    mirror = TranscriptMirror(
        thread_id=THREAD_ID,
        project_dir=project,
        sink=run.sink,
        reply_sink=run.reply_sink,
        file_sink=run.file_sink,
        poll_interval=0.02,
        idle_flush_seconds=0,
        api_error_sink=run.api_error_sink,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        thread = SimpleNamespace(id=THREAD_ID, parent_id=1505747831447883806, guild=None)
        info = turn_events.begin_turn(thread, StartedBy("webhook", "7", "Captain Hook"))
        mirror.note_turn_started(trigger_id=42)
        mirror._background_agents.update(f"{AGENT[:-1]}{i}" for i in range(agents))
        for event in person:
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        await asyncio.sleep(0.2)
        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()
        for event in later:
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        await turn_events.drain()
    finally:
        await mirror.stop()
    return run


_PERSON = [
    _row(
        "user",
        "2026-10-29T08:21:05.000Z",
        message={"role": "user", "content": "3本の候補を確認して"},
    ),
    _text("2026-10-29T08:22:40.000Z", "3本の候補確認が終わるのを待っています"),
    _end("2026-10-29T08:22:48.000Z"),
]


async def test_held_completion_is_released_as_failure_on_an_api_error(tmp_path, log):
    """AC1 (the 10/29 17:47 shape): the request's held completion goes out as failure."""
    later = [
        *_notification("2026-10-29T08:40:00.000Z", f"{AGENT[:-1]}0"),
        _text("2026-10-29T08:44:10.000Z", "I'll put the shared check in …"),
        _api_error(),
        _end("2026-10-29T08:47:21.000Z"),
    ]
    run = await _replay(tmp_path, _PERSON, later, agents=1)

    events = _lines(log)
    assert [(e["kind"], e.get("reason")) for e in events] == [("failure", "api_error")]
    assert events[0]["started_by"]["type"] == "webhook"
    # AC2: one Japanese notice, besides the folded English line
    assert len(run.notices) == 1
    assert run.notices[0].startswith("API Error: The response stopped arriving.")


async def test_a_self_turn_of_its_own_is_a_failure_on_an_api_error(tmp_path, log):
    """AC1: with nothing held, the self-started turn's own event is the failure."""
    later = [
        *_notification("2026-10-29T08:40:00.000Z", "bdeadbeef"),
        _api_error(),
        _end("2026-10-29T08:47:21.000Z"),
    ]
    run = await _replay(tmp_path, _PERSON, later, agents=0)

    events = _lines(log)
    assert [(e["kind"], e.get("reason")) for e in events] == [
        ("completion", None),  # the person's turn, which ended normally
        ("failure", "api_error"),
    ]
    assert events[1]["started_by"]["type"] == "self"
    assert len(run.notices) == 1


async def test_an_error_claude_answered_past_is_not_a_failure(tmp_path, log):
    """The CLI retried and Claude went on: an ordinary completion, no notice."""
    later = [
        *_notification("2026-10-29T08:40:00.000Z", "bdeadbeef"),
        _api_error(),
        _text("2026-10-29T08:47:40.000Z", "続けます。終わりました"),
        _end("2026-10-29T08:47:50.000Z"),
    ]
    run = await _replay(tmp_path, _PERSON, later, agents=0)

    assert [e["kind"] for e in _lines(log)] == ["completion", "completion"]
    assert run.notices == []
