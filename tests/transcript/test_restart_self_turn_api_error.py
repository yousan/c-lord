"""#1179: a turn Claude started by itself that stops on an API error across a restart.

The API-error row was written before c-lord restarted and the ``turn_duration``
after it. The new process read the open turn back (#1093) but not the error, so
the turn was recorded as a ``completion`` and the thread never got its ❌ — the
same turn without a restart in between is a ``failure`` with one notice (#1131).
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

from . import test_self_turn_api_error as a
from . import test_wakeup_turn_end as w
from .helpers import clord_marker_event

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
    return [json.loads(x) for x in path.read_text().splitlines()]


class _Restored:
    """A mirror restored at startup on a transcript the old process was reading."""

    def __init__(self, tmp_path: Path, *before: dict) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        self.jsonl = project / "s.jsonl"
        with self.jsonl.open("a", encoding="utf-8") as f:
            for e in (clord_marker_event(), *before):
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.utime(self.jsonl, (1, 1))
        self.notices: list[str] = []

        def record() -> None:
            reply_tracker.record_reply_message(THREAD_ID, SimpleNamespace(id=30_000))

        async def sink(text: str) -> None:
            pass

        async def reply_sink(text: str, **kw) -> None:
            record()

        async def file_sink(text: str, _p: str, **kw) -> None:
            record()

        async def api_error_sink(text: str) -> None:
            self.notices.append(text)

        self.mirror = TranscriptMirror(
            thread_id=THREAD_ID,
            project_dir=project,
            sink=sink,
            reply_sink=reply_sink,
            file_sink=file_sink,
            verbosity="minimal",
            poll_interval=0.02,
            idle_flush_seconds=0,
            resumed=True,
            api_error_sink=api_error_sink,
        )

    async def __aenter__(self):
        self.mirror.start()
        await asyncio.sleep(0.15)
        return self

    async def __aexit__(self, *_):
        await self.mirror.stop()

    async def feed(self, *events: dict) -> None:
        with self.jsonl.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        await asyncio.sleep(0.3)
        await turn_events.drain()


async def test_restart_between_api_error_row_and_turn_end(tmp_path, log):
    """API エラーの行は再起動前、turn_duration は再起動後。"""
    before = (
        w._reply("2026-10-29T18:00:00.000Z"),
        w._end("2026-10-29T18:00:01.000Z"),
        *w._wakeup("2026-10-29T18:36:00.386Z"),
        a._api_error(),
    )
    async with _Restored(tmp_path, *before) as r:
        await r.feed(w._end("2026-10-29T18:47:21.000Z"))
    ev = _lines(log)
    assert [(e["kind"], e.get("reason")) for e in ev] == [("failure", "api_error")]
    assert len(r.notices) == 1


async def test_an_error_answered_past_before_the_restart_stays_a_completion(tmp_path, log):
    """Claude wrote again after the error, before the restart: not a failure."""
    before = (
        w._reply("2026-10-29T18:00:00.000Z"),
        w._end("2026-10-29T18:00:01.000Z"),
        *w._wakeup("2026-10-29T18:36:00.386Z"),
        a._api_error(),
        w._reply("2026-10-29T18:40:00.000Z"),
    )
    async with _Restored(tmp_path, *before) as r:
        await r.feed(w._end("2026-10-29T18:47:21.000Z"))
    ev = _lines(log)
    assert [e["kind"] for e in ev] == ["completion"]
    assert r.notices == []
