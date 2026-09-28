"""#815: a turn that ends in ❌ Error must take its progress line with it.

The progress line (#539) is ended by the transcript mirror — on the turn-end
marker, the next prompt, or the final answer.  A claude that dies mid-turn
writes none of those, so the line kept counting "⏳ 待機中 … 長考の可能性" for
twelve hours after the turn had already ended in ❌ Error.  The run that ended
the turn is the one party that knows, so it has to say so.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.tmux_runner import NO_RESPONSE_ERROR_PREFIX
from c_lord.claude.types import MessageType, StreamEvent
from c_lord.cogs._run_helper import run_claude_in_thread
from c_lord.discord_ui.turn_progress import TurnProgress
from c_lord.transcript.mirror import TranscriptMirror

_CRASH = (
    "Claude exited without producing a response (possible startup failure or crash) "
    "— check the tmux pane."
)


class _Recorder:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.edits: list[str] = []
        self.deletes: list[object] = []

    async def post(self, text: str) -> object:
        self.posts.append(text)
        return f"msg-{len(self.posts)}"

    async def edit(self, handle: object, text: str) -> None:
        self.edits.append(text)

    async def delete(self, handle: object) -> None:
        self.deletes.append(handle)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def thread() -> MagicMock:
    t = MagicMock(spec=discord.Thread)
    t.id = 815
    t.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return t


@pytest.fixture
def repo() -> MagicMock:
    r = MagicMock()
    r.save = AsyncMock()
    return r


def _runner(*events: StreamEvent, preempted: bool = False) -> MagicMock:
    async def gen(*_a: object, **_k: object):
        for e in events:
            yield e

    r = MagicMock()
    r.run = gen
    r.preempted = preempted
    return r


def _result(error: str | None) -> StreamEvent:
    return StreamEvent(message_type=MessageType.RESULT, is_complete=True, error=error)


async def _mirror_showing_line(tmp_path: Path, rec: _Recorder, clock: _Clock):
    """A live mirror for thread 815 whose turn has gone quiet long enough to show the line."""
    progress = TurnProgress(
        post=rec.post, edit=rec.edit, delete=rec.delete, clock=clock, quiet_seconds=90.0
    )
    mirror = TranscriptMirror(
        thread_id=815, project_dir=tmp_path, sink=AsyncMock(), progress=progress
    )
    mirror.start()
    mirror.note_turn_started()
    clock.advance(100)
    await progress.tick()
    assert rec.posts, "precondition: the progress line is showing"
    return mirror, progress


async def _still_counting(progress: TurnProgress, rec: _Recorder, clock: _Clock) -> bool:
    before = len(rec.posts) + len(rec.edits)
    clock.advance(3600)
    await progress.tick()
    return len(rec.posts) + len(rec.edits) > before


class TestRunEndStopsTheLine:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            _CRASH,
            f"{NO_RESPONSE_ERROR_PREFIX} Claude never started this turn (pane unchanged "
            "for 300s). Send the message again, or check the tmux pane.",
        ],
        ids=["error", "no-response"],
    )
    async def test_failed_turn_removes_the_line(
        self, tmp_path: Path, thread: MagicMock, repo: MagicMock, error: str
    ) -> None:
        """AC1: RED before the fix — the line stayed armed and kept being edited."""
        rec, clock = _Recorder(), _Clock()
        mirror, progress = await _mirror_showing_line(tmp_path, rec, clock)
        try:
            await run_claude_in_thread(thread, _runner(_result(error)), repo, "x", None)
            assert rec.deletes == ["msg-1"]
            assert not await _still_counting(progress, rec, clock)
        finally:
            await mirror.stop()

    @pytest.mark.asyncio
    async def test_finished_turn_removes_the_line(
        self, tmp_path: Path, thread: MagicMock, repo: MagicMock
    ) -> None:
        """A turn the pane saw finish is finished, whatever the transcript said."""
        rec, clock = _Recorder(), _Clock()
        mirror, progress = await _mirror_showing_line(tmp_path, rec, clock)
        try:
            await run_claude_in_thread(thread, _runner(_result(None)), repo, "x", None)
            assert not await _still_counting(progress, rec, clock)
        finally:
            await mirror.stop()

    @pytest.mark.asyncio
    async def test_preempted_turn_leaves_the_line_to_its_successor(
        self, tmp_path: Path, thread: MagicMock, repo: MagicMock
    ) -> None:
        """The next message already restarted the line for ITS turn; do not take it away."""
        rec, clock = _Recorder(), _Clock()
        mirror, progress = await _mirror_showing_line(tmp_path, rec, clock)
        try:
            await run_claude_in_thread(
                thread, _runner(_result(None), preempted=True), repo, "x", None
            )
            assert rec.deletes == []
            assert await _still_counting(progress, rec, clock)
        finally:
            await mirror.stop()

    @pytest.mark.asyncio
    async def test_no_mirror_is_fine(self, thread: MagicMock, repo: MagicMock) -> None:
        """A thread with no live mirror (tests, older consumers) must not break the run."""
        await run_claude_in_thread(thread, _runner(_result(_CRASH)), repo, "x", None)
