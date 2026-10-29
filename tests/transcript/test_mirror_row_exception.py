"""#1041: one row the mirror cannot handle must not stop the mirror.

The mirror is the thread's only delivery. ``_launched_agent_ids`` joined a
``tool_result``'s text parts with ``" ".join(part.get("text", ""))``; a part
whose ``text`` is ``null`` raised ``TypeError``, ``_run`` caught only
``CancelledError``, and the thread went silent for good with one INFO line —
``TranscriptMirror stopped``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from unittest import mock

import pytest

from c_lord.transcript import mirror as mirror_mod
from c_lord.transcript.mirror import TranscriptMirror, _launched_agent_ids

from .helpers import clord_transcript


def _user(content: object) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}}


def _null_text_result() -> dict:
    return _user(
        [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": None}]}]
    )


def test_a_null_text_part_is_not_an_error() -> None:
    assert _launched_agent_ids(_null_text_result()) == []


async def _run_mirror(tmp_path: Path, rows: list[dict]) -> list[str]:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = clord_transcript(project / "s.jsonl")
    os.utime(jsonl, (1, 1))
    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    mirror = TranscriptMirror(thread_id=1041, project_dir=project, sink=sink, poll_interval=0.05)
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        with jsonl.open("a", encoding="utf-8") as f:
            for e in rows:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        await asyncio.sleep(0.4)
    finally:
        await mirror.stop()
    return posted


async def test_the_issue_row_does_not_silence_the_thread(tmp_path: Path) -> None:
    posted = await _run_mirror(tmp_path, [_null_text_result(), _user("その後の依頼")])
    assert any("その後の依頼" in p for p in posted), posted


async def test_any_exception_on_a_row_skips_that_row_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2: whatever raises while handling one row, later rows still arrive."""
    real = mirror_mod._tool_call_ids
    calls = {"n": 0}

    def boom_once(event: dict):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected row shape")
        return real(event)

    with (
        mock.patch.object(mirror_mod, "_tool_call_ids", boom_once),
        caplog.at_level(logging.ERROR, logger="c_lord.transcript.mirror"),
    ):
        posted = await _run_mirror(tmp_path, [_user("一つ目"), _user("二つ目")])
    assert any("二つ目" in p for p in posted), posted
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "thread=1041" in errors[0].getMessage()
