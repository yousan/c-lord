"""#1110 / #1136 / #1139 / #1177: one odd row, a self-woken finished agent, and stable numbers."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from c_lord.transcript.subagents import SubagentLog

from .test_subagent_log import SESSION, Clock, FakePoster, _append, _project

AID = "a0123456789abcdef"


def _row(uid: str, ts: str, role: str, content: object, **extra: object) -> dict:
    return {
        "type": role,
        "uuid": uid,
        "timestamp": ts,
        "isSidechain": True,
        "message": {"role": role, "content": content, **extra.pop("message", {})},
        **extra,
    }


def _notice(agent_id: str, ts: str) -> dict:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": ts,
        "content": (
            f"<task-notification>\n<task-id>{agent_id}</task-id>\n"
            "<status>completed</status>\n</task-notification>"
        ),
    }


def _end_turn(uid: str, ts: str, text: str) -> dict:
    return _row(
        uid, ts, "assistant", [{"type": "text", "text": text}], message={"stop_reason": "end_turn"}
    )


def _log(project: Path, poster: FakePoster, *, voices: bool = False) -> SubagentLog:
    return SubagentLog(
        thread_id=1, project_dir=project, poster=poster, clock=Clock(), voices=voices
    )


# --- #1110: one row of an unexpected shape --------------------------------

_ODD_RESULTS: dict[str, object] = {
    "number": 5,
    "dict": {"a": 1},
    "list_of_numbers": [1, 2],
    "list_of_none": [None],
    "text_number": [{"type": "text", "text": 7}],
}


@pytest.mark.parametrize(
    "name", [*_ODD_RESULTS, "assistant_text_int", "assistant_text_none", "content_number"]
)
async def test_an_odd_row_does_not_drop_the_rows_after_it(
    tmp_path: Path, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    project, subdir = _project(tmp_path, SESSION)
    log = _log(project, FakePoster())
    await log.prime()
    path = subdir / f"agent-{AID}.jsonl"
    _append(path, [_row("u0", "2026-10-29T00:00:00Z", "user", "依頼")])
    await log.step()
    if name.startswith("assistant_text"):
        text = 5 if name.endswith("int") else None
        odd = _row("a1", "2026-10-29T00:00:01Z", "assistant", [{"type": "text", "text": text}])
    elif name == "content_number":
        odd = _row("a1", "2026-10-29T00:00:01Z", "assistant", 12)
    else:
        odd = _row(
            "u1",
            "2026-10-29T00:00:02Z",
            "user",
            [{"type": "tool_result", "tool_use_id": "t1", "content": _ODD_RESULTS[name]}],
        )
    _append(
        path,
        [
            _row(
                "a0",
                "2026-10-29T00:00:01Z",
                "assistant",
                [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
            ),
            odd,
            _row(
                "a2",
                "2026-10-29T00:00:03Z",
                "assistant",
                [{"type": "text", "text": "最後の報告です"}],
            ),
        ],
    )
    caplog.set_level(logging.INFO)
    await log.step()
    agent = log._agents[AID]
    assert agent.run is not None and agent.run.report == "最後の報告です"
    assert any("最後の報告です" in x for x in agent.lines), list(agent.lines)


async def test_a_row_that_raises_is_logged_as_an_error_and_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1110: the guard itself — whatever a row raises, the next row is read."""
    from c_lord.transcript import subagents as mod

    project, subdir = _project(tmp_path, SESSION)
    log = _log(project, FakePoster())
    await log.prime()
    real = mod.render_event

    def boom(event: dict):  # noqa: ANN202
        if event.get("uuid") == "a1":
            raise TypeError("odd row")
        return real(event)

    monkeypatch.setattr(mod, "render_event", boom)
    _append(
        subdir / f"agent-{AID}.jsonl",
        [
            _row("u0", "2026-10-29T00:00:00Z", "user", "依頼"),
            _row("a1", "2026-10-29T00:00:01Z", "assistant", [{"type": "text", "text": "x"}]),
            _row("a2", "2026-10-29T00:00:02Z", "assistant", [{"type": "text", "text": "報告"}]),
        ],
    )
    caplog.set_level(logging.INFO)
    await log.step()
    assert log._agents[AID].run.report == "報告"  # type: ignore[union-attr]
    assert any(r.levelno >= logging.ERROR and r.exc_info for r in caplog.records)


# --- #1136: woken by its own background task's notice ---------------------


async def _finished_agent(tmp_path: Path, *, voices: bool = True):
    project, subdir = _project(tmp_path, SESSION)
    poster = FakePoster()
    log = _log(project, poster, voices=voices)
    await log.prime()
    path = subdir / f"agent-{AID}.jsonl"
    _append(
        path,
        [
            _row("u0", "2026-10-29T00:00:00Z", "user", "新潟県の講座を探す"),
            _row(
                "a0",
                "2026-10-29T00:00:01Z",
                "assistant",
                [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
            ),
            _end_turn("a1", "2026-10-29T00:00:05Z", "見つかったのは3件です"),
        ],
    )
    await log.step()
    log.observe_parent(_notice(AID, "2026-10-29T00:00:06Z"))
    await log.step()
    return log, poster, path


_SYSTEM_NOTICE = (
    "[SYSTEM NOTIFICATION - NOT USER INPUT] This is an automated background-task event, "
    "not a message from the user."
)


async def test_a_finished_agent_woken_by_its_own_background_task_posts_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#1136 AC1. RED: 再開, 終了 · 0:00 · ツール 0 件 and a second 📝 最終報告."""
    log, poster, path = await _finished_agent(tmp_path)
    before = (len(poster.statuses), len(poster.edits), len(poster.voices))
    assert [p.text for p in poster.statuses][-1].startswith("-# 🤖 サブエージェント")
    caplog.set_level(logging.INFO)
    _append(
        path,
        [
            _row("u1", "2026-10-29T06:00:00Z", "user", _SYSTEM_NOTICE, isMeta=True),
            _end_turn("a2", "2026-10-29T06:00:05Z", "Nothing changes from this."),
        ],
    )
    await log.step()
    log.observe_parent(_notice(AID, "2026-10-29T06:00:06Z"))
    await log.step()
    assert (len(poster.statuses), len(poster.edits), len(poster.voices)) == before, (
        poster.everything()
    )
    assert "Nothing changes" not in poster.everything()
    assert any("background-task notice" in r.getMessage() for r in caplog.records)
    assert log._agents[AID].run is None


async def test_a_self_woken_run_that_uses_tools_is_shown_once(tmp_path: Path) -> None:
    """#1136 AC2: woken by its own notice and calling a tool is work — 再開 / 終了 as before."""
    log, poster, path = await _finished_agent(tmp_path)
    before = len(poster.statuses)
    _append(
        path,
        [
            _row("u1", "2026-10-29T06:00:00Z", "user", _SYSTEM_NOTICE, isMeta=True),
            _row(
                "a2",
                "2026-10-29T06:00:01Z",
                "assistant",
                [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}}],
            ),
            _end_turn("a3", "2026-10-29T06:00:05Z", "追加で1件見つけました"),
        ],
    )
    await log.step()
    log.observe_parent(_notice(AID, "2026-10-29T06:00:06Z"))
    await log.step()
    new = [p.text for p in poster.statuses[before:]]
    assert len(new) == 2, new
    assert new[0].endswith("再開")
    assert "終了" in new[1] and "ツール 1 件" in new[1]
    assert any("追加で1件見つけました" in t for _, t in poster.voices)


# --- #1139: one number per agent, kept across restarts --------------------

A_LATE = "a0000000000000001"  # sorts first, appears second
B_EARLY = "b0000000000000002"  # sorts second, appears first


def _two_agents(subdir: Path) -> None:
    _append(
        subdir / f"agent-{B_EARLY}.jsonl",
        [_row("b0", "2026-10-29T00:00:00Z", "user", "先に起動")],
    )
    _append(
        subdir / f"agent-{A_LATE}.jsonl",
        [_row("a0", "2026-10-29T00:00:10Z", "user", "後に起動")],
    )


async def test_numbers_follow_the_order_the_agents_appeared(tmp_path: Path) -> None:
    """#1139 AC3: ``agent-b…`` came first, so it is 1 — not the id order."""
    project, subdir = _project(tmp_path, SESSION)
    log = _log(project, FakePoster())
    await log.prime()
    _two_agents(subdir)
    await log.step()
    assert log._agents[B_EARLY].ordinal == 1
    assert log._agents[A_LATE].ordinal == 2


async def test_a_restart_keeps_every_agents_number(tmp_path: Path) -> None:
    """#1139 AC1 / AC2. RED: the next process renumbered by agent id."""
    project, subdir = _project(tmp_path, SESSION)
    first = _log(project, FakePoster())
    await first.prime()
    _append(subdir / f"agent-{B_EARLY}.jsonl", [_row("b0", "2026-10-29T00:00:00Z", "user", "先")])
    await first.step()
    _append(subdir / f"agent-{A_LATE}.jsonl", [_row("a0", "2026-10-29T00:00:10Z", "user", "後")])
    await first.step()
    before = {a: first._agents[a].ordinal for a in (A_LATE, B_EARLY)}
    await first.stop()

    second = _log(project, FakePoster())  # a restart
    await second.prime()
    third_id = "c0000000000000003"
    _append(subdir / f"agent-{third_id}.jsonl", [_row("c0", "2026-10-29T01:00:00Z", "user", "次")])
    await second.step()
    after = {a: second._agents[a].ordinal for a in (A_LATE, B_EARLY)}
    assert after == before == {B_EARLY: 1, A_LATE: 2}
    assert second._agents[third_id].ordinal == 3
    ordinals = [a.ordinal for a in second._agents.values()]
    assert len(set(ordinals)) == len(ordinals)


async def test_agents_from_before_the_numbers_were_kept_get_them_in_order(tmp_path: Path) -> None:
    """After the upgrade, a thread's existing agents are numbered by when they appeared."""
    project, subdir = _project(tmp_path, SESSION)
    _two_agents(subdir)  # already on disk: baselined
    log = _log(project, FakePoster())
    await log.prime()
    await log.step()
    assert log._agents[B_EARLY].ordinal == 1
    assert log._agents[A_LATE].ordinal == 2


# --- #1177: the coordinator speaking to a finished agent is a real 再開 -------
#
# #1158 silenced every ``isMeta`` row that was not ``peer`` as the agent's own
# background-task notice. CLI 2.1.305 / 2.1.312 write the parent's message to a
# finished agent as ``isMeta`` with ``origin.kind == "coordinator"``, so when the
# parent asked a finished agent something, the thread showed nothing of it.


def _coordinator_row(uid: str, ts: str, text: str) -> dict:
    return _row(
        uid,
        ts,
        "user",
        f"The coordinator sent a message while you were working:\n{text}",
        isMeta=True,
        origin={"kind": "coordinator"},
    )


async def test_parent_message_to_finished_agent_coordinator_origin_is_a_resume(
    tmp_path: Path,
) -> None:
    """The agent answers in text (no tool). #1136 AC3: a resume shows 再開 / 終了."""
    log, poster, path = await _finished_agent(tmp_path)
    before = len(poster.statuses)
    _append(
        path,
        [
            _coordinator_row("u1", "2026-10-29T06:00:00Z", "見つけた3件の URL を教えて"),
            _end_turn("a2", "2026-10-29T06:00:05Z", "URL は A, B, C です"),
        ],
    )
    await log.step()
    log.observe_parent(_notice(AID, "2026-10-29T06:00:06Z"))
    await log.step()
    new = [p.text for p in poster.statuses[before:]]
    assert len(new) == 2 and new[0].endswith("再開"), new
    assert any("URL は A, B, C です" in t for _, t in poster.voices)


async def test_resume_with_coordinator_origin_is_shown_before_its_first_tool(
    tmp_path: Path,
) -> None:
    """Same row, the agent then works: 再開 is posted at the resume, not at the 1st tool."""
    log, poster, path = await _finished_agent(tmp_path)
    before = len(poster.statuses)
    _append(
        path,
        [
            _coordinator_row("u1", "2026-10-29T06:00:00Z", "第2弾"),
            _row(
                "a2",
                "2026-10-29T06:00:01Z",
                "assistant",
                [{"type": "thinking", "thinking": "考え中"}],
            ),
        ],
    )
    await log.step()
    assert len(poster.statuses) == before + 1, [p.text for p in poster.statuses[before:]]


async def test_own_notice_with_its_real_origin_is_still_quiet(tmp_path: Path) -> None:
    """#1136 kept with what the CLI really writes: ``origin.kind == "task-notification"``."""
    log, poster, path = await _finished_agent(tmp_path)
    before = (len(poster.statuses), len(poster.edits), len(poster.voices))
    _append(
        path,
        [
            _row(
                "u1",
                "2026-10-29T06:00:00Z",
                "user",
                _SYSTEM_NOTICE,
                isMeta=True,
                origin={"kind": "task-notification"},
            ),
            _end_turn("a2", "2026-10-29T06:00:05Z", "Nothing changes from this."),
        ],
    )
    await log.step()
    log.observe_parent(_notice(AID, "2026-10-29T06:00:06Z"))
    await log.step()
    assert (len(poster.statuses), len(poster.edits), len(poster.voices)) == before


async def test_the_parents_question_is_in_the_md_1203(tmp_path: Path) -> None:
    """#1203: the coordinator's message that resumed a finished child is in its md."""
    from .test_subagent_log import _append

    log, poster, path = await _finished_agent(tmp_path)
    _append(
        path,
        [
            _row(
                "u1",
                "2026-10-29T06:00:00Z",
                "user",
                "The coordinator sent a message:\nQUESTION-XYZ",
                isMeta=True,
                origin={"kind": "coordinator"},
            ),
            _end_turn("a2", "2026-10-29T06:00:05Z", "ANSWER-XYZ"),
        ],
    )
    await log.step()
    log.observe_parent(_notice(AID, "2026-10-29T06:00:06Z"))
    await log.step()
    files = "\n".join((p.file or "") for p in poster.statuses + [e for _, e in poster.edits])
    assert "ANSWER-XYZ" in files
    assert "📨 ← 親: QUESTION-XYZ" in files
