"""A mirror that cannot find its transcript has to say so out loud (#773/#585).

#627 chose silence as the safe failure: rather than mirror a stranger's
conversation, post nothing.  That was right about the danger and wrong about the
cost — when CLI 2.1.278 made *every* thread unrecognisable, seventeen production
threads ran a full day of work while Discord showed nothing after the start
notice, and nobody could tell "the bot is thinking" from "the bot is broken".

So silence stays, but it stops being invisible: a mirror whose turn is running
and which has resolved nothing for a while tells the thread, once.

An idle mirror must stay quiet — ``on_ready`` starts one for every open session
on this host, and a workspace nobody has run Claude in has no transcript by
design.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from c_lord.transcript.mirror import TranscriptMirror
from c_lord.transcript.tail import UnresolvedTranscript, tail_events

from .helpers import clord_transcript


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=timeout)


# ── the tail reports it ──────────────────────────────────────────────────


async def test_tail_reports_when_nothing_resolves(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    # A sub-invocation's transcript: present, but never ours (#627).
    (project / "sub.jsonl").write_text(
        json.dumps({"type": "assistant", "uuid": "u1"}) + "\n", encoding="utf-8"
    )

    reports: list[UnresolvedTranscript] = []

    async def on_unresolved(report: UnresolvedTranscript) -> None:
        reports.append(report)

    agen = tail_events(
        project,
        poll_interval=0.01,
        on_unresolved=on_unresolved,
        unresolved_after=0.05,
    )

    async def pull() -> None:
        async for _ in agen:
            pass

    task = asyncio.create_task(pull())
    try:
        await _wait_for(lambda: reports)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert reports[0].project_dir == project
    assert reports[0].candidates == 1


async def test_tail_stays_quiet_while_the_transcript_resolves(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    clord_transcript(project / "s1.jsonl")

    reports: list[UnresolvedTranscript] = []

    async def on_unresolved(report: UnresolvedTranscript) -> None:
        reports.append(report)

    agen = tail_events(
        project, poll_interval=0.01, on_unresolved=on_unresolved, unresolved_after=0.05
    )

    async def pull() -> None:
        async for _ in agen:
            pass

    task = asyncio.create_task(pull())
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert reports == []


# ── the mirror decides whether to tell the reader ────────────────────────


def _mirror(project: Path, posted: list[str], **kwargs) -> TranscriptMirror:
    async def sink(text: str) -> None:
        posted.append(text)

    return TranscriptMirror(
        thread_id=1,
        project_dir=project,
        sink=sink,
        poll_interval=0.01,
        **kwargs,
    )


@pytest.mark.parametrize("expect_turn", [True, False])
async def test_only_a_running_turn_gets_told(tmp_path: Path, expect_turn: bool) -> None:
    """A turn is running → say it.  Nothing is running → do not invent an alarm."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "sub.jsonl").write_text(
        json.dumps({"type": "assistant", "uuid": "u1"}) + "\n", encoding="utf-8"
    )
    posted: list[str] = []
    mirror = _mirror(project, posted, expect_turn=expect_turn, unresolved_after=0.05)
    mirror.start()
    try:
        if expect_turn:
            await _wait_for(lambda: posted)
        else:
            await asyncio.sleep(0.3)
    finally:
        await mirror.stop()

    assert bool(posted) is expect_turn
    if expect_turn:
        assert "transcript" in posted[0] or "見つかりません" in posted[0]


async def test_the_reader_is_told_at_most_once_per_turn(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "sub.jsonl").write_text(
        json.dumps({"type": "assistant", "uuid": "u1"}) + "\n", encoding="utf-8"
    )
    posted: list[str] = []
    mirror = _mirror(project, posted, expect_turn=True, unresolved_after=0.02)
    mirror.start()
    try:
        await _wait_for(lambda: posted)
        await asyncio.sleep(0.25)
    finally:
        await mirror.stop()

    assert len(posted) == 1, posted
