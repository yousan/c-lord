"""Which text the cursor lands on when a turn's last text went out early (#1186, #1187).

The mirror remembers the answer it delivered (the cursor) so a restart does not
post it again. Two ways it still came back a second time:

- #1186: text A → tool → text B, and B went out as the answer by the idle flush
  (the next tool call took longer than the idle window). At the turn end the
  #1125 rule then adopted A — the text before the last tool call — and moved the
  cursor back before B, so the next restart posted B again.
- #1187: the bot restarted between "text A → tool call" and the turn's end. The
  memory of A (posted silently by the old process) was lost with the process, so
  no cursor was committed for the turn and the next restart posted A again.

The other two tests pin the #1125 boundaries the fix must keep.
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

THREAD_ID = 1150


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


def _user(text, uuid):
    return {
        "type": "user",
        "uuid": uuid,
        "isSidechain": False,
        "message": {"role": "user", "content": text},
    }


def _text(text, uuid):
    return {
        "type": "assistant",
        "uuid": uuid,
        "isSidechain": False,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _tool(tid, uuid):
    return {
        "type": "assistant",
        "uuid": uuid,
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "true"}}
            ],
        },
    }


def _result(tid, uuid):
    return {
        "type": "user",
        "uuid": uuid,
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}],
        },
        "toolUseResult": {"stdout": "ok", "stderr": "", "interrupted": False},
    }


END = {"type": "system", "subtype": "turn_duration", "uuid": "end-1", "isSidechain": False}


class _Run:
    def __init__(self) -> None:
        self.posted: list[tuple[str, str]] = []
        self.ids: dict[str, int] = {}
        self.cursor: list[str] = []

    def _record(self, kind, text):
        self.posted.append((kind, text))
        self.ids[text] = 40_000 + len(self.posted)
        return SimpleNamespace(id=self.ids[text])

    async def sink(self, text: str):
        return self._record("silent", text)

    async def reply_sink(self, text: str, **_: object) -> None:
        reply_tracker.record_reply_message(THREAD_ID, self._record("reply", text))

    async def file_sink(self, text: str, _path: str, **_: object) -> None:
        await self.reply_sink(text)

    async def cursor_sink(self, uuid: str) -> None:
        self.cursor.append(uuid)


async def _replay(project: Path, steps, *, idle: float = 0) -> _Run:
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
        idle_flush_seconds=idle,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        thread = SimpleNamespace(id=THREAD_ID, parent_id=1, guild=None)
        info = turn_events.begin_turn(thread, StartedBy("webhook", "7", "Captain Hook"))
        mirror.note_turn_started(trigger_id=42)
        for row, wait in steps:
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            await asyncio.sleep(wait)
        await asyncio.sleep(0.2)
        turn_events.end_turn(info)
        turn_events.finish_turn(info)
        await turn_events.drain()
    finally:
        await mirror.stop()
    return run


async def test_idle_flushed_later_text_stays_the_answer(tmp_path, log):
    """text A → tool → text B (idle-flushed as the reply: the next tool_use took
    longer than the idle window to stream) → tool → turn end.
    B is the turn's last word; adopting A moves the cursor back before B."""
    steps = [
        (_user("依頼", "u0"), 0.05),
        (_text("途中の一言A", "a1"), 0.05),
        (_tool("t1", "a2"), 0.05),
        (_result("t1", "u1"), 0.05),
        (_text("最後の返事B", "a3"), 0.8),  # > idle window
        (_tool("t2", "a4"), 0.05),
        (_result("t2", "u2"), 0.05),
        (END, 0.05),
    ]
    project = tmp_path / "proj"
    run = await _replay(project, steps, idle=0.3)
    print("posted", run.posted, "cursor", run.cursor)
    events = [json.loads(line) for line in log.read_text().splitlines()]
    print("events", events)
    print("rescue with the committed cursor:", final_answer_needs_recovery(project, run.cursor[-1]))
    assert run.cursor[-1] == "a3", run.cursor
    assert final_answer_needs_recovery(project, run.cursor[-1]) is None
    assert events[-1]["last_message_id"] == str(run.ids["最後の返事B"])


async def test_two_silent_texts_last_one_is_the_answer(tmp_path, log):
    steps = [
        (_user("依頼", "u0"), 0.05),
        (_text("A", "a1"), 0.05),
        (_tool("t1", "a2"), 0.05),
        (_result("t1", "u1"), 0.05),
        (_text("B", "a3"), 0.05),
        (_tool("t2", "a4"), 0.05),
        (_result("t2", "u2"), 0.05),
        (END, 0.05),
    ]
    project = tmp_path / "proj"
    run = await _replay(project, steps)
    print(run.posted, run.cursor)
    assert run.cursor == ["a3"]
    assert final_answer_needs_recovery(project, "a3") is None


async def test_tool_only_turn_after_a_text_turn_adopts_nothing(tmp_path, log):
    steps = [
        (_user("依頼1", "u0"), 0.05),
        (_text("A", "a1"), 0.05),
        (_tool("t1", "a2"), 0.05),
        (_result("t1", "u1"), 0.05),
        (END, 0.05),
        (_user("依頼2", "u5"), 0.05),
        (_tool("t2", "a4"), 0.05),
        (_result("t2", "u2"), 0.05),
        ({**END, "uuid": "end-2"}, 0.05),
    ]
    project = tmp_path / "proj"
    run = await _replay(project, steps)
    print(run.posted, run.cursor)
    assert run.cursor == ["a1"]


async def _restart_mid_turn(tmp_path: Path, *, old_process_posted: bool) -> _Run:
    project = tmp_path / "proj"
    project.mkdir()
    if old_process_posted:
        from c_lord.transcript.mirror import LAST_SILENT_FILENAME

        (project / LAST_SILENT_FILENAME).write_text("a1")
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    with jsonl.open("a", encoding="utf-8") as f:
        for r in (_user("依頼", "u0"), _text("予約しました", "a1"), _tool("t1", "a2")):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.utime(jsonl, (1, 1))
    run = _Run()
    m = TranscriptMirror(
        thread_id=THREAD_ID,
        project_dir=project,
        sink=run.sink,
        reply_sink=run.reply_sink,
        file_sink=run.file_sink,
        reply_cursor_sink=run.cursor_sink,
        poll_interval=0.02,
        idle_flush_seconds=0,
        resumed=True,
    )
    m.start()
    try:
        await asyncio.sleep(0.15)
        with jsonl.open("a", encoding="utf-8") as f:
            for r in (_result("t1", "u1"), END):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        await asyncio.sleep(0.3)
    finally:
        await m.stop()
    run.project = project  # type: ignore[attr-defined]
    return run


async def test_restart_between_silent_text_and_turn_end(tmp_path, log):
    """Bot restarts after 'text A → tool_use' was mirrored; the turn ends after the
    restore. A was posted (silently) by the old process; the cursor should still
    land on A so the next restart does not re-post it."""
    run = await _restart_mid_turn(tmp_path, old_process_posted=True)
    assert run.cursor == ["a1"]


async def test_text_written_while_no_process_read_is_not_taken_as_posted(tmp_path, log):
    """Review of #1187: the tail resumes at the end, so A may have been written while
    the bot was down and posted by nobody. Then the cursor stays put and the next
    restart's rescue delivers it — late, not lost."""
    run = await _restart_mid_turn(tmp_path, old_process_posted=False)
    assert "a1" not in run.cursor
    rescue = final_answer_needs_recovery(run.project, run.cursor[-1] if run.cursor else None)
    assert rescue is not None and rescue.uuid == "a1"


async def test_a_silent_text_is_recorded_as_posted(tmp_path, log):
    """#1187: the live mirror leaves the proof the next process needs."""
    from c_lord.transcript.mirror import LAST_SILENT_FILENAME

    project = tmp_path / "p"
    await _replay(
        project, [(_user("依頼", "u0"), 0.05), (_text("A", "a1"), 0.05), (_tool("t1", "a2"), 0.1)]
    )
    assert (project / LAST_SILENT_FILENAME).read_text() == "a1"


def _write_rows(path: Path, *rows: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_read_back_finds_the_text_before_the_last_tool_call(tmp_path):
    """#1187: what the restored mirror takes as the turn's silent text."""
    from c_lord.transcript.mirror import _silent_text_at_end

    path = tmp_path / "s.jsonl"
    _write_rows(path, _user("依頼", "u0"), _text("A", "a1"), _tool("t1", "a2"))
    assert _silent_text_at_end(path) == "a1"


def test_read_back_leaves_a_held_text_to_the_old_process(tmp_path):
    """The old process flushed a still-held text as the reply when it stopped."""
    from c_lord.transcript.mirror import _silent_text_at_end

    path = tmp_path / "s.jsonl"
    _write_rows(
        path, _user("依頼", "u0"), _text("A", "a1"), _tool("t1", "a2"), _result("t1", "u1"),
        _text("B", "a3"),
    )  # fmt: skip
    assert _silent_text_at_end(path) is None


def test_read_back_does_not_carry_a_text_across_a_new_request(tmp_path):
    from c_lord.transcript.mirror import _silent_text_at_end

    path = tmp_path / "s.jsonl"
    _write_rows(
        path, _user("依頼1", "u0"), _text("A", "a1"), _tool("t1", "a2"), _result("t1", "u1"),
        END, _user("依頼2", "u5"), _tool("t2", "a4"),
    )  # fmt: skip
    assert _silent_text_at_end(path) is None
