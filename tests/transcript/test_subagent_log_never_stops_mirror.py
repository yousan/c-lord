"""#1021: the sub-agent log (#971) must never stop the thread's transcript mirror.

The mirror is the thread's only delivery path, and ``observe_parent`` runs inside
its loop. An exception there used to end ``_run`` — every later reply of the
thread went nowhere until the bot restarted.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from c_lord.transcript.mirror import TranscriptMirror
from c_lord.transcript.subagents import sent_messages

from .helpers import clord_transcript


def _user(content: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}}


def test_a_send_message_whose_input_is_not_an_object_is_ignored() -> None:
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "SendMessage", "input": "oops"}]},
    }
    assert sent_messages(event) == []


async def test_a_failing_subagent_log_does_not_stop_the_mirror(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = clord_transcript(project / "s.jsonl")
    os.utime(jsonl, (1, 1))
    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    log = MagicMock()
    log.stop = AsyncMock()
    log.observe_parent.side_effect = [AttributeError("boom"), None, None]
    mirror = TranscriptMirror(
        thread_id=1021, project_dir=project, sink=sink, poll_interval=0.05, subagent_log=log
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        with jsonl.open("a", encoding="utf-8") as f:
            for e in (_user("一つ目"), _user("二つ目")):
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()
    assert any("二つ目" in p for p in posted), posted
