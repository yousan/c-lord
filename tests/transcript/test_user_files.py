"""Tests for the ``SendUserFile`` → Discord attachment path (Issue #233).

The Claude Code harness tool ``SendUserFile`` returns ``1 file delivered to
user.`` and writes a ``tool_use`` into the transcript — but its delivery
channel is the harness's, not Discord's.  The mirror is the only thing that
writes to a thread (#71 single-writer), so it has to recognise that tool_use
and attach the files itself.  29 calls were dropped in silence before this.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from c_lord.transcript.mirror import TranscriptMirror, _user_file_requests

from .helpers import clord_transcript


def _write_event(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _send_user_file(
    files: list[str],
    *,
    caption: str | None = None,
    tool_use_id: str = "toolu_send_1",
    uuid: str = "u-send-1",
) -> dict:
    """A transcript event shaped exactly like a real ``SendUserFile`` call."""
    inp: dict = {"files": files, "status": "normal"}
    if caption is not None:
        inp["caption"] = caption
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": "SendUserFile", "input": inp}
            ],
        },
    }


def _assistant_text(text: str, uuid: str = "u-text") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


async def _drain(mirror: TranscriptMirror, jsonl: Path, events: list[dict]) -> None:
    """Start the mirror, append *events*, let the tail catch up, stop."""
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        for event in events:
            _write_event(jsonl, event)
        await asyncio.sleep(0.35)
    finally:
        await mirror.stop()


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    return project, jsonl


# --- the extractor -------------------------------------------------------


def test_user_file_requests_reads_files_and_caption() -> None:
    reqs = _user_file_requests(_send_user_file(["/a.png", "/b.png"], caption="2枚です"))
    assert len(reqs) == 1
    assert reqs[0].paths == ["/a.png", "/b.png"]
    assert reqs[0].caption == "2枚です"
    assert reqs[0].tool_use_id == "toolu_send_1"


def test_user_file_requests_without_caption() -> None:
    reqs = _user_file_requests(_send_user_file(["/a.png"]))
    assert reqs[0].caption is None


def test_user_file_requests_ignores_other_tools() -> None:
    event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
            ],
        },
    }
    assert _user_file_requests(event) == []


def test_user_file_requests_survives_malformed_input() -> None:
    """A shape we did not expect must yield nothing, never raise."""
    for content in (
        [{"type": "tool_use", "id": "t", "name": "SendUserFile", "input": {"files": "nope"}}],
        [{"type": "tool_use", "id": "t", "name": "SendUserFile", "input": {}}],
        [{"type": "tool_use", "id": "t", "name": "SendUserFile"}],
        [{"type": "tool_use", "id": "t", "name": "SendUserFile", "input": {"files": [1, 2]}}],
        "not-a-list",
    ):
        assert _user_file_requests({"type": "assistant", "message": {"content": content}}) == []


def test_user_file_requests_survives_events_without_a_message() -> None:
    """Runs on every event the tail yields — a shape it cannot read must not raise.

    An exception here kills the tail task, and with it the thread's whole mirror.
    """
    for event in (
        {"type": "summary", "summary": "…"},
        {"type": "assistant", "message": None},
        {"type": "system", "message": "a string"},
        {},
    ):
        assert _user_file_requests(event) == []


def test_user_file_requests_drops_blank_paths() -> None:
    reqs = _user_file_requests(_send_user_file(["", "  ", "/real.png"]))
    assert reqs[0].paths == ["/real.png"]


# --- the mirror ----------------------------------------------------------


async def test_send_user_file_reaches_the_sink(tmp_path: Path) -> None:
    project, jsonl = _project(tmp_path)
    delivered: list = []

    async def user_file_sink(request) -> None:
        delivered.append(request)

    mirror = TranscriptMirror(
        thread_id=1,
        project_dir=project,
        sink=_noop_sink(),
        user_file_sink=user_file_sink,
        poll_interval=0.05,
    )
    await _drain(mirror, jsonl, [_send_user_file(["/tmp/shot.png"], caption="スクショです")])

    assert len(delivered) == 1
    assert delivered[0].paths == ["/tmp/shot.png"]
    assert delivered[0].caption == "スクショです"


async def test_send_user_file_is_not_delivered_twice(tmp_path: Path) -> None:
    """The same ``tool_use`` id read again must not post the files a second time."""
    project, jsonl = _project(tmp_path)
    delivered: list = []

    async def user_file_sink(request) -> None:
        delivered.append(request)

    mirror = TranscriptMirror(
        thread_id=2,
        project_dir=project,
        sink=_noop_sink(),
        user_file_sink=user_file_sink,
        poll_interval=0.05,
    )
    await _drain(
        mirror,
        jsonl,
        [
            _send_user_file(["/tmp/a.png"], tool_use_id="toolu_same", uuid="u1"),
            _send_user_file(["/tmp/a.png"], tool_use_id="toolu_same", uuid="u2"),
        ],
    )

    assert len(delivered) == 1


async def test_other_tools_do_not_reach_the_user_file_sink(tmp_path: Path) -> None:
    project, jsonl = _project(tmp_path)
    delivered: list = []

    async def user_file_sink(request) -> None:
        delivered.append(request)

    mirror = TranscriptMirror(
        thread_id=3,
        project_dir=project,
        sink=_noop_sink(),
        user_file_sink=user_file_sink,
        poll_interval=0.05,
    )
    await _drain(
        mirror,
        jsonl,
        [
            {
                "type": "assistant",
                "uuid": "u-bash",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
                    ],
                },
            }
        ],
    )

    assert delivered == []


async def test_pending_text_is_posted_before_the_files(tmp_path: Path) -> None:
    """Prose written before the tool call must reach the thread first.

    Claude narrates ("下に貼ります") and *then* calls SendUserFile; the files
    landing above that sentence would read backwards.
    """
    project, jsonl = _project(tmp_path)
    order: list[str] = []

    async def sink(text: str) -> None:
        order.append(f"text:{text}")

    async def user_file_sink(request) -> None:
        order.append(f"files:{request.paths[0]}")

    mirror = TranscriptMirror(
        thread_id=4,
        project_dir=project,
        sink=sink,
        user_file_sink=user_file_sink,
        poll_interval=0.05,
    )
    await _drain(
        mirror,
        jsonl,
        [
            _assistant_text("これから貼ります", uuid="u-a"),
            _send_user_file(["/tmp/shot.png"], uuid="u-b"),
        ],
    )

    assert order == ["text:これから貼ります", "files:/tmp/shot.png"]


async def test_mirror_without_user_file_sink_still_runs(tmp_path: Path) -> None:
    """Backward compatibility: the sink is optional (Zero-Config default)."""
    project, jsonl = _project(tmp_path)
    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    mirror = TranscriptMirror(
        thread_id=5, project_dir=project, sink=sink, verbosity="full", poll_interval=0.05
    )
    await _drain(mirror, jsonl, [_send_user_file(["/tmp/shot.png"])])

    assert any("SendUserFile" in p for p in posted)


def _noop_sink():
    async def sink(text: str) -> None:
        return None

    return sink
