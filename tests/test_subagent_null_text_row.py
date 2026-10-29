"""Review3 #1072: a `text: null` tool_result in a subagent transcript fails the whole step."""

from __future__ import annotations

from pathlib import Path

from c_lord.transcript.subagents import SubagentLog

from .test_subagent_log import SESSION, Clock, FakePoster, _append, _project

AID = "a0123456789abcdef"


def _row(uid: str, ts: str, role: str, content: list) -> dict:
    return {
        "type": role,
        "uuid": uid,
        "timestamp": ts,
        "agentId": AID,
        "isSidechain": True,
        "message": {"role": role, "content": content},
    }


async def test_null_text_row_in_a_subagent_does_not_drop_later_rows(tmp_path: Path) -> None:
    project, subdir = _project(tmp_path, SESSION)
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, clock=Clock())
    await log.prime()
    path = subdir / f"agent-{AID}.jsonl"
    _append(
        path, [_row("u0", "2026-10-29T00:00:00Z", "user", [{"type": "text", "text": "依頼の本文"}])]
    )
    await log.step()
    _append(
        path,
        [
            _row(
                "a1",
                "2026-10-29T00:00:01Z",
                "assistant",
                [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
            ),
            _row(
                "u1",
                "2026-10-29T00:00:02Z",
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": None}],
                    }
                ],
            ),
            _row(
                "a2",
                "2026-10-29T00:00:03Z",
                "assistant",
                [{"type": "text", "text": "最後の報告です"}],
            ),
        ],
    )
    try:
        await log.step()
        raised = None
    except Exception as e:  # noqa: BLE001
        raised = e
    await log.step()
    assert raised is None, repr(raised)
    agent = log._agents[AID]
    got = list(agent.lines._head) + list(agent.lines._tail)
    assert any("最後の報告です" in x for x in got), got
