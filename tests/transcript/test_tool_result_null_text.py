"""Review #1055: the Issue's own row is still *skipped*, not handled."""

from __future__ import annotations

import asyncio, json, logging, os
from pathlib import Path

from c_lord.discord_ui.turn_progress import TurnProgress
from c_lord.transcript.mirror import TranscriptMirror

from .helpers import clord_transcript


def _tool_use() -> dict:
    return {
        "type": "assistant",
        "uuid": "a1",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
            ],
        },
    }


def _null_result() -> dict:
    return {
        "type": "user",
        "uuid": "u1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [{"type": "text", "text": None}],
                }
            ],
        },
    }


async def _run(tmp_path: Path, rows: list[dict]):
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = clord_transcript(project / "s.jsonl")
    os.utime(jsonl, (1, 1))
    posted: list[str] = []
    files: list[tuple[str, str]] = []

    async def sink(t: str) -> None:
        posted.append(t)

    async def fsink(body: str, path: str, **_: object) -> None:
        files.append((body, Path(path).read_text()))

    async def noop(*a: object) -> None:
        return None

    progress = TurnProgress(post=noop, edit=noop, delete=noop)
    m = TranscriptMirror(
        thread_id=1055,
        project_dir=project,
        sink=sink,
        file_sink=fsink,
        progress=progress,
        poll_interval=0.05,
    )
    m.start()
    try:
        await asyncio.sleep(0.15)
        with jsonl.open("a", encoding="utf-8") as f:
            for e in rows:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        await asyncio.sleep(0.4)
        running = dict(progress._running)
    finally:
        await m.stop()
    return posted, files, running


async def test_issue_row_is_handled_not_skipped(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="c_lord.transcript.mirror"):
        _, _, running = await _run(tmp_path, [_tool_use(), _null_result()])
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    print("RUNNING", running)
    assert errors == [], errors  # PR: "could not handle a row — skipped it ... type=user"
    assert running == {}, running  # PR: {'t1': 'Bash: ls'} — the call stays "running"
