"""Tests for the sub-agent log (#971): what a thread's sub-agents did, on Discord.

The fixtures under ``tests/fixtures/subagents/`` are real transcripts (CLI
2.1.312), trimmed to their ``user`` / ``assistant`` / ``queue-operation`` rows:

- ``plain/``: two ordinary sub-agents of a c-lord thread (2026-10-29). ``asker-b``
  asks ``calc-a`` for 2+2 with ``SendMessage``; ``calc-a`` is resumed by that
  message and answers — so it is notified twice.
- ``teams/``: an Agent Teams in-process team (alice, bob) reporting to the lead.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from c_lord.transcript.subagents import (
    SubagentLog,
    sent_messages,
    task_notifications,
    teammate_idles,
)

FIXTURES = Path(__file__).parent / "fixtures" / "subagents"
CALC_A = "a201a36f65822ff1e"
ASKER_B = "afb460848e6688725"
SESSION = "8c72e7d9-97b9-4d63-9918-a02c95e39650"
TEAM_SESSION = "49db0b8c-f7cc-4d60-b86e-b42fd63141d6"


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@dataclass
class _Posted:
    text: str
    file: str | None = None


@dataclass
class FakePoster:
    statuses: list[_Posted] = field(default_factory=list)
    edits: list[tuple[int, _Posted]] = field(default_factory=list)
    voices: list[tuple[str, str]] = field(default_factory=list)

    async def post_status(self, text: str, file):
        self.statuses.append(_Posted(text, _read(file)))
        return len(self.statuses) - 1

    async def edit_status(self, handle, text: str, file) -> None:
        self.edits.append((handle, _Posted(text, _read(file))))

    async def post_voice(self, speaker: str, text: str) -> None:
        self.voices.append((speaker, text))

    def everything(self) -> str:
        parts = [p.text + (p.file or "") for p in self.statuses]
        parts += [p.text + (p.file or "") for _, p in self.edits]
        parts += [f"{s}{t}" for s, t in self.voices]
        return "\n".join(parts)


def _read(file) -> str | None:
    if file is None or not file.path:  # KEEP_ATTACHMENT (#1078)
        return None
    return Path(file.path).read_text(encoding="utf-8")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _project(tmp_path: Path, session: str) -> tuple[Path, Path]:
    project = tmp_path / "project"
    subdir = project / session / "subagents"
    subdir.mkdir(parents=True)
    (project / ".clord-session").write_text(session + "\n", encoding="utf-8")
    return project, subdir


def _copy_meta(src: Path, dst: Path) -> None:
    """The metas as they are while the agents still run (the fixtures are final)."""
    for meta in src.glob("*.meta.json"):
        raw = json.loads(meta.read_text(encoding="utf-8"))
        raw.pop("finished", None)
        (dst / meta.name).write_text(json.dumps(raw), encoding="utf-8")


def _append(path: Path, events: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _split_after_text(events: list[dict], text: str) -> tuple[list[dict], list[dict]]:
    for i, e in enumerate(events):
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list) and any(
            b.get("type") == "text" and b.get("text", "").startswith(text) for b in content
        ):
            return events[: i + 1], events[i + 1 :]
    raise AssertionError(text)


def _parent_rows(name: str) -> list[dict]:
    return _events(FIXTURES / name / "parent.jsonl")


def _enqueues(rows: list[dict], agent: str) -> list[dict]:
    return [
        r
        for r in rows
        if r.get("type") == "queue-operation"
        and r.get("operation") == "enqueue"
        and f"<task-id>{agent}</task-id>" in r.get("content", "")
    ]


# --- parsing ---------------------------------------------------------------


def test_task_notifications_reads_enqueues_only() -> None:
    rows = _parent_rows("plain")
    found = [n for r in rows for n in task_notifications(r)]
    # calc-a twice (resumed by asker-b), asker-b once; the ``remove`` rows and
    # the absorbed ``queued_command`` attachments are the same notices again.
    assert found == [(CALC_A, "completed"), (CALC_A, "completed"), (ASKER_B, "completed")]


def test_sent_messages_reads_sendmessage_tool_use() -> None:
    rows = _events(FIXTURES / "plain" / "subagents" / f"agent-{ASKER_B}.jsonl")
    sent = [m for r in rows for m in sent_messages(r)]
    assert sent == [(CALC_A, "asker-b here: please compute 2+2 and send the answer back to me.")]


def test_teammate_idles_reads_idle_notifications() -> None:
    rows = _parent_rows("teams")
    assert [n for r in rows for n in teammate_idles(r)] == ["bob", "alice", "alice"]


# --- the log, driven step by step -----------------------------------------


async def _plain_run(tmp_path: Path, *, voices: bool, progress_seconds: float = 60.0):
    """Replay the plain fixture in the order Claude Code wrote it."""
    project, subdir = _project(tmp_path, SESSION)
    poster = FakePoster()
    clock = Clock()
    log = SubagentLog(
        thread_id=1,
        project_dir=project,
        poster=poster,
        voices=voices,
        progress_seconds=progress_seconds,
        clock=clock,
    )
    await log.prime()
    src = FIXTURES / "plain" / "subagents"
    a_rows = _events(src / f"agent-{CALC_A}.jsonl")
    b_rows = _events(src / f"agent-{ASKER_B}.jsonl")
    a1, a2 = _split_after_text(a_rows, "calc-a ready")
    b1, b2 = _split_after_text(b_rows, "asker-b waiting")
    parent = _parent_rows("plain")
    a_done = _enqueues(parent, CALC_A)
    b_done = _enqueues(parent, ASKER_B)
    _copy_meta(src, subdir)

    _append(subdir / f"agent-{CALC_A}.jsonl", a1)
    await log.step()
    log.observe_parent(a_done[0])
    await log.step()
    _append(subdir / f"agent-{ASKER_B}.jsonl", b1)
    _append(subdir / f"agent-{CALC_A}.jsonl", a2)
    await log.step()
    clock.now += 61
    await log.step()
    log.observe_parent(a_done[1])
    _append(subdir / f"agent-{ASKER_B}.jsonl", b2)
    await log.step()
    log.observe_parent(b_done[0])
    await log.step()
    return log, poster


async def test_one_start_and_one_end_per_run(tmp_path: Path) -> None:
    _, poster = await _plain_run(tmp_path, voices=False)
    texts = [p.text for p in poster.statuses]
    assert texts[0] == "-# 🤖 サブエージェント「probe child A (calc)」開始"
    starts = [t for t in texts if "開始" in t or "再開" in t]
    ends = [t for t in texts if "終了" in t]
    # calc-a ran twice (its own turn, then the reply to asker-b); asker-b once.
    assert sum("probe child A" in t and "開始" in t for t in starts) == 1
    assert sum("probe child A" in t and "再開" in t for t in starts) == 1
    assert sum("probe child B" in t for t in starts) == 1
    assert sum("probe child A" in t for t in ends) == 2
    assert sum("probe child B" in t for t in ends) == 1


async def test_end_line_carries_report_and_tool_calls(tmp_path: Path) -> None:
    _, poster = await _plain_run(tmp_path, voices=False)
    end_b = next(p for p in poster.statuses if "終了" in p.text and "probe child B" in p.text)
    assert "ツール 2 件" in end_b.text
    assert end_b.file is not None
    assert "## 最終報告" in end_b.file
    assert "asker-b got 4" in end_b.file or "has finished" in end_b.file
    assert "🔧 SendMessage" in end_b.file
    assert "## 依頼" in end_b.file and 'You are "asker-b"' in end_b.file


async def test_no_internal_ids_or_paths(tmp_path: Path) -> None:
    _, poster = await _plain_run(tmp_path, voices=True)
    everything = poster.everything()
    assert CALC_A not in everything
    assert CALC_A[:7] not in everything
    assert ASKER_B not in everything
    assert "/tmp/claude-" not in everything
    assert "internal metadata" not in everything


async def test_progress_file_is_replaced_while_running(tmp_path: Path) -> None:
    _, poster = await _plain_run(tmp_path, voices=False, progress_seconds=60.0)
    running = [p for _, p in poster.edits if "作業中" in p.text]
    assert running, "the start line should be edited with the live progress file"
    assert all(p.file is not None for p in running)


async def test_voices_off_posts_no_named_messages(tmp_path: Path) -> None:
    _, poster = await _plain_run(tmp_path, voices=False)
    assert poster.voices == []


async def test_voices_show_who_said_what_to_whom(tmp_path: Path) -> None:
    _, poster = await _plain_run(tmp_path, voices=True)
    voices = poster.voices
    # The parent's assignments, under the parent's name.
    assert any(s == "親" and "→ **probe child A (calc)**" in t for s, t in voices)
    # Child to child, both directions, under the sender's name.
    asks = [
        t for s, t in voices if s == "probe child B (asks A)" and "→ **probe child A (calc)**" in t
    ]
    assert len(asks) == 1 and "please compute 2+2" in asks[0]
    answers = [
        t for s, t in voices if s == "probe child A (calc)" and "→ **probe child B (asks A)**" in t
    ]
    assert len(answers) == 1 and "2+2 = 4" in answers[0]
    # Final reports, under each child's name.
    reports = [s for s, t in voices if t.startswith("📝 **最終報告**")]
    assert reports.count("probe child B (asks A)") == 1
    assert reports.count("probe child A (calc)") == 2
    # Tool calls never become messages.
    assert not any("🔧" in t for _, t in voices)


async def test_restart_does_not_repost_finished_agents(tmp_path: Path) -> None:
    project, subdir = _project(tmp_path, SESSION)
    src = FIXTURES / "plain" / "subagents"
    for f in src.iterdir():
        shutil.copy(f, subdir / f.name)
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, voices=True)
    await log.prime()
    for row in _parent_rows("plain"):
        log.observe_parent(row)
    await log.step()
    await log.step()
    assert poster.statuses == [] and poster.voices == []


async def test_teammates_use_their_names_and_end_on_idle(tmp_path: Path) -> None:
    project, subdir = _project(tmp_path, TEAM_SESSION)
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, voices=True)
    await log.prime()
    src = FIXTURES / "teams" / "subagents"
    _copy_meta(src, subdir)
    bob = _events(src / "agent-abob-986a1b2d93bf8eee.jsonl")
    _append(subdir / "agent-abob-986a1b2d93bf8eee.jsonl", bob)
    await log.step()
    for row in _parent_rows("teams"):
        if "bob" in json.dumps(row):
            log.observe_parent(row)
    await log.step()
    texts = [p.text for p in poster.statuses]
    assert texts[0] == "-# 🤖 チームメイト「bob」開始"
    assert any("「bob」終了" in t for t in texts)
    assert ("bob", "→ **親**\nbob result: 2+2 = 4") in poster.voices


async def test_parent_message_to_a_child_is_voiced(tmp_path: Path) -> None:
    project, _ = _project(tmp_path, TEAM_SESSION)
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, voices=True)
    await log.prime()
    _copy_meta(FIXTURES / "teams" / "subagents", project / TEAM_SESSION / "subagents")
    for row in _parent_rows("teams"):
        log.observe_parent(row)
    await log.step()
    to_alice = [t for s, t in poster.voices if s == "親" and t.startswith("→ **alice**")]
    assert any("3+3" in t for t in to_alice)


@pytest.mark.parametrize("value", ["0", "false", "off"])
def test_log_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from c_lord.transcript.subagents import subagent_log_enabled, subagent_voices_enabled

    monkeypatch.setenv("CLORD_SUBAGENT_LOG", value)
    assert subagent_log_enabled() is False
    monkeypatch.delenv("CLORD_SUBAGENT_LOG")
    assert subagent_log_enabled() is True
    assert subagent_voices_enabled() is False
    monkeypatch.setenv("CLORD_SUBAGENT_VOICES", "1")
    assert subagent_voices_enabled() is True


# --- #970: the launch receipt is Claude's bookkeeping -----------------------


def _launch_results(name: str) -> list[dict]:
    return [
        r
        for r in _parent_rows(name)
        if r.get("type") == "user"
        and isinstance((r.get("message") or {}).get("content"), list)
        and any(b.get("type") == "tool_result" for b in r["message"]["content"])
    ]


@pytest.mark.parametrize("name", ["plain", "teams"])
def test_launch_receipt_hides_ids_paths_and_instructions(name: str) -> None:
    from c_lord.transcript.formatter import render_event

    rows = _launch_results(name)
    receipts = [r for r in rows if "successfully" in json.dumps(r)]
    assert receipts
    for row in receipts:
        rendered = render_event(row)
        assert rendered is not None
        assert rendered.body == "サブエージェントを起動しました"


def test_agent_call_names_its_description() -> None:
    from c_lord.transcript.formatter import render_event

    call = next(
        r
        for r in _parent_rows("plain")
        if r.get("type") == "assistant"
        and any(b.get("name") == "Agent" for b in r["message"]["content"])
    )
    rendered = render_event(call)
    assert rendered is not None
    assert rendered.body == "🔧 Agent: probe child A (calc)"


async def test_session_learned_late_does_not_replay_its_old_agents(tmp_path: Path) -> None:
    """No ``.clord-session``: the session is first named by a parent row."""
    project = tmp_path / "project"
    subdir = project / SESSION / "subagents"
    subdir.mkdir(parents=True)
    for f in (FIXTURES / "plain" / "subagents").iterdir():
        shutil.copy(f, subdir / f.name)
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, voices=True)
    await log.prime()
    log.observe_parent({"type": "user", "sessionId": SESSION, "message": {"content": "hi"}})
    await log.step()
    await log.step()
    assert poster.statuses == [] and poster.voices == []


async def test_run_without_a_notice_is_closed_when_meta_says_finished(tmp_path: Path) -> None:
    project, subdir = _project(tmp_path, SESSION)
    clock = Clock()
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, clock=clock)
    await log.prime()
    src = FIXTURES / "plain" / "subagents"
    meta = json.loads((src / f"agent-{ASKER_B}.meta.json").read_text())
    meta["finished"] = False
    (subdir / f"agent-{ASKER_B}.meta.json").write_text(json.dumps(meta))
    _append(subdir / f"agent-{ASKER_B}.jsonl", _events(src / f"agent-{ASKER_B}.jsonl"))
    await log.step()
    clock.now += 120
    await log.step()
    assert not any("終了" in p.text for p in poster.statuses)  # still running
    meta["finished"] = True
    (subdir / f"agent-{ASKER_B}.meta.json").write_text(json.dumps(meta))
    clock.now += 1
    await log.step()
    assert sum("終了" in p.text for p in poster.statuses) == 1


async def test_run_that_goes_silent_is_closed_eventually(tmp_path: Path) -> None:
    project, subdir = _project(tmp_path, SESSION)
    clock = Clock()
    poster = FakePoster()
    log = SubagentLog(thread_id=1, project_dir=project, poster=poster, clock=clock)
    await log.prime()
    src = FIXTURES / "plain" / "subagents"
    meta = json.loads((src / f"agent-{ASKER_B}.meta.json").read_text())
    meta.pop("finished")
    (subdir / f"agent-{ASKER_B}.meta.json").write_text(json.dumps(meta))
    _append(subdir / f"agent-{ASKER_B}.jsonl", _events(src / f"agent-{ASKER_B}.jsonl"))
    await log.step()
    clock.now += 3600
    await log.step()
    ends = [p.text for p in poster.statuses if "終了" in p.text]
    assert len(ends) == 1 and "記録が止まりました" in ends[0]


async def test_each_agent_gets_its_own_attachment_name(tmp_path: Path) -> None:
    """Japanese names have no ASCII slug; two agents must not share one name."""
    _, poster = await _plain_run(tmp_path, voices=False)
    # The fixture names are ASCII; check the numbering with a non-ASCII one too.
    from c_lord.transcript.subagents import AgentMeta, _Agent, _filename

    a = _Agent(agent_id="a1", path=tmp_path / "a", meta=AgentMeta(name="計算係"), ordinal=1)
    b = _Agent(agent_id="a2", path=tmp_path / "b", meta=AgentMeta(name="質問係"), ordinal=2)
    assert _filename(a) != _filename(b)
    assert _filename(a) == "subagent-1.md"
    names = {p.file and p.text.split("「")[1].split("」")[0] for p in poster.statuses if p.file}
    assert names == {"probe child A (calc)", "probe child B (asks A)"}
