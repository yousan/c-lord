"""A turn that crosses a bot restart keeps being the turn it was (#1011/#1012).

The mirror restored at startup tails from the end of the transcript, so it used
to start knowing nothing: no turn open, no background agents out. Two things
went wrong from there:

* #1011 — a background notification landing in the person's turn read as a turn
  Claude started by itself: a second turn-end event (``self_resumed_turn`` beside
  #978's ``restart_crossed``) and the person's answer posted without its quote.
* #1012 — the ``restart_crossed`` completion went out at once, while agents the
  turn launched before the restart were still running (#917 holds it).

The restored mirror now reads back where the transcript stands.
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
from c_lord.transcript.mirror import TranscriptMirror, _turn_state_at_end
from c_lord.turn_events import StartedBy

from .helpers import clord_marker_event

THREAD_ID = 1011

_NOTIFICATION = (
    "<task-notification>\n<task-id>b1</task-id>\n<status>running</status>\n"
    '<summary>Monitor event: "lap 2"</summary>\n</task-notification>'
)
_LAUNCH_TEXT = (
    "Async agent launched successfully. (This tool result is internal metadata)\n"
    "agentId: {id} (internal ID - do not mention to user.)"
)
TURN_END = {"type": "system", "subtype": "turn_duration"}


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
    return [json.loads(line) for line in path.read_text().splitlines()]


def _user(content: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}}


def _text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }  # noqa: E501


def _tool_use() -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "Agent", "input": {}}],
        },
    }


def _launch(agent_id: str) -> dict:
    text = _LAUNCH_TEXT.format(id=agent_id)
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": text}],
        },
    }


def _report(agent_id: str) -> dict:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "content": (
            f"<task-notification>\n<task-id>{agent_id}</task-id>\n"
            "<status>completed</status>\n</task-notification>"
        ),
    }


def _write(path: Path, *events: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _thread() -> SimpleNamespace:
    return SimpleNamespace(id=THREAD_ID, parent_id=1505747831447883806, guild=SimpleNamespace(id=1))


class _Restored:
    """A mirror restored at startup over a transcript the old process left behind."""

    def __init__(self, tmp_path: Path, *before: dict) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        self.jsonl = project / "s.jsonl"
        _write(self.jsonl, clord_marker_event(), *before)
        os.utime(self.jsonl, (1, 1))
        self.sink: list[str] = []
        self.replies: list[str] = []
        self.files: list[tuple[str, dict]] = []

        async def sink(text: str) -> None:
            self.sink.append(text)

        def record(text: str) -> None:
            reply_tracker.record_reply_message(
                THREAD_ID, SimpleNamespace(id=20_000 + len(self.replies))
            )  # noqa: E501

        async def reply_sink(text: str, **_: int) -> None:
            record(text)
            self.replies.append(text)

        async def file_sink(text: str, _path: str, **kwargs: object) -> None:
            record(text)
            self.files.append((text, kwargs))

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
        )

    async def __aenter__(self) -> _Restored:
        self.mirror.start()
        await asyncio.sleep(0.15)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.mirror.stop()

    async def feed(self, *events: dict) -> None:
        _write(self.jsonl, *events)
        await asyncio.sleep(0.25)
        await turn_events.drain()


def _announce() -> None:
    """What ``_close_recovered_turn`` does when the lamp sees the turn over (#978)."""
    turn_events.announce_recovered_turn(
        _thread(), StartedBy("user", "777"), started_at=None, failure_reason=None
    )


# -- #1011 --------------------------------------------------------------------


async def test_a_notification_inside_the_persons_turn_makes_no_second_event(
    tmp_path: Path, log: Path
) -> None:
    """#1011 AC1. RED: ``self_resumed_turn`` *and* ``restart_crossed`` — two ends."""
    async with _Restored(tmp_path, _tool_use()) as restored:
        await restored.feed(_user(_NOTIFICATION), _text("調べ終わりました。"), TURN_END)
        _announce()
        await turn_events.drain()
    events = _lines(log)
    assert [e.get("reason") for e in events] == ["restart_crossed"], events


async def test_the_persons_answer_is_still_a_quoted_reply(tmp_path: Path, log: Path) -> None:
    """#1011 AC2. RED: posted as a notification turn's answer — quote dropped."""
    async with _Restored(tmp_path, _tool_use()) as restored:
        await restored.feed(_user(_NOTIFICATION), _text("調べ終わりました。"), TURN_END)
    quoted = restored.replies + [t for t, kw in restored.files if kw.get("quote") is not False]
    assert quoted == ["調べ終わりました。"], (restored.sink, restored.replies, restored.files)


async def test_a_notification_between_turns_is_still_claudes_own_turn(
    tmp_path: Path, log: Path
) -> None:
    """#1011 AC3: the person's turn ended before the restart — #961 as before."""
    async with _Restored(tmp_path, _text("終わりました。"), TURN_END) as restored:
        await restored.feed(_user(_NOTIFICATION), _text("Still running."), TURN_END)
    assert restored.sink[-1:] == ["Still running."]
    assert restored.replies == []
    assert [e.get("reason") for e in _lines(log)] == ["self_resumed_turn"]


# -- #1012 --------------------------------------------------------------------


async def test_the_completion_waits_for_agents_launched_before_the_restart(
    tmp_path: Path, log: Path
) -> None:
    """#1012 AC1/AC2. RED: the completion went out while the agent still ran."""
    async with _Restored(tmp_path, _tool_use(), _launch("a1b2c3")) as restored:
        await restored.feed(_text("エージェントを待っています。"), TURN_END)
        _announce()
        await turn_events.drain()
        assert _lines(log) == [], "completion sent while the agent was still running"

        await restored.feed(
            _report("a1b2c3"), _user(_NOTIFICATION), _text("全部そろいました。"), TURN_END
        )
    events = _lines(log)
    assert [(e["kind"], e.get("reason")) for e in events] == [("completion", "restart_crossed")]


async def test_without_agents_the_completion_goes_out_at_once(tmp_path: Path, log: Path) -> None:
    """#1012 AC3: nothing to wait for — one ``restart_crossed`` straight away (#982)."""
    async with _Restored(tmp_path, _tool_use()) as restored:
        await restored.feed(_text("終わりました。"), TURN_END)
        _announce()
        await turn_events.drain()
    assert [e.get("reason") for e in _lines(log)] == ["restart_crossed"]


# -- the read-back itself -----------------------------------------------------


def test_read_back_follows_the_live_rules(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    _write(path, clord_marker_event(), _tool_use(), _launch("aaa"), _launch("bbb"), _report("aaa"))
    assert _turn_state_at_end(path) == ("person", None, {"bbb"})

    _write(path, _text("待ちます"), TURN_END)
    assert _turn_state_at_end(path) == (None, None, {"bbb"})

    _write(path, _user(_NOTIFICATION))
    assert _turn_state_at_end(path) == ("notification", None, {"bbb"})

    # a ``!`` command typed in the pane opens nobody's turn (#1013)
    _write(path, _text("x"), TURN_END, _user("<bash-input>ls</bash-input>"))
    assert _turn_state_at_end(path) == (None, None, {"bbb"})


def test_read_back_of_a_missing_file_knows_nothing(tmp_path: Path) -> None:
    assert _turn_state_at_end(tmp_path / "nope.jsonl") == (None, None, set())


# -- review of PR #1018 -------------------------------------------------------


async def test_an_answer_written_while_reading_back_is_delivered(
    tmp_path: Path, monkeypatch, log: Path
) -> None:
    """RED: the read-back read one end of the file, the tail started at a later
    one, and what Claude wrote in between reached neither — the answer was lost."""
    from c_lord.transcript import mirror as mirror_mod

    original = mirror_mod._turn_state_at_end
    holder: dict = {}

    def slow_read_back(path: Path, *args: object, **kwargs: object):
        result = original(path, *args, **kwargs)  # type: ignore[arg-type]
        # Claude keeps writing while c-lord reads 16 MiB back.
        _write(holder["jsonl"], _text("最終回答です。"), TURN_END)
        return result

    monkeypatch.setattr(mirror_mod, "_turn_state_at_end", slow_read_back)
    restored = _Restored(tmp_path, _tool_use())
    holder["jsonl"] = restored.jsonl
    async with restored:
        await asyncio.sleep(0.5)
    delivered = restored.sink + restored.replies + [t for t, _ in restored.files]
    assert "最終回答です。" in delivered, delivered


# Claude Code's record on --resume when the previous process died with agents
# out (real shape, a transcript of 2026-10-09): several ids in one notification.
_STOPPED_TOGETHER = (
    "<task-notification>\n<task-id>aaa111</task-id>\n<task-id>bbb222</task-id>\n"
    "<status>stopped</status>\n<summary>2 background agents didn't finish before the "
    "previous session ended</summary>\n</task-notification>"
)


def _stopped_together() -> dict:
    return {"type": "queue-operation", "operation": "enqueue", "content": _STOPPED_TOGETHER}


def test_agents_reported_stopped_together_are_not_out(tmp_path: Path) -> None:
    """#1042. RED: ``(None, None, {'bbb222'})`` — only the first ``<task-id>`` was read."""
    path = tmp_path / "s.jsonl"
    _write(
        path, _tool_use(), _launch("aaa111"), _launch("bbb222"), _text("x"), TURN_END,
        _stopped_together(), _user(_STOPPED_TOGETHER), _text("止まっていました"), TURN_END,
    )  # fmt: skip
    assert _turn_state_at_end(path) == (None, None, set())


async def test_a_later_turn_is_not_held_by_agents_stopped_together(
    tmp_path: Path, log: Path
) -> None:
    """#1042 through #1018: the read-back must not carry dead agents forward."""
    before = (
        _tool_use(), _launch("aaa111"), _launch("bbb222"), _text("x"), TURN_END,
        _stopped_together(), _user(_STOPPED_TOGETHER), _text("止まっていました"), TURN_END,
        _user("次の依頼"),
    )  # fmt: skip
    async with _Restored(tmp_path, *before) as restored:
        await restored.feed(_text("終わりました。"), TURN_END)
        _announce()
        await turn_events.drain()
    assert [e.get("reason") for e in _lines(log)] == ["restart_crossed"]


def test_read_back_stops_at_the_tails_start_and_reads_in_chunks(
    tmp_path: Path, monkeypatch
) -> None:
    """#1018: only whole lines before *end*; the window and chunk edges drop nothing."""
    from c_lord.transcript import mirror as mirror_mod

    monkeypatch.setattr(mirror_mod, "_READ_BACK_CHUNK", 7)
    path = tmp_path / "s.jsonl"
    lines = [json.dumps({"n": i}).encode() for i in range(40)]
    path.write_bytes(b"\n".join(lines) + b"\n")
    end = len(b"\n".join(lines[:30])) + 1  # where line 30 starts

    assert list(mirror_mod._read_back_lines(path, end)) == lines[:30]

    monkeypatch.setattr(mirror_mod, "_READ_BACK_MAX_BYTES", 50)
    got = list(mirror_mod._read_back_lines(path, end))
    assert got and got == lines[30 - len(got) : 30]  # a torn first line is dropped


# -- #1093 --------------------------------------------------------------------
# A turn Claude opened by itself before the restart — the read-back knows that
# now (#1011), so its end is announced like any self-started turn's (#961).


def _notification_at(ts: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": _NOTIFICATION}}


def _wakeup_at(ts: str) -> list[dict]:
    return [
        {"type": "system", "subtype": "scheduled_task_fire", "timestamp": ts, "isMeta": False},
        {
            "type": "user",
            "timestamp": ts,
            "isMeta": True,
            "message": {"role": "user", "content": "(起床予定のプロンプト)"},
        },
    ]


async def test_a_notification_turn_crossing_the_restart_gets_its_end(
    tmp_path: Path, log: Path
) -> None:
    """#1093 AC1/AC3. RED: no event — the restored mirror never noted a self start."""
    before = (_text("終わりました。"), TURN_END, _notification_at("2026-10-29T19:27:32.978Z"))
    async with _Restored(tmp_path, *before, _tool_use()) as restored:
        await restored.feed(_text("CI が通りました。"), TURN_END)
    events = _lines(log)
    assert [(e["kind"], e.get("reason")) for e in events] == [("completion", "self_resumed_turn")]
    assert events[0]["started_by"] == {"type": "self", "id": None, "name": None}
    assert events[0]["turn_started_at"].startswith("2026-10-29T19:27:32.978")


async def test_a_wakeup_turn_crossing_the_restart_gets_its_end(tmp_path: Path, log: Path) -> None:
    """#1093 AC2: the same for a turn a scheduled wakeup opened (#1092)."""
    before = (_text("終わりました。"), TURN_END, *_wakeup_at("2026-10-29T18:36:00.386Z"))
    async with _Restored(tmp_path, *before) as restored:
        await restored.feed(_text("やることはありません。"), TURN_END)
    events = _lines(log)
    assert [e.get("reason") for e in events] == ["self_resumed_turn"]
    assert events[0]["turn_started_at"].startswith("2026-10-29T18:36:00.386")


async def test_a_crossing_self_turn_with_agents_out_is_held(tmp_path: Path, log: Path) -> None:
    """#1093 with #1012: agents it launched before the restart hold the event."""
    before = (
        _text("x"), TURN_END, _notification_at("2026-10-29T19:00:00.000Z"),
        _tool_use(), _launch("a1b2c3"),
    )  # fmt: skip
    async with _Restored(tmp_path, *before) as restored:
        await restored.feed(_text("エージェント待ち"), TURN_END)
        assert _lines(log) == []
        await restored.feed(
            _report("a1b2c3"), _user(_NOTIFICATION), _text("そろいました"), TURN_END
        )
    assert [e.get("reason") for e in _lines(log)] == ["self_resumed_turn"]


def test_read_back_knows_when_the_open_turn_began(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    _write(path, _text("x"), TURN_END, _notification_at("2026-10-29T19:27:32.978Z"), _tool_use())
    assert _turn_state_at_end(path) == ("notification", "2026-10-29T19:27:32.978Z", set())
