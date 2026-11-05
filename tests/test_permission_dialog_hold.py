"""#1247: a permission dialog left open between turns must not eat the next request.

Production, 2026-11-04/05: a background sub-agent's Bash call stopped on a
permission dialog after the parent's turn had ended (``run_claude: exit
(outcome=ok)``, ten background agents still running). When the next request
arrived, c-lord typed its text plus Enter straight into that dialog:

- the Enter answered the dialog "Yes" — in both production cases a
  ``safetyCheck`` / ``dangerousRemoval`` dialog, so a dangerous ``rm -rf`` ran
  with no human judgement;
- the request itself never reached Claude (no person row for it in the
  transcript), while the turn still ended ``outcome=ok`` with a 🟡.

``_run_turn`` only looked for an open AskUserQuestion / plan menu before typing
(#358), never for a permission dialog. What the user should see instead: the
request is held, the thread is told once that a permission dialog is open in
Claude's pane (and what it asks), nothing is typed and no key is pressed, and
the request goes out as soon as a human has answered the dialog. If nobody
answers within the bound, the turn ends with an error that says the request
was NOT sent, so it can be resent.

The pane is a real capture (``tests/fixtures/panes/
i1247_permission_dialog_background_agent_v2_1_318.txt``, Claude Code 2.1.318,
``--permission-mode acceptEdits``): a background agent's ``rm -rf ./junk``
waiting for approval while the parent shows "Waiting for 1 background agent".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner, _normalize_capture
from c_lord.claude.types import MessageType, StreamEvent

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "panes"
    / "i1247_permission_dialog_background_agent_v2_1_318.txt"
)
PERMISSION_PANE = FIXTURE.read_text(encoding="utf-8")

DONE_PANE = """\
● done

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
"""

# The dialog after a human answered it in the pane: the agent's command ran and
# the parent is back at its input box.
ANSWERED_PANE = """\
● Agent "Delete junk directory" finished · 52s

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
"""

# Claude's own last answer ends with the marker phrase, above a live input box.
# That is prose, not a dialog — the request must go out at once (#156's lesson).
PROSE_ASKS_TO_PROCEED_PANE = """\
● I can delete the old clones now.
  Do you want to proceed?

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
"""

THREAD_ID = 1566967064349114438


@pytest.fixture
def tmux_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.capture_pane.return_value = ""
    mgr.is_claude_running.return_value = True
    mgr.start_claude.return_value = True
    mgr.send_input.return_value = True
    mgr.send_interrupt.return_value = True
    mgr.duplicate_window_names.return_value = []
    mgr.session_name = "clord"
    mgr._find_window_for_thread.return_value = "w65"
    return mgr


def _runner(tmux_manager: MagicMock) -> TmuxClaudeRunner:
    return TmuxClaudeRunner(
        tmux_manager=tmux_manager, thread_id=THREAD_ID, model="opus", timeout_seconds=5
    )


def _fast():  # noqa: ANN202 — a patch stack
    import contextlib

    stack = contextlib.ExitStack()
    for name, value in (
        ("_POLL_INTERVAL", 0.02),
        ("_RESPONSE_STABLE_TIMEOUT", 0.05),
        ("_POST_STARTUP_DELAY", 0.0),
        ("_MENU_NAV_DELAY", 0.0),
    ):
        stack.enter_context(patch(f"c_lord.claude.tmux_runner.{name}", value))
    # The hold's own pacing (new in #1247; ``create`` so this file runs on main).
    stack.enter_context(patch("c_lord.claude.tmux_runner._PERMISSION_HOLD_POLL", 0.01, create=True))
    return stack


def _keys_pressed(tmux_manager: MagicMock, raw_tmux: MagicMock) -> list[str]:
    """Every keystroke that could answer a dialog: send_keys and raw ``tmux send-keys``."""
    pressed = [f"send_keys{c.args[1:]}" for c in tmux_manager.send_keys.call_args_list]
    for call in raw_tmux.call_args_list:
        argv = call.args[0] if call.args else []
        if "send-keys" in argv:
            pressed.append(" ".join(argv))
    return pressed


def test_real_dialog_is_a_permission_prompt() -> None:
    """c-lord's detector recognises the real dialog, raw and normalised."""
    assert TmuxClaudeRunner._has_permission_prompt(PERMISSION_PANE) is True
    assert TmuxClaudeRunner._has_permission_prompt(_normalize_capture(PERMISSION_PANE)) is True


def test_real_dialog_shows_no_input_box() -> None:
    """The dialog replaces the input box — what tells it apart from prose (#1247)."""
    assert TmuxClaudeRunner._has_input_prompt(_normalize_capture(PERMISSION_PANE)) is False


@pytest.mark.asyncio
async def test_request_waits_for_the_dialog_and_is_sent_after_a_human_answers(
    tmux_manager: MagicMock,
) -> None:
    """The request is held while the dialog is open, then delivered — never typed into it."""
    runner = _runner(tmux_manager)
    captures = {"n": 0}
    dialog_open_at_send: list[bool] = []

    def capture(_tid: int, *args: object, **kwargs: object) -> str:
        # A human answers the dialog in the pane after a few looks.
        captures["n"] += 1
        if tmux_manager.send_input.called:
            return DONE_PANE
        return PERMISSION_PANE if captures["n"] <= 4 else ANSWERED_PANE

    def send_input(_tid: int, _prompt: str, **_kw: object) -> bool:
        dialog_open_at_send.append(captures["n"] <= 4)
        return True

    tmux_manager.capture_pane.side_effect = capture
    tmux_manager.send_input.side_effect = send_input
    events: list[StreamEvent] = []
    with _fast(), patch("c_lord.tmux._run") as raw_tmux:
        async for event in runner.run("<text>"):
            events.append(event)

    assert tmux_manager.send_input.called, "the request should still be delivered"
    assert dialog_open_at_send[0] is False, (
        "send_input typed the request into an open permission dialog — its Enter "
        "answers the dialog and the request never reaches Claude"
    )
    assert _keys_pressed(tmux_manager, raw_tmux) == [], "c-lord must not answer the dialog"
    notices = [e for e in events if getattr(e, "permission_dialog_hold", None)]
    assert len(notices) == 1, "the thread is told once that the request is on hold"
    assert notices[0].message_type == MessageType.SYSTEM
    assert "rm -rf ./junk" in notices[0].permission_dialog_hold
    assert len(notices[0].permission_dialog_hold) <= 300
    # (How the turn ends after the send is the poll loop's business; this mock
    # pane never changes after the send. It just must not be the hold's error.)
    final = [e for e in events if e.message_type == MessageType.RESULT]
    assert final and "送られていません" not in (final[-1].error or "")


@pytest.mark.asyncio
async def test_dialog_never_answered_ends_the_turn_unsent(tmux_manager: MagicMock) -> None:
    """Past the bound the turn ends as an error that says the request was not sent."""
    runner = _runner(tmux_manager)
    tmux_manager.capture_pane.return_value = PERMISSION_PANE
    events: list[StreamEvent] = []
    with (
        _fast(),
        patch("c_lord.claude.tmux_runner._PERMISSION_HOLD_TIMEOUT", 0.05, create=True),
        patch("c_lord.tmux._run") as raw_tmux,
    ):
        async for event in runner.run("<text>"):
            events.append(event)

    assert not tmux_manager.send_input.called
    assert _keys_pressed(tmux_manager, raw_tmux) == []
    final = [e for e in events if e.message_type == MessageType.RESULT]
    assert final and final[-1].is_complete
    assert final[-1].error is not None
    assert "送られていません" in final[-1].error


@pytest.mark.asyncio
async def test_stop_during_the_hold_sends_nothing(tmux_manager: MagicMock) -> None:
    """A stop / preempt while held ends the turn without typing or pressing anything."""
    runner = _runner(tmux_manager)
    tmux_manager.capture_pane.return_value = PERMISSION_PANE
    events: list[StreamEvent] = []
    with _fast(), patch("c_lord.tmux._run") as raw_tmux:
        async for event in runner.run("<text>"):
            events.append(event)
            if getattr(event, "permission_dialog_hold", None):
                # The next message preempts this turn while it is on hold.
                assert await runner.interrupt(silent=True) is True

    assert not tmux_manager.send_input.called
    # C-c in the dialog would answer it too: a held turn is withdrawn, not interrupted.
    assert not tmux_manager.send_interrupt.called
    assert _keys_pressed(tmux_manager, raw_tmux) == []
    final = [e for e in events if e.message_type == MessageType.RESULT]
    assert final and final[-1].error is None


@pytest.mark.asyncio
async def test_prose_asking_to_proceed_is_not_a_dialog(tmux_manager: MagicMock) -> None:
    """Claude's answer ending in "Do you want to proceed?" does not hold the request."""
    runner = _runner(tmux_manager)
    sent: list[int] = []

    def capture(_tid: int, *args: object, **kwargs: object) -> str:
        return DONE_PANE if sent else PROSE_ASKS_TO_PROCEED_PANE

    def send_input(_tid: int, _prompt: str, **_kw: object) -> bool:
        sent.append(1)
        return True

    tmux_manager.capture_pane.side_effect = capture
    tmux_manager.send_input.side_effect = send_input
    events: list[StreamEvent] = []
    with _fast(), patch("c_lord.tmux._run"):
        async for event in runner.run("<text>"):
            events.append(event)

    assert sent == [1]
    assert not [e for e in events if getattr(e, "permission_dialog_hold", None)]


@pytest.mark.asyncio
async def test_the_hold_notice_is_posted_to_the_thread() -> None:
    """The thread sees what is open, that the request was not sent, and what to do."""
    import discord

    from c_lord.cogs.event_processor import EventProcessor
    from c_lord.cogs.run_config import RunConfig

    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    repo = MagicMock()
    repo.save = AsyncMock()
    config = RunConfig(thread=thread, runner=MagicMock(), prompt="hi", repo=repo)
    await EventProcessor(config).process(
        StreamEvent(message_type=MessageType.SYSTEM, permission_dialog_hold="rm -rf ./junk")
    )

    thread.send.assert_awaited_once()
    embed = thread.send.await_args.kwargs["embed"]
    assert "まだ送っていません" in embed.title
    assert "rm -rf ./junk" in embed.description
    assert "30 分" in embed.description
    assert "/workspace-stop" in embed.description
    repo.save.assert_not_awaited()  # not a session save
