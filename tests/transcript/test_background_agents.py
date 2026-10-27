"""The mirror counts background sub-agents so a turn-end event can wait (#917).

The shapes below are copied from real Claude Code transcripts: the launch is the
``tool_result`` of an ``Agent`` call with ``run_in_background``, the report is a
``queue-operation`` carrying a ``<task-notification>``, and the turn end is
``system/turn_duration`` — written *before* the report when the agent is still
running, after which Claude Code starts another turn by itself.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from c_lord import turn_events
from c_lord.transcript.mirror import (
    TranscriptMirror,
    _launched_agent_ids,
    _reported_task_id,
    has_pending_final,
)

from .helpers import clord_transcript

LAUNCH_TEXT = (
    "Async agent launched successfully. (This tool result is internal metadata)\n"
    "agentId: a6cf3f7a95d208c56 (internal ID - do not mention to user.)"
)


def _launch(agent_id: str = "a6cf3f7a95d208c56", *, as_list: bool = True) -> dict:
    text = LAUNCH_TEXT.replace("a6cf3f7a95d208c56", agent_id)
    content = [{"type": "text", "text": text}] if as_list else text
    return {
        "type": "user",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": content}],
        },
    }


def _report(agent_id: str = "a6cf3f7a95d208c56", status: str = "completed") -> dict:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "content": (
            f"<task-notification>\n<task-id>{agent_id}</task-id>\n"
            f"<status>{status}</status>\n</task-notification>"
        ),
    }


TURN_END = {"type": "system", "subtype": "turn_duration", "isSidechain": False}


class TestParsing:
    def test_launch_is_read_from_a_list_or_a_string_result(self):
        assert _launched_agent_ids(_launch("aaa111")) == ["aaa111"]
        assert _launched_agent_ids(_launch("bbb222", as_list=False)) == ["bbb222"]

    def test_an_ordinary_tool_result_launches_nothing(self):
        event = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "ok"}]},
        }
        assert _launched_agent_ids(event) == []

    def test_a_subagents_own_conversation_is_not_this_threads(self):
        event = {**_launch(), "isSidechain": True}
        assert _launched_agent_ids(event) == []

    @pytest.mark.parametrize("status", ["completed", "failed", "killed", "stopped"])
    def test_any_status_releases_the_agent(self, status):
        assert _reported_task_id(_report("abc123", status)) == "abc123"

    def test_only_the_enqueue_counts(self):
        assert _reported_task_id({**_report(), "operation": "remove"}) is None
        assert _reported_task_id({**_report(), "operation": "dequeue"}) is None

    def test_odd_shapes_never_raise(self):
        for event in ({}, {"type": "user", "message": "x"}, {"type": "queue-operation"}):
            assert _launched_agent_ids(event) == []
            assert _reported_task_id(event) is None


def _write(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


async def test_turn_end_reports_how_many_agents_are_still_running(tmp_path, monkeypatch):
    """launch → turn end (1 running) → report → turn end (0 running)."""
    monkeypatch.setenv(turn_events.ENV_LOG, str(tmp_path / "events.jsonl"))
    turn_events.reset()
    seen: list[int] = []
    monkeypatch.setattr(
        turn_events, "note_turn_end", lambda _tid, outstanding: seen.append(outstanding)
    )
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))

    async def sink(text: str) -> None:
        pass

    mirror = TranscriptMirror(thread_id=77, project_dir=project, sink=sink, poll_interval=0.05)
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        _write(jsonl, _launch())
        _write(jsonl, TURN_END)
        await asyncio.sleep(0.3)
        _write(jsonl, _report())
        _write(jsonl, TURN_END)
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    assert seen == [1, 0]


async def test_final_answer_counts_as_pending_until_it_is_posted(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))
    release = asyncio.Event()
    posted: list[str] = []

    async def sink(text: str) -> None:
        posted.append(text)

    async def reply_sink(text: str) -> None:
        await release.wait()  # the reply is still being sent
        posted.append(text)

    mirror = TranscriptMirror(
        thread_id=78,
        project_dir=project,
        sink=sink,
        reply_sink=reply_sink,
        poll_interval=0.05,
        idle_flush_seconds=0,
    )
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        assert not has_pending_final(78)
        _write(
            jsonl,
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            },
        )
        await asyncio.sleep(0.3)
        assert has_pending_final(78)  # read, not yet posted as the answer
        _write(jsonl, TURN_END)
        await asyncio.sleep(0.3)
        assert has_pending_final(78)  # being sent right now
        release.set()
        await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    assert not has_pending_final(78)
    assert posted == ["done"]
