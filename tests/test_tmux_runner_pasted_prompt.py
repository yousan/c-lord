"""The pane may not end a turn the transcript says is still going (#1132).

A long prompt Claude Code takes as a paste shows no spinner and nothing new on
the pane for the 15–40 s before Claude's first output, while the response area
differs from the previous turn's. The pane exit ("response stable 3 s, input
box visible, no spinner") closed four production turns 14–19 s in on
2026-10-29 — 📊, 🟡 and a ``completion`` with ``last_message_id: null`` — and
the work then went on for up to 50 minutes.
# <<REDACTED 5 lines>>
from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner
from c_lord.turn_end_bus import turn_end_bus


def _pane(response: str) -> str:
    """A finished-looking pane: a response, the input box, no spinner."""
    return "\n".join(
        [
            "❯ [Pasted text #1 +42 lines]",
            "",
            response,
            "",
            "─" * 40,
            "❯",
            "─" * 40,
            "-- INSERT -- ⏵⏵ bypass permissions on",
        ]
    )


PREVIOUS = _pane("● 前のターンの答えです。")
# What the pane shows once the paste was sent and before Claude's first output:
# still no spinner, but the response area is no longer the previous turn's.
WAITING = _pane("● 続きの依頼を受け取りました。")


@pytest.fixture
def tmux_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.capture_pane.return_value = ""
    mgr.is_claude_running.return_value = True
    mgr.start_claude.return_value = True
    mgr.send_input.return_value = True
    mgr.send_interrupt.return_value = True
    mgr.kill_session.return_value = True
    mgr.duplicate_window_names.return_value = []
    mgr.session_name = "clord"
    mgr._find_window_for_thread.return_value = "work1"
    mgr._target.return_value = "@7"
    return mgr


async def _run(tmux_manager: MagicMock, thread_id: int, script) -> tuple[list, dict]:
    runner = TmuxClaudeRunner(
        tmux_manager=tmux_manager, thread_id=thread_id, model="sonnet", timeout_seconds=600
    )
    calls = {"n": 0}

    def capture(_tid):
        calls["n"] += 1
        return script(calls["n"])

    tmux_manager.capture_pane.side_effect = capture
    events: list = []

    async def drain() -> None:
        async for event in runner.run("長い依頼"):
            events.append(event)

    with (
        patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.01),
        patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.03),
    ):
        await asyncio.wait_for(drain(), timeout=10.0)
    return events, calls


@pytest.fixture
def thread_id():
    tid = 11320
    turn_end_bus.forget(tid)
    yield tid
    turn_end_bus.forget(tid)


async def test_pane_does_not_end_a_turn_the_transcript_has_open(tmux_manager, thread_id) -> None:
    """AC2: the instruction is in the transcript with no end after it → the pane waits."""
    mark_at = 60

    def script(n: int) -> str:
        if n == 2:
            turn_end_bus.note_prompt(thread_id)  # the paste reached the transcript
        if n <= 2:
            return PREVIOUS
        if n == mark_at:
            turn_end_bus.mark(thread_id)  # Claude really finished
        return WAITING

    events, calls = await _run(tmux_manager, thread_id, script)

    assert calls["n"] >= mark_at, (
        f"the pane ended the turn at poll {calls['n']}, before the transcript's "
        f"turn end at poll {mark_at} (#1132)"
    )
    result = [e for e in events if e.is_complete]
    assert len(result) == 1 and result[0].error is None


async def test_pane_waits_for_a_paste_to_reach_the_transcript(tmux_manager, thread_id) -> None:
    """A mirror is tailing, but the paste is not in the transcript yet → wait for it."""
    prompt_at, mark_at = 30, 60

    def script(n: int) -> str:
        if n == 2:
            turn_end_bus.note_tailing(thread_id, True)
        if n == prompt_at:
            turn_end_bus.note_prompt(thread_id)
        if n == mark_at:
            turn_end_bus.mark(thread_id)
        return PREVIOUS if n <= 2 else WAITING

    _events, calls = await _run(tmux_manager, thread_id, script)
    assert calls["n"] >= mark_at


async def test_without_a_transcript_the_pane_still_decides(tmux_manager, thread_id) -> None:
    """AC3: nothing tailing, no record → the pane exit as before."""

    def script(n: int) -> str:
        return PREVIOUS if n <= 2 else WAITING

    events, calls = await _run(tmux_manager, thread_id, script)
    assert calls["n"] < 30
    assert [e.error for e in events if e.is_complete] == [None]


async def test_a_transcript_that_never_ends_the_turn_is_not_waited_on_forever(
    tmux_manager, thread_id
) -> None:
    """A build that writes no turn-end marker: the pane is believed after the cap."""

    def script(n: int) -> str:
        if n == 2:
            turn_end_bus.note_tailing(thread_id, True)
            turn_end_bus.note_prompt(thread_id)
        return PREVIOUS if n <= 2 else WAITING

    with patch("c_lord.claude.tmux_runner._IN_FLIGHT_PANE_GRACE", 0.3):
        events, calls = await _run(tmux_manager, thread_id, script)
    assert [e.error for e in events if e.is_complete] == [None]
