"""#912: an interrupted turn's held text must not answer the NEW instruction.

Production, 2026-10-09 (W13 #29 交易). Claude wrote "Docs: VOYAGE.md (marker),
STORY.md …" at 15:36:16 as part of the turn it was in. A new instruction came
at 15:36:52 and interrupted that turn — no ``turn_duration`` was written. The
mirror was still holding the sentence as "maybe this turn's final answer", and
flushed it at 15:37:06 as a quote-reply to **whatever the trigger was by then**:
the new instruction. Discord read it as Claude's answer to the new instruction,
with the old turn's progress.txt attached.

What decides which request a held text answers is where it sits in the
transcript: before the new prompt, it belongs to the turn before.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from c_lord.transcript.mirror import TranscriptMirror

from .helpers import clord_transcript

OLD = 1557954000000000001
NEW = 1557954718586642502


def _write(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _prompt(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _tool() -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
            ]
        },
    }


def _tool_result() -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    }


class _Sinks:
    def __init__(self) -> None:
        self.replies: list[tuple[str, dict]] = []
        self.files: list[tuple[str, dict]] = []

    async def sink(self, text: str) -> None:
        pass

    async def reply(self, text: str, **kw) -> None:
        self.replies.append((text, kw))

    async def file(self, text: str, path: str, **kw) -> None:
        self.files.append((text, kw))

    def all(self) -> list[tuple[str, dict]]:
        return self.replies + self.files


def _quoted(kw: dict) -> int | str:
    """Which trigger a flush quoted: the explicit one, or "current"."""
    return kw.get("reply_to", "current")


async def _mirror(
    tmp_path: Path, sinks: _Sinks, *, idle: float = 0
) -> tuple[TranscriptMirror, Path]:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))
    mirror = TranscriptMirror(
        thread_id=912,
        project_dir=project,
        sink=sinks.sink,
        reply_sink=sinks.reply,
        file_sink=sinks.file,
        poll_interval=0.03,
        idle_flush_seconds=idle,
    )
    mirror.start()
    await asyncio.sleep(0.1)
    return mirror, jsonl


async def test_interrupted_text_answers_its_own_request(tmp_path: Path) -> None:
    """AC1/AC3 — the production shape, flushed when the new prompt is read.

    RED before #912: quoted the new instruction, progress.txt and all."""
    sinks = _Sinks()
    mirror, jsonl = await _mirror(tmp_path, sinks)
    try:
        mirror.note_turn_started(trigger_id=OLD)
        _write(jsonl, _prompt("ドキュメントを書いて"))
        _write(jsonl, _tool())
        _write(jsonl, _tool_result())
        _write(jsonl, _text("Docs: VOYAGE.md (marker), STORY.md (joao lines)"))
        await asyncio.sleep(0.2)
        # The new instruction interrupts: no turn_duration for the old turn.
        mirror.note_turn_started(trigger_id=NEW)
        _write(jsonl, _prompt("master を取り込んで rebase して"))
        await asyncio.sleep(0.2)
        _write(jsonl, _text("ドキュメントを仕上げてから、master を取り込みます。"))
        _write(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    flushed = {text: _quoted(kw) for text, kw in sinks.all()}
    old = "Docs: VOYAGE.md (marker), STORY.md (joao lines)"
    assert old in flushed, sinks.all()
    assert flushed[old] == OLD, f"the old turn's text must quote the old request: {flushed!r}"
    new = "ドキュメントを仕上げてから、master を取り込みます。"
    assert flushed[new] == "current", f"AC2: the new answer quotes the new request: {flushed!r}"
    # AC3: the old turn's progress.txt rides with the OLD text, never the new reply.
    assert [t for t, _ in sinks.files] == [old], sinks.files


async def test_interrupted_text_idle_flushed_after_the_new_request(tmp_path: Path) -> None:
    """The idle flush (#218) fires after c-lord accepted the new instruction but
    before the transcript shows it — still the old turn's text."""
    sinks = _Sinks()
    mirror, jsonl = await _mirror(tmp_path, sinks, idle=0.3)
    try:
        mirror.note_turn_started(trigger_id=OLD)
        _write(jsonl, _prompt("ドキュメントを書いて"))
        _write(jsonl, _text("常駐が起動し、ログ末尾から追い始めました。"))
        await asyncio.sleep(0.1)
        mirror.note_turn_started(trigger_id=NEW)
        await asyncio.sleep(0.6)
    finally:
        await mirror.stop()

    assert [(t, _quoted(kw)) for t, kw in sinks.all()] == [
        ("常駐が起動し、ログ末尾から追い始めました。", OLD)
    ]


async def test_an_uninterrupted_answer_still_quotes_the_current_request(tmp_path: Path) -> None:
    """AC2 — no new instruction in between: unchanged (no explicit target)."""
    sinks = _Sinks()
    mirror, jsonl = await _mirror(tmp_path, sinks)
    try:
        mirror.note_turn_started(trigger_id=OLD)
        _write(jsonl, _prompt("調べて"))
        _write(jsonl, _text("調べました。"))
        _write(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert [(t, _quoted(kw)) for t, kw in sinks.all()] == [("調べました。", "current")]


async def test_a_mirror_that_never_saw_the_request_keeps_quoting_the_current_one(
    tmp_path: Path,
) -> None:
    """A mirror restored after a restart does not know which request its held
    text came from — it keeps the old behaviour rather than guess."""
    sinks = _Sinks()
    mirror, jsonl = await _mirror(tmp_path, sinks)
    try:
        _write(jsonl, _text("途中の文"))
        await asyncio.sleep(0.1)
        mirror.note_turn_started(trigger_id=NEW)
        _write(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert [(t, _quoted(kw)) for t, kw in sinks.all()] == [("途中の文", "current")]


async def test_an_interrupted_turns_tool_log_is_not_attached_to_the_new_answer(
    tmp_path: Path,
) -> None:
    """AC3 — staging 2026-10-28: the interrupted turn ran a Write and wrote no
    text after it; the new instruction's 「了解」 went out with that turn's
    progress.txt. A tool log never crosses into the next instruction's turn."""
    sinks = _Sinks()
    mirror, jsonl = await _mirror(tmp_path, sinks)
    try:
        mirror.note_turn_started(trigger_id=OLD)
        _write(jsonl, _prompt("旅行記を書いて"))
        _write(jsonl, _text("ジュンビオーケー"))
        _write(jsonl, _tool())
        _write(jsonl, _tool_result())
        await asyncio.sleep(0.2)
        mirror.note_turn_started(trigger_id=NEW)
        _write(jsonl, _prompt("やっぱりやめて「了解」とだけ返して"))
        _write(jsonl, _text("了解"))
        _write(jsonl, {"type": "system", "subtype": "turn_duration"})
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert sinks.files == [], f"the new answer carried the old turn's progress.txt: {sinks.files}"
    assert [(t, _quoted(kw)) for t, kw in sinks.replies] == [("了解", "current")]
