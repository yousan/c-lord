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


async def test_an_old_name_whose_transcript_never_came_does_not_hold(tmp_path: Path) -> None:
    """Review of #1190: in production the threads with no transcript mostly do have a
    claim — one whose file never appeared. Past the resolver's grace it is not
    "reading", so the pane's exit does not wait on it every turn."""
    import os
    import time

    from c_lord.transcript.claim import claim_path
    from c_lord.transcript.resolver import CLAIM_GRACE_SECONDS

    project = tmp_path / "proj"
    project.mkdir()
    write_claim(project, SESSION_ID)
    old = time.time() - CLAIM_GRACE_SECONDS - 60
    os.utime(claim_path(project), (old, old))
    mirror = TranscriptMirror(thread_id=THREAD, project_dir=project, sink=_sink, poll_interval=0.02)
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        assert not turn_end_bus.is_tailing(THREAD)
    finally:
        await mirror.stop()


async def test_first_pasted_prompt_long_after_a_fresh_wake_still_holds(
    tmux_manager: MagicMock,  # noqa: F811
    tmp_path: Path,
) -> None:
    """#1223 (regression of the #1190 fix): a workspace woken with nothing to continue
    (``wake()`` → ``start_claude(prompt=None)``, e.g. /tmux-screenshot) names its
    transcript at wake time, but Claude writes it only with the first prompt. When
    that first (pasted) message came more than 120 s after the wake, the name was
    "old", the mirror said it was not reading, and the pane closed the turn early
    — 📊 / 🟡 / ``completion`` while the work went on (the #1132 symptom)."""
    import os

    from c_lord.transcript.claim import claim_path
    from c_lord.transcript.resolver import CLAIM_GRACE_SECONDS

    project = tmp_path / "proj"
    project.mkdir()
    write_claim(project, SESSION_ID)  # the wake's fresh start named the session
    woke = time.time() - CLAIM_GRACE_SECONDS - 60  # user wrote 3 min later
    os.utime(claim_path(project), (woke, woke))
    mirror = TranscriptMirror(thread_id=THREAD, project_dir=project, sink=_sink, poll_interval=0.05)
    mirror.start()
    try:
        await asyncio.sleep(0.15)
        events, calls, _ = await _run(tmux_manager, _script, start_grace=0.5)
        assert [e.error for e in events if e.is_complete] == [None]
        assert calls["n"] >= 40, f"closed early at poll {calls['n']} (#1132 is back)"
    finally:
        await mirror.stop()


def test_a_named_transcript_that_never_came_holds_only_its_first_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1223 keeps #1190's point: the grace counts from the first instruction sent
    under a name, once. A name whose transcript never came after that turn does not
    hold every later turn; a new name (another wake) gets its own first turn."""
    from c_lord import turn_end_bus as bus_mod
    from c_lord.transcript.resolver import CLAIM_GRACE_SECONDS

    now = {"t": 1000.0}
    monkeypatch.setattr(bus_mod.time, "monotonic", lambda: now["t"])
    bus = bus_mod.TurnEndBus()

    bus.note_pending_claim(THREAD, "a@1")
    assert not bus.is_tailing(THREAD), "an old name with no turn running does not hold"
    bus.note_turn_started(THREAD)
    assert bus.is_tailing(THREAD), "the first instruction under the name is due to appear"

    now["t"] += CLAIM_GRACE_SECONDS + 1
    assert not bus.is_tailing(THREAD)
    bus.note_turn_started(THREAD)
    assert not bus.is_tailing(THREAD), "the same name must not hold every later turn"

    bus.note_pending_claim(THREAD, "b@2")  # woken again: a new name
    bus.note_turn_started(THREAD)
    assert bus.is_tailing(THREAD)

    bus.note_pending_claim(THREAD, None)  # the transcript appeared, or the mirror stopped
    assert not bus.is_tailing(THREAD)
