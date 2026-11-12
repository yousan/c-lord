"""A turn that says its answer and then makes one last tool call (#1125).

"予約しました" → ``ScheduleWakeup`` → its result → turn end. The text went out
silently as an intermediate (a tool call followed it), the turn ended with no
answer — no cursor, ``last_message_id: null`` — and the next restart's #215
rescue posted the same text a second time. The text is the turn's last word,
which is what the rescue picks too, so it takes the answer's place live.

``i1125_text_then_final_tool.jsonl`` is the staging-1 transcript of thread
1514085380666691664 (2026-10-29 21:16Z, #1093 検証), unchanged but for the
session ids.
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
from c_lord.transcript.recovery import final_answer_needs_recovery
from c_lord.turn_events import StartedBy

from .helpers import clord_transcript

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "transcripts"
THREAD_ID = 1125
ANSWER_UUID = "ba5a83ff-8eb6-4564-b03a-4b03f7335e4e"


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


def _rows() -> list[dict]:
    text = (_FIXTURES / "i1125_text_then_final_tool.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class _Run:
    def __init__(self) -> None:
        self.posted: list[str] = []
        self.cursor: list[str] = []

    async def sink(self, text: str) -> SimpleNamespace:
        self.posted.append(text)
        return SimpleNamespace(id=30_000 + len(self.posted))

    async def reply_sink(self, text: str, **_: object) -> None:
        self.posted.append(text)
        reply_tracker.record_reply_message(THREAD_ID, SimpleNamespace(id=30_000 + len(self.posted)))

    async def file_sink(self, text: str, _path: str, **_: object) -> None:
        await self.reply_sink(text)

    def said(self) -> list[str]:
        """What Claude said (the prompt's 👤 echo left out)."""
        return [p for p in self.posted if not p.startswith("👤")]

    async def cursor_sink(self, uuid: str) -> None:
        self.cursor.append(uuid)


async def _replay(project: Path, rows: list[dict]) -> _Run:
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
        reply_cursor_sink=run.cursor_sink,
        poll_interval=0.02,
        idle_flush_seconds=0,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        thread = SimpleNamespace(id=THREAD_ID, parent_id=1, guild=None)
        info = turn_events.begin_turn(thread, StartedBy("webhook", "7", "Captain Hook"))
        mirror.note_turn_started(trigger_id=42)
        for row in rows:
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()
    finally:
        await mirror.stop()
    return run


async def test_the_text_before_the_last_tool_call_is_the_answer(tmp_path, log):
    """AC1/AC2: posted once, and the turn-end event names it."""
    run = await _replay(tmp_path / "proj", _rows())

    assert run.said() == ["予約しました"]
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["kind"] for e in events] == ["completion"]
    assert events[0]["last_message_id"] == str(30_000 + run.posted.index("予約しました") + 1)


async def test_the_restart_rescue_does_not_post_it_again(tmp_path, log):
    """AC3: the cursor is on it, so #215 finds nothing to re-deliver."""
    project = tmp_path / "proj"
    run = await _replay(project, _rows())

    assert run.cursor == [ANSWER_UUID]
    assert final_answer_needs_recovery(project, run.cursor[-1]) is None


async def test_an_answer_after_the_tools_is_still_the_answer(tmp_path, log):
    """A text after the last tool call stays the answer; the earlier one stays intermediate."""
    rows = _rows()
    end = rows.pop()
    rows.append(
        {
            "type": "assistant",
            "uuid": "final-answer",
            "isSidechain": False,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "本当の答え"}]},
        }
    )
    rows.append(end)
    run = await _replay(tmp_path / "proj", rows)

    assert run.said() == ["予約しました", "本当の答え"]
    assert run.cursor == ["final-answer"]
