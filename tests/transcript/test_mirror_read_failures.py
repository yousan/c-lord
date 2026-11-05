"""#1102 / #1103: a failure on the reading side, or a full /tmp, must not silence a thread."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from unittest import mock

from c_lord.transcript import mirror as mirror_mod
from c_lord.transcript import tail as tail_mod
from c_lord.transcript.mirror import TranscriptMirror

from .helpers import clord_transcript


def _a(content: list) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _setup(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = clord_transcript(project / "s.jsonl")
    os.utime(jsonl, (1, 1))
    posted: list[str] = []
    files: list[str] = []

    async def sink(t: str) -> None:
        posted.append(t)

    async def fsink(body: str, path: str, **_: object) -> None:
        files.append(body)
        posted.append(body)

    def w(*rows: object) -> None:
        with jsonl.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")

    return project, posted, files, sink, fsink, w


async def test_one_failed_poll_does_not_silence_the_thread(tmp_path: Path, caplog) -> None:
    """#1102 (a): PermissionError / EIO in one poll ended the reading task for good."""
    project, posted, _, sink, _, w = _setup(tmp_path)
    m = TranscriptMirror(
        thread_id=8, project_dir=project, sink=sink, poll_interval=0.05, idle_flush_seconds=0.2
    )
    real = tail_mod._poll_once
    n = {"c": 0}

    def flaky(state):  # noqa: ANN001, ANN202
        n["c"] += 1
        if n["c"] == 3:
            raise PermissionError(13, "Permission denied")
        return real(state)

    caplog.set_level(logging.INFO)
    with mock.patch.object(tail_mod, "_poll_once", flaky):
        m.start()
        try:
            await asyncio.sleep(0.4)
            w({"type": "user", "message": {"role": "user", "content": "後の依頼"}})
            await asyncio.sleep(0.5)
        finally:
            await m.stop()
    assert any("後の依頼" in p for p in posted), posted
    # AC2: the failure is on record.
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Permission denied" in (r.exc_text or "") or r.exc_info for r in errors), [
        r.getMessage() for r in errors
    ]


async def test_non_object_json_line_does_not_silence_the_thread(tmp_path: Path) -> None:
    """#1102 (b): a valid-JSON line that is not an object (``null``) raised in the tail."""
    project, posted, _, sink, _, w = _setup(tmp_path)
    m = TranscriptMirror(thread_id=9, project_dir=project, sink=sink, poll_interval=0.05)
    m.start()
    try:
        await asyncio.sleep(0.15)
        w("null", "3", "[1, 2]")
        await asyncio.sleep(0.2)
        w({"type": "user", "message": {"role": "user", "content": "後の依頼"}})
        await asyncio.sleep(0.4)
    finally:
        await m.stop()
    assert any("後の依頼" in p for p in posted), posted


def test_seeding_skips_non_object_lines(tmp_path: Path) -> None:
    seen: set[str] = set()
    tail_mod._record_uuid(b"null", seen)
    tail_mod._record_uuid(b"[1]", seen)
    tail_mod._record_uuid(b'{"uuid": "u1"}', seen)
    assert seen == {"u1"}


async def test_idle_flush_failure_errors_are_rate_limited(tmp_path: Path, caplog) -> None:
    """#1103 AC1: while /tmp stays full, one held reply produced an ERROR per idle window."""
    project, posted, _, sink, fsink, w = _setup(tmp_path)
    m = TranscriptMirror(
        thread_id=10,
        project_dir=project,
        sink=sink,
        file_sink=fsink,
        poll_interval=0.02,
        idle_flush_seconds=0.05,
    )

    def ntf(*a: object, **k: object) -> None:
        raise OSError(28, "No space left on device")

    caplog.set_level(logging.INFO)
    with mock.patch.object(mirror_mod.tempfile, "NamedTemporaryFile", ntf):
        m.start()
        try:
            await asyncio.sleep(0.1)
            w(
                _a([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
                _a([{"type": "text", "text": "答え"}]),
            )
            await asyncio.sleep(1.0)
        finally:
            await m.stop()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) <= 2, [r.getMessage() for r in errors]


async def test_reply_goes_out_without_progress_when_tmp_is_full(tmp_path: Path, caplog) -> None:
    """#1103 AC2: the body is posted without the attachment rather than not at all."""
    project, posted, files, sink, fsink, w = _setup(tmp_path)
    m = TranscriptMirror(
        thread_id=11,
        project_dir=project,
        sink=sink,
        file_sink=fsink,
        poll_interval=0.02,
        idle_flush_seconds=0.05,
    )

    def ntf(*a: object, **k: object) -> None:
        raise OSError(28, "No space left on device")

    caplog.set_level(logging.INFO)
    with mock.patch.object(mirror_mod.tempfile, "NamedTemporaryFile", ntf):
        m.start()
        try:
            await asyncio.sleep(0.1)
            w(
                _a([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
                _a([{"type": "text", "text": "答え"}]),
            )
            await asyncio.sleep(0.6)
        finally:
            await m.stop()
    assert posted.count("答え") == 1, posted
    assert files == []
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "progress.txt" in errors[0].getMessage(), [
        r.getMessage() for r in errors
    ]


async def test_persistent_stat_permission_error_leaves_a_record(tmp_path: Path, caplog) -> None:
    """#1188 (#1102 AC2): the transcript's stat() keeps failing with EACCES from some
    point on. Nothing can reach the thread — and the log said nothing about why:
    the tail dropped every error but a vanished file without a word."""
    import inspect
    import pathlib

    project = tmp_path / "proj"
    project.mkdir()
    jsonl = clord_transcript(project / "s.jsonl")
    os.utime(jsonl, (1, 1))
    posted: list[str] = []

    async def sink(t: str) -> None:
        posted.append(t)

    real_stat = pathlib.Path.stat
    broken = {"on": False}

    def stat(self, *a, **k):
        if broken["on"] and self.name == "s.jsonl" and inspect.stack()[1].function == "_poll_once":
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *a, **k)

    caplog.set_level(logging.DEBUG)
    m = TranscriptMirror(
        thread_id=11, project_dir=project, sink=sink, poll_interval=0.02, idle_flush_seconds=0
    )
    with mock.patch.object(pathlib.Path, "stat", stat):
        m.start()
        try:
            await asyncio.sleep(0.15)
            broken["on"] = True
            with jsonl.open("a", encoding="utf-8") as f:
                row = {"type": "user", "message": {"role": "user", "content": "後の依頼"}}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            await asyncio.sleep(0.6)
        finally:
            await m.stop()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert posted == []  # nothing gets through while stat fails (expected)
    assert errors, "the thread is silent and nothing at ERROR says why"
    # Once an hour, not once per poll (#810).
    assert len(errors) == 1, [r.getMessage() for r in errors]


async def test_a_vanished_transcript_is_still_quiet(tmp_path: Path, caplog) -> None:
    """#1188 AC2: FileNotFoundError on stat is the ordinary "deleted / rotated"
    case — dropped without a word, as before."""
    import inspect
    import pathlib

    project = tmp_path / "proj"
    project.mkdir()
    jsonl = clord_transcript(project / "s.jsonl")
    os.utime(jsonl, (1, 1))

    async def sink(t: str) -> None:
        pass

    real_stat = pathlib.Path.stat
    broken = {"on": False}

    def stat(self, *a, **k):
        if broken["on"] and self.name == "s.jsonl" and inspect.stack()[1].function == "_poll_once":
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_stat(self, *a, **k)

    caplog.set_level(logging.DEBUG)
    m = TranscriptMirror(
        thread_id=12, project_dir=project, sink=sink, poll_interval=0.02, idle_flush_seconds=0
    )
    with mock.patch.object(pathlib.Path, "stat", stat):
        m.start()
        try:
            await asyncio.sleep(0.15)
            broken["on"] = True
            await asyncio.sleep(0.3)
        finally:
            await m.stop()
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_unreadable_error_and_poll_exception_each_get_their_own_line(
    tmp_path: Path, caplog
) -> None:
    """#1216: in one transcript directory, a poll that raised and a transcript that
    cannot be read (#1188) are two different reasons the thread hears nothing. They
    shared one hourly sampler keyed by the directory, so whichever came first kept
    the other out of the log for an hour — and the reader could not tell them apart
    (#678). Each must get its own line."""
    import threading

    project = tmp_path / "proj"
    project.mkdir()
    jsonl = clord_transcript(project / "s.jsonl")
    os.utime(jsonl, (1, 1))

    calls = {"n": 0}
    done = threading.Event()

    def fake_poll(state):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "Input/output error")
        state.read_error = PermissionError(13, "Permission denied")
        if calls["n"] >= 3:
            done.set()
        return []

    caplog.set_level(logging.DEBUG)

    async def consume() -> None:
        async for _ in tail_mod.tail_events(project, poll_interval=0.01):
            pass

    with mock.patch.object(tail_mod, "_poll_once", fake_poll):
        task = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(done.wait, 10.0), "the tail never polled 3 times"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("reading the transcript failed" in m for m in messages), messages
    assert any("cannot read the transcript" in m for m in messages), messages
    # Still once an hour each (#810): three polls, two lines.
    assert len(messages) == 2, messages
