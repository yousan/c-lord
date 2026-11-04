"""#1190 (#1132 AC3): a thread with no transcript to read still ends its turn on the pane.

#1132 holds the pane's "done" while a mirror reads the transcript and the
instruction has not reached it yet (a pasted prompt). But the mirror said "I am
reading" the moment it started, transcript or not — so in a thread whose
transcript cannot be found at all (the very threads that already get no
replies) 📊, 🟡 and ``completion`` came 120 s after the send instead of when the
pane finished. The mirror now says so only once there is a transcript to read,
or one c-lord has named (``.clord-session``) that Claude is about to write.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner
from c_lord.transcript.claim import write_claim
from c_lord.transcript.mirror import TranscriptMirror
from c_lord.turn_end_bus import turn_end_bus
from tests.test_tmux_runner_pasted_prompt import PREVIOUS, WAITING, tmux_manager  # noqa: F401

THREAD = 51151
SESSION_ID = "0b7c4a1e-1111-4222-8333-944455556666"


@pytest.fixture(autouse=True)
def _clean():
    turn_end_bus.forget(THREAD)
    yield
    turn_end_bus.forget(THREAD)


async def _sink(_m: str) -> None:
    return None


async def _run(mgr: MagicMock, script, *, start_grace: float) -> tuple[list, dict, float]:
    runner = TmuxClaudeRunner(
        tmux_manager=mgr, thread_id=THREAD, model="sonnet", timeout_seconds=600
    )
    calls = {"n": 0}

    def capture(_tid):
        calls["n"] += 1
        return script(calls["n"])

    mgr.capture_pane.side_effect = capture
    events: list = []

    async def drain():
        async for e in runner.run("長い依頼"):
            events.append(e)

    t0 = time.monotonic()
    with (
        patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.01),
        patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.03),
        patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_FALLBACK", 0.3),
        patch("c_lord.claude.tmux_runner._IDLE_TIMEOUT", 0.1),
        patch("c_lord.claude.tmux_runner._IN_FLIGHT_PANE_GRACE", 0.3),
        patch("c_lord.claude.tmux_runner._TURN_START_GRACE", start_grace),
    ):
        await asyncio.wait_for(drain(), timeout=10.0)
    return events, calls, time.monotonic() - t0


def _script(n: int) -> str:
    return PREVIOUS if n <= 2 else WAITING


async def test_a_mirror_with_no_transcript_does_not_delay_the_pane_exit(
    tmux_manager: MagicMock,  # noqa: F811
    tmp_path: Path,
) -> None:
    """RED: ``is_tailing`` was True with no jsonl in sight, and the run waited out
    the whole turn-start grace (≥ 40 polls here) instead of closing at ~5."""
    project = tmp_path / "proj"
    project.mkdir()
    mirror = TranscriptMirror(thread_id=THREAD, project_dir=project, sink=_sink, poll_interval=0.05)
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        assert not turn_end_bus.is_tailing(THREAD)
        events, calls, _ = await _run(tmux_manager, _script, start_grace=0.5)
        assert [e.error for e in events if e.is_complete] == [None]
        assert calls["n"] < 40, calls
    finally:
        await mirror.stop()
    assert not turn_end_bus.is_tailing(THREAD)


async def test_a_named_transcript_not_yet_written_still_holds(
    tmux_manager: MagicMock,  # noqa: F811
    tmp_path: Path,
) -> None:
    """A fresh session: c-lord named the transcript, Claude writes it with the
    first (pasted) prompt. That is the #1132 case — the pane must still wait."""
    project = tmp_path / "proj"
    project.mkdir()
    write_claim(project, SESSION_ID)
    mirror = TranscriptMirror(thread_id=THREAD, project_dir=project, sink=_sink, poll_interval=0.05)
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        assert turn_end_bus.is_tailing(THREAD)
        events, calls, _ = await _run(tmux_manager, _script, start_grace=0.5)
        assert [e.error for e in events if e.is_complete] == [None]
        assert calls["n"] >= 40, calls
    finally:
        await mirror.stop()
    assert not turn_end_bus.is_tailing(THREAD)


async def test_a_transcript_that_appears_later_turns_tailing_on(tmp_path: Path) -> None:
    """No transcript at start (not tailing), then one appears: tailing from then on."""
    from .transcript.helpers import clord_transcript

    project = tmp_path / "proj"
    project.mkdir()
    mirror = TranscriptMirror(thread_id=THREAD, project_dir=project, sink=_sink, poll_interval=0.02)
    mirror.start()
    try:
        await asyncio.sleep(0.1)
        assert not turn_end_bus.is_tailing(THREAD)
        clord_transcript(project / "s.jsonl")
        await asyncio.sleep(0.2)
        assert turn_end_bus.is_tailing(THREAD)
    finally:
        await mirror.stop()
    assert not turn_end_bus.is_tailing(THREAD)
