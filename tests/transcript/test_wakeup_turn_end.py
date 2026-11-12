"""A turn Claude starts on its own scheduled wakeup gets its end event too (#1092).

``ScheduleWakeup`` / ``/loop`` fire a prompt into the idle session. Claude Code
writes it as ``system/scheduled_task_fire`` followed by ``isMeta`` user rows —
not as a plain prompt — so the mirror never saw the turn start, nobody had
recorded a self-started turn, and its ``turn_duration`` announced nothing.
On 2026-10-29 every one of 37 such turns in production went without an event.

``i1092_loop_wakeups`` is thread 1565160116817498155 from 17:23Z to 17:59Z,
reduced to prompts, queue operations, replies and turn ends (bodies replaced
by placeholders): wakeups at 17:23, 17:40, 17:44, 17:48 and 17:59 — the last
two carry the extra "[N prior /loop wakeups …]" row — and one
``<task-notification>`` turn at 17:30, the only one that had an event.
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
from c_lord.transcript.mirror import TranscriptMirror, _turn_state_at_end
from c_lord.turn_events import StartedBy

from .helpers import clord_transcript

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "transcripts"

THREAD_ID = 1092


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


def _fixture() -> list[dict]:
    text = (_FIXTURES / "i1092_loop_wakeups.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


def _is_turn_end(event: dict) -> bool:
    return event.get("type") == "system" and event.get("subtype") == "turn_duration"


def _wakeup(ts: str) -> list[dict]:
    """A wakeup's own rows, as Claude Code writes them."""
    return [
        {
            "type": "system",
            "subtype": "scheduled_task_fire",
            "timestamp": ts,
            "isSidechain": False,
            "isMeta": False,
            "content": "Claude resuming /loop wakeup",
            "taskKind": "loop",
        },
        {
            "type": "user",
            "timestamp": ts,
            "isSidechain": False,
            "isMeta": True,
            "message": {"role": "user", "content": "(起床予定のプロンプト)"},
        },
    ]


def _reply(ts: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "isSidechain": False,
        "message": {"role": "assistant", "content": [{"type": "text", "text": f"返信 {ts}"}]},
    }


def _end(ts: str) -> dict:
    return {"type": "system", "subtype": "turn_duration", "timestamp": ts, "isSidechain": False}


class _Run:
    def __init__(self) -> None:
        self.posted: list[str] = []
        self.replies: list[str] = []
        self.reply_kwargs: list[dict] = []
        self.file_kwargs: list[dict] = []

    async def sink(self, text: str) -> None:
        self.posted.append(text)

    def _record(self, text: str) -> None:
        # what the cog's reply / file sinks do once the message is out
        reply_tracker.record_reply_message(THREAD_ID, SimpleNamespace(id=10_000 + len(self.posted)))
        self.posted.append(text)

    async def reply_sink(self, text: str, **kwargs: int | bool) -> None:
        self.replies.append(text)
        self.reply_kwargs.append(kwargs)
        self._record(text)

    async def file_sink(self, text: str, _path: str, **kwargs: int | bool) -> None:
        self.file_kwargs.append(kwargs)
        self._record(text)


async def _replay(tmp_path: Path, events: list[dict], *, person_turn: bool = False) -> _Run:
    """Stream *events* into a running mirror; with *person_turn*, c-lord opened a turn first."""
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
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        info = None
        if person_turn:
            thread = SimpleNamespace(id=THREAD_ID, parent_id=1505747831447883806, guild=None)
            info = turn_events.begin_turn(thread, StartedBy("webhook", "7", "Captain Hook"))
            mirror.note_turn_started(trigger_id=42)
        for event in events:
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            if _is_turn_end(event):
                await asyncio.sleep(0.15)
                if info is not None:
                    turn_events.end_turn(info)
                    turn_events.finish_turn(info)
                    info = None
                await turn_events.drain()
        await asyncio.sleep(0.15)
        await turn_events.drain()
    finally:
        await mirror.stop()
    return run


async def test_every_wakeup_turn_gets_a_self_completion(tmp_path, log):
    """AC1/AC2: five wakeups and one notification — six turns, six events."""
    await _replay(tmp_path, _fixture())

    events = _lines(log)
    assert len(events) == 6, [e.get("turn_started_at") for e in events]
    for event in events:
        assert event["kind"] == "completion"
        assert event["started_by"] == {"type": "self", "id": None, "name": None}
        assert event["reason"] == "self_resumed_turn"
        assert event["thread_id"] == str(THREAD_ID)
    started = [e["turn_started_at"][11:19] for e in events]
    assert started == ["17:23:00", "17:30:06", "17:40:00", "17:44:00", "17:48:00", "17:59:00"]
    # each names the report its own turn posted
    assert len({e["last_message_id"] for e in events}) == 6


async def test_a_wakeup_inside_a_persons_turn_leaves_it_theirs(tmp_path, log):
    """AC3: one turn, one event — the person's, not a second self-started one."""
    events = [*_wakeup("2026-10-29T18:00:00.000Z"), _reply("2026-10-29T18:00:05.000Z")]
    events.append(_end("2026-10-29T18:00:06.000Z"))
    await _replay(tmp_path, events, person_turn=True)

    lines = _lines(log)
    assert [e["started_by"]["type"] for e in lines] == ["webhook"]
    assert "reason" not in lines[0]


async def test_a_wakeup_reply_is_still_posted_as_before(tmp_path, log):
    """AC4: only the event is new — the answer goes out through the same sink."""
    events = [*_wakeup("2026-10-29T18:00:00.000Z"), _reply("2026-10-29T18:00:05.000Z")]
    events.append(_end("2026-10-29T18:00:06.000Z"))
    run = await _replay(tmp_path, events)

    assert run.replies == ["返信 2026-10-29T18:00:05.000Z"]
    assert [e["reason"] for e in _lines(log)] == ["self_resumed_turn"]


def test_read_back_sees_a_wakeup_turn_still_open(tmp_path):
    """A mirror restored mid-wakeup knows a turn is open, so a notice inside it is no new turn."""
    path = tmp_path / "s.jsonl"
    rows = [*_wakeup("2026-10-29T18:00:00.000Z"), _reply("2026-10-29T18:00:05.000Z")]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert _turn_state_at_end(path) == ("wakeup", "2026-10-29T18:00:00.000Z", set())

    with path.open("a") as f:
        f.write(json.dumps(_end("2026-10-29T18:00:06.000Z")) + "\n")
    assert _turn_state_at_end(path) == (None, None, set())


# --- #1138: a wakeup's answer is not a quote-reply to an old request ----------


def _tool_use(ts: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_w",
                    "name": "Bash",
                    "input": {"command": "gh pr view"},
                }
            ],
        },
    }


def _tool_result(ts: str) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_w", "content": "OPEN"}],
        },
    }


async def test_a_wakeup_answer_is_not_quoted_1138(tmp_path, log):
    """AC1: scheduled_task_fire → isMeta prompt → reply → turn end: no quote."""
    events = [*_wakeup("2026-10-29T18:00:00.000Z"), _reply("2026-10-29T18:00:05.000Z")]
    events.append(_end("2026-10-29T18:00:06.000Z"))
    run = await _replay(tmp_path, events)

    assert run.replies == ["返信 2026-10-29T18:00:05.000Z"]
    assert run.reply_kwargs == [{"quote": False}]


async def test_a_wakeup_answer_after_tools_keeps_progress_without_quote_1138(tmp_path, log):
    """AC2: the same with a tool call — progress.txt rides, the quote does not."""
    events = [
        *_wakeup("2026-10-29T18:00:00.000Z"),
        _tool_use("2026-10-29T18:00:01.000Z"),
        _tool_result("2026-10-29T18:00:02.000Z"),
        _reply("2026-10-29T18:00:05.000Z"),
        _end("2026-10-29T18:00:06.000Z"),
    ]
    run = await _replay(tmp_path, events)

    assert run.file_kwargs == [{"quote": False}]
    assert run.replies == []


async def test_a_persons_answer_is_still_quoted_1138(tmp_path, log):
    """AC3: a turn a person started still answers them as a quote-reply."""
    events = [
        {
            "type": "user",
            "timestamp": "2026-10-29T18:00:00.000Z",
            "isSidechain": False,
            "message": {"role": "user", "content": "PR を見て"},
        },
        _reply("2026-10-29T18:00:05.000Z"),
        _end("2026-10-29T18:00:06.000Z"),
    ]
    run = await _replay(tmp_path, events, person_turn=True)

    assert run.replies == ["返信 2026-10-29T18:00:05.000Z"]
    assert run.reply_kwargs and all("quote" not in kw for kw in run.reply_kwargs)
