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
    # A hold that never ends fails the test in seconds, not in 30 minutes.
    stack.enter_context(
        patch("c_lord.claude.tmux_runner._PERMISSION_HOLD_TIMEOUT", 2.0, create=True)
    )
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


# --- Review of 84fa59e ------------------------------------------------------

PANES = Path(__file__).parent / "fixtures" / "panes"
# Real captures of the Edit / Write dialogs a background agent raises in the
# default permission mode (Claude Code 2.1.318). They carry no
# ``_PERMISSION_PROMPT_MARKERS`` phrase.
EDIT_PANE = (PANES / "i1247_edit_dialog_background_agent_v2_1_318.txt").read_text(encoding="utf-8")
CREATE_PANE = (PANES / "i1247_create_dialog_background_agent_v2_1_318.txt").read_text(
    encoding="utf-8"
)
# A second Bash dialog from another agent: the real dialog, another command.
SECOND_DIALOG_PANE = PERMISSION_PANE.replace("rm -rf ./junk;", "rm -rf ./other-junk;")
# A redraw between two states: neither the dialog's question nor the input box.
TRANSIENT_PANE = '● Agent "Delete junk directory" is running…\n\n  ⎿  Bash(rm -rf ./junk)\n'


async def _scripted_run(
    tmux_manager: MagicMock, script: list[str]
) -> tuple[list[StreamEvent], list[str]]:
    """Run a turn whose pane shows ``script`` capture by capture (last one repeats).

    Returns the events and the pane each ``send_input`` was typed into.
    """
    runner = _runner(tmux_manager)
    shown = {"pane": script[0], "n": 0}
    typed_into: list[str] = []

    def capture(_tid: int, *args: object, **kwargs: object) -> str:
        if typed_into:
            shown["pane"] = DONE_PANE
        else:
            shown["pane"] = script[min(shown["n"], len(script) - 1)]
            shown["n"] += 1
        return shown["pane"]

    def send_input(_tid: int, _prompt: str, **_kw: object) -> bool:
        typed_into.append(shown["pane"])
        return True

    tmux_manager.capture_pane.side_effect = capture
    tmux_manager.send_input.side_effect = send_input
    events: list[StreamEvent] = []
    with _fast(), patch("c_lord.tmux._run") as raw_tmux:
        async for event in runner.run("<text>"):
            events.append(event)
    assert _keys_pressed(tmux_manager, raw_tmux) == [], "c-lord must not answer a dialog"
    return events, typed_into


@pytest.mark.asyncio
async def test_a_second_dialog_right_after_the_first_is_held_too(tmux_manager: MagicMock) -> None:
    """HIGH 1: agent 2's dialog opens as soon as agent 1's is answered."""
    script = [PERMISSION_PANE] * 3 + [ANSWERED_PANE, SECOND_DIALOG_PANE, SECOND_DIALOG_PANE]
    script += [SECOND_DIALOG_PANE] * 3 + [ANSWERED_PANE]
    events, typed_into = await _scripted_run(tmux_manager, script)

    assert typed_into == [ANSWERED_PANE], "the request was typed into the second dialog"
    notices = [e.permission_dialog_hold for e in events if e.permission_dialog_hold]
    assert len(notices) == 2 and "other-junk" in notices[1]


@pytest.mark.asyncio
async def test_the_hold_ends_only_when_the_input_box_is_back(tmux_manager: MagicMock) -> None:
    """HIGH 1: a redraw with no dialog and no input box is not "answered"."""
    script = [PERMISSION_PANE] * 3 + [TRANSIENT_PANE] * 3 + [ANSWERED_PANE]
    _events, typed_into = await _scripted_run(tmux_manager, script)
    assert typed_into == [ANSWERED_PANE]


@pytest.mark.parametrize("pane", [EDIT_PANE, CREATE_PANE], ids=["edit", "create"])
def test_edit_and_create_dialogs_hold_the_request(pane: str) -> None:
    """MEDIUM 3: the real Edit / Write dialogs count as permission dialogs before a send."""
    from c_lord.claude.tmux_runner import _permission_dialog_open

    assert _permission_dialog_open(_normalize_capture(pane)) is True


@pytest.mark.parametrize("pane", [EDIT_PANE, CREATE_PANE], ids=["edit", "create"])
def test_in_turn_auto_accept_still_ignores_edit_and_create_dialogs(pane: str) -> None:
    """Decision 4 unchanged: inside a turn these stay the unknown-prompt alert, not an Enter."""
    norm = _normalize_capture(pane)
    assert TmuxClaudeRunner._has_permission_prompt(norm) is False
    assert TmuxClaudeRunner._has_unknown_interactive(norm) is True


@pytest.mark.asyncio
async def test_request_is_not_typed_into_an_edit_dialog(tmux_manager: MagicMock) -> None:
    """MEDIUM 3: the Enter would approve the edit just like a Bash call."""
    events, typed_into = await _scripted_run(tmux_manager, [EDIT_PANE] * 3 + [ANSWERED_PANE])
    assert typed_into == [ANSWERED_PANE]
    notices = [e.permission_dialog_hold for e in events if e.permission_dialog_hold]
    assert notices and "notes.txt" in notices[0]


@pytest.mark.parametrize("line", [">", "❯"])
def test_a_command_line_that_looks_like_an_input_box_keeps_the_hold(line: str) -> None:
    """MEDIUM 4: a bare ``>`` / ``❯`` row inside the command is not the input box."""
    from c_lord.claude.tmux_runner import _permission_dialog_open

    pane = PERMISSION_PANE.replace(' rm -rf ./junk; echo "exit=$?"', f" cat <<'EOF'\n {line}\n EOF")
    assert pane != PERMISSION_PANE
    assert _permission_dialog_open(_normalize_capture(pane)) is True


def _ask_proceed_pane() -> str:
    """The real AskUserQuestion menu (i751) whose question is "Do you want to proceed?"."""
    pane = (PANES / "i751_ask_menu_open_v2_1_280.txt").read_text(encoding="utf-8")
    asked = pane.replace("#751 の検証を続けますか?", "Do you want to proceed?")
    assert asked != pane
    return asked


@pytest.mark.asyncio
async def test_an_ask_menu_asking_to_proceed_is_not_held(tmux_manager: MagicMock) -> None:
    """LOW 6: an AskUserQuestion menu goes the #358 way (Esc, then send), not a 30-min hold."""
    runner = _runner(tmux_manager)
    tmux_manager.capture_pane.side_effect = lambda *_a, **_k: (
        DONE_PANE if tmux_manager.send_input.called else _ask_proceed_pane()
    )
    events: list[StreamEvent] = []
    with _fast(), patch("c_lord.tmux._run"):
        async for event in runner.run("<text>"):
            events.append(event)

    assert tmux_manager.send_input.called
    assert not [e for e in events if e.permission_dialog_hold]
    tmux_manager.send_keys.assert_any_call(THREAD_ID, "Escape")


def test_the_quote_redacts_url_credentials() -> None:
    """LOW 7: a token in the dialog's command does not reach Discord."""
    from c_lord.claude.tmux_runner import _permission_dialog_summary

    pane = PERMISSION_PANE.replace(
        ' rm -rf ./junk; echo "exit=$?"',
        " git push https://bot:ghp_SECRET123@github.com/o/r.git",
    )
    summary = _permission_dialog_summary(_normalize_capture(pane))
    assert "github.com/o/r.git" in summary
    assert "ghp_SECRET123" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/compact", "/clear"])
async def test_slash_command_is_not_typed_into_a_permission_dialog(command: str) -> None:
    """HIGH 2: /compact and /clear with no turn running refuse instead of pressing Enter."""
    import discord

    from c_lord.cogs import claude_chat as cc

    bot = MagicMock()
    bot.channel_id = 999
    chat = cc.ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    tmux = MagicMock()
    tmux.is_claude_running.return_value = True
    tmux.capture_pane.return_value = PERMISSION_PANE
    tmux.pane_foreground_command.return_value = "claude"

    refusal = await chat._deliver_slash_command(thread, tmux, command)

    assert refusal is not None and "許可ダイアログ" in refusal and command in refusal
    tmux.send_literal.assert_not_called()
    tmux.send_keys.assert_not_called()


@pytest.mark.asyncio
async def test_slash_command_goes_out_at_an_idle_prompt() -> None:
    """HIGH 2 guard: with no dialog, /compact is typed as before."""
    import discord

    from c_lord.cogs import claude_chat as cc

    bot = MagicMock()
    bot.channel_id = 999
    chat = cc.ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    tmux = MagicMock()
    tmux.is_claude_running.return_value = True
    tmux.capture_pane.return_value = DONE_PANE
    tmux.send_literal.return_value = True

    assert await chat._deliver_slash_command(thread, tmux, "/compact") is None
    tmux.send_literal.assert_called_once_with(THREAD_ID, "/compact")
