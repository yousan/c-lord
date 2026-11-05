"""#1247 (review 16 of PR #1277): dialog shapes the pre-send hold must see.

W57 took real captures (Claude Code 2.1.318, isolated tmux, manual permission
mode, c-lord's own ``capture-pane -e -p -J -S -500``, every dialog closed with
Esc) of three shapes the first fix missed, and each one had the request typed
into the dialog, its Enter picking "1. Yes":

- a Write over an existing file: "Do you want to overwrite notes.txt?" — the
  verb is in no marker;
- an Edit at 34–70 columns: the question wraps before the file name and the
  capture drops the trailing space the marker needed;
- a Bash dialog at 30 columns: the wrapped options push "Do you want to
  proceed?" above the bottom 15 rows the markers are searched in.

Plus the delivery paths around the hold, including an AskUserQuestion menu that
opens while the request is held: it must go the #358 way (Esc, then send), not
wait out the deadline and end with "the permission dialog stayed open".

The dialog text, from the binary (strings in 2.1.318):
``["Do you want to ", verbPhrase, " ", <bold fileName>, "?"]`` — Write:
"overwrite" / "write to" / "create"; Edit: "make this edit to"; NotebookEdit:
"insert this cell into" / "delete this cell from".
"""

# ruff: noqa: F811 — the imported ``tmux_manager`` fixture is used by name as a parameter

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner, pane_shows_permission_dialog
from c_lord.claude.types import MessageType
from tests.test_permission_dialog_hold import (  # noqa: F401 — fixture re-export
    ANSWERED_PANE,
    DONE_PANE,
    PERMISSION_PANE,
    THREAD_ID,
    _fast,
    _keys_pressed,
    _runner,
    _scripted_run,
    tmux_manager,
)

PANES = Path(__file__).parent / "fixtures" / "panes"


def _pane(name: str) -> str:
    return (PANES / f"i1247_w57_{name}_v2_1_318.txt").read_text(encoding="utf-8")


ASK_PANE = (PANES / "i751_ask_menu_open_v2_1_280.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        # "Overwrite file / Do you want to overwrite notes.txt?" — Write on an
        # existing file, 160 columns. The verb "overwrite" is in no marker.
        "overwrite_160",
        # Edit dialog, question wrapped at the space before the file name:
        # "Do you want to make this edit to" (no trailing space after capture).
        "edit_35",
        "edit_50",
        "edit_60",
        # Bash dialog at 30 columns: the wrapped "always allow access to <path>"
        # option pushes "Do you want to proceed?" above the 15-row zone.
        "bash_multiline_30",
    ],
)
def test_real_dialog_is_seen_by_the_pre_send_hold(name: str) -> None:
    assert pane_shows_permission_dialog(_pane(name)) is True


@pytest.mark.parametrize("name", ["edit_160", "bash_multiline_36"])
def test_control_same_dialogs_that_are_seen(name: str) -> None:
    """Control: the same sessions at widths where the PR's check works."""
    assert pane_shows_permission_dialog(_pane(name)) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["overwrite_160", "edit_50"])
async def test_request_is_not_typed_into_a_real_overwrite_or_wrapped_edit_dialog(
    name: str,
) -> None:
    mgr = MagicMock()
    mgr.is_claude_running.return_value = True
    mgr.start_claude.return_value = True
    mgr.send_input.return_value = True
    mgr.send_interrupt.return_value = True
    mgr.duplicate_window_names.return_value = []
    mgr.session_name = "clord"
    mgr._find_window_for_thread.return_value = "w65"
    dialog = _pane(name)
    mgr.capture_pane.side_effect = lambda *_a, **_k: DONE_PANE if mgr.send_input.called else dialog
    runner = TmuxClaudeRunner(tmux_manager=mgr, thread_id=1, model="opus", timeout_seconds=5)
    with (
        patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.02),
        patch("c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT", 0.05),
        patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
        patch("c_lord.claude.tmux_runner._MENU_NAV_DELAY", 0.0),
        patch("c_lord.claude.tmux_runner._PERMISSION_HOLD_POLL", 0.01, create=True),
        patch("c_lord.claude.tmux_runner._PERMISSION_HOLD_TIMEOUT", 0.3, create=True),
        patch("c_lord.tmux._run"),
    ):
        async for _event in runner.run("<text>"):
            pass
    assert not mgr.send_input.called, (
        "the request was typed into an open file-write dialog; its Enter picks "
        "'1. Yes' and the file is written without a human"
    )


@pytest.mark.asyncio
async def test_sent_exactly_once_after_answer(tmux_manager) -> None:
    script = [PERMISSION_PANE] * 4 + [ANSWERED_PANE]
    events, typed_into = await _scripted_run(tmux_manager, script)
    assert tmux_manager.send_input.call_count == 1
    assert typed_into == [ANSWERED_PANE]
    assert len([e for e in events if e.permission_dialog_hold]) == 1


@pytest.mark.asyncio
async def test_same_dialog_redrawn_many_times_notifies_once(tmux_manager) -> None:
    script = [PERMISSION_PANE] * 30 + [ANSWERED_PANE]
    events, _ = await _scripted_run(tmux_manager, script)
    assert len([e for e in events if e.permission_dialog_hold]) == 1


@pytest.mark.asyncio
async def test_same_dialog_back_after_redraw_gap_notifies_once(tmux_manager) -> None:
    # dialog -> empty capture (ignored) -> dialog again
    script = [PERMISSION_PANE] * 3 + [""] * 3 + [PERMISSION_PANE] * 3 + [ANSWERED_PANE]
    events, typed_into = await _scripted_run(tmux_manager, script)
    assert typed_into == [ANSWERED_PANE]
    assert len([e for e in events if e.permission_dialog_hold]) == 1


@pytest.mark.asyncio
async def test_ask_menu_opening_during_hold(tmux_manager) -> None:
    """Dialog answered, then an AskUserQuestion menu is up: #358 (Esc, send), no wrong error."""
    script = [PERMISSION_PANE] * 3 + [ASK_PANE]
    # The one key allowed is #358's Esc to the ask menu — never to the dialog.
    events, typed_into = await _scripted_run(
        tmux_manager, script, allowed_keys=("send_keys('Escape',)",)
    )
    final = [e for e in events if e.message_type == MessageType.RESULT]
    pane_asks = [e for e in events if getattr(e, "pane_ask", None)]
    assert not pane_asks
    assert not (final and final[-1].error and "許可ダイアログ" in final[-1].error)
    # Expect the menu to go the #358 way (Esc then send), like before a send.
    assert typed_into, "request never sent: the hold kept waiting on an ask menu"


@pytest.mark.asyncio
async def test_stop_while_held_then_no_send(tmux_manager) -> None:
    runner = _runner(tmux_manager)
    tmux_manager.capture_pane.return_value = PERMISSION_PANE
    events = []
    with _fast(), patch("c_lord.tmux._run") as raw_tmux:
        async for event in runner.run("<text>"):
            events.append(event)
            if getattr(event, "permission_dialog_hold", None):
                assert await runner.interrupt(silent=False) is True
    assert not tmux_manager.send_input.called
    assert not tmux_manager.send_interrupt.called
    assert _keys_pressed(tmux_manager, raw_tmux) == []
    final = [e for e in events if e.message_type == MessageType.RESULT]
    assert len(final) == 1 and final[-1].error  # STOPPED_BY_USER


# --- More real captures (Claude Code 2.1.318, isolated ``tmux -L``, default
# permission mode, each raised by a background agent and closed with Esc) ----

MORE_DIALOGS = {
    # No reason row at all above "Do you want to proceed?".
    "i1247_bash_rm_outside_project_v2_1_318.txt": "junk2",
    # A different reason row: "rm path argument is runtime-determined".
    "i1247_bash_runtime_path_reason_v2_1_318.txt": "runtime-determined",
    # Read outside the project: "Read file" / "2. Yes, allow reading from …".
    "i1247_read_outside_project_v2_1_318.txt": "Read file",
    # NotebookEdit: "Do you want to insert this cell into nb.ipynb?".
    "i1247_notebook_insert_v2_1_318.txt": "Edit notebook",
}


@pytest.mark.parametrize("name", sorted(MORE_DIALOGS))
def test_more_real_dialogs_are_held_and_quoted(name: str) -> None:
    from c_lord.claude.tmux_runner import _normalize_capture, _permission_dialog_summary

    pane = (PANES / name).read_text(encoding="utf-8")
    assert pane_shows_permission_dialog(pane) is True
    summary = _permission_dialog_summary(_normalize_capture(pane))
    assert MORE_DIALOGS[name] in summary
    assert "Tip:" not in summary and "auto mode" not in summary


@pytest.mark.parametrize("name", ["w57_bash_multiline_30", "w57_bash_multiline_36"])
def test_a_wrapped_tip_does_not_leak_into_the_quote(name: str) -> None:
    from c_lord.claude.tmux_runner import _normalize_capture, _permission_dialog_summary

    pane = (PANES / f"i1247_{name}_v2_1_318.txt").read_text(encoding="utf-8")
    summary = _permission_dialog_summary(_normalize_capture(pane))
    assert "auto mode" not in summary and "rm -rf ./junk" in summary


def _is_real_dialog_fixture(name: str) -> bool:
    return name.startswith("i1247_") and "idle_after" not in name


@pytest.mark.parametrize("path", sorted(PANES.glob("*.txt")), ids=lambda p: p.name)
def test_only_real_permission_dialogs_hold(path: Path) -> None:
    """Every real capture: dialogs hold; ask / plan / trust / bypass / login / y/N /
    idle panes (including the ones right after /compact and /clear) do not."""
    held = pane_shows_permission_dialog(path.read_text(encoding="utf-8"))
    assert held is _is_real_dialog_fixture(path.name)


def test_timeout_after_the_dialog_closed_does_not_blame_the_dialog() -> None:
    """Low: the dialog was answered but the input box never came back."""
    from c_lord.claude.tmux_runner import _permission_hold_timed_out

    assert "送られていません" in _permission_hold_timed_out(dialog_open=False)
    assert "開いたままでした" not in _permission_hold_timed_out(dialog_open=False)
    assert "開いたままでした" in _permission_hold_timed_out(dialog_open=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["overwrite_160", "edit_50", "bash_multiline_30"])
async def test_slash_command_refuses_on_the_new_shapes(name: str) -> None:
    """The /compact · /clear guard uses the same detector."""
    import discord

    from c_lord.cogs import claude_chat as cc

    bot = MagicMock()
    bot.channel_id = 999
    chat = cc.ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
    thread = MagicMock(spec=discord.Thread)
    thread.id = 1
    tmux = MagicMock()
    tmux.is_claude_running.return_value = True
    tmux.capture_pane.return_value = _pane(name)
    refusal = await chat._deliver_slash_command(thread, tmux, "/compact")
    assert refusal is not None and "許可ダイアログ" in refusal
    tmux.send_literal.assert_not_called()
    tmux.send_keys.assert_not_called()


# --- Review of efd4350 --------------------------------------------------------
# Variants of the real NotebookEdit dialog (``i1247_notebook_insert_…``), edited on
# its normalised text the way a narrower pane or a status line draws it. Its
# question ("Do you want to insert this cell into nb.ipynb?") matches no marker, so
# only the shape detector can see these.

REAL_FOOTER = "Esc to cancel · Tab to amend · ctrl+x ctrl+k twice to stop background agents"


def _real_dialog_lines() -> list[str]:
    from c_lord.claude.tmux_runner import _normalize_capture

    raw = (PANES / "i1247_notebook_insert_v2_1_318.txt").read_text(encoding="utf-8")
    lines = _normalize_capture(raw).rstrip().splitlines()
    assert lines[-1].strip() == REAL_FOOTER
    return [line.rstrip() for line in lines]


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return [f" {row}" for row in textwrap.wrap(text, width, break_long_words=True)]


def test_footer_wrapped_over_four_rows_still_holds() -> None:
    """LOW 1: below ~26 columns the footer itself wraps."""
    rows = _wrap(REAL_FOOTER, 22)
    assert len(rows) >= 4
    pane = "\n".join(_real_dialog_lines()[:-1] + rows)
    assert pane_shows_permission_dialog(pane) is True


def test_status_rows_under_the_dialog_still_hold() -> None:
    """LOW 1: three status rows drawn under the footer."""
    status = [
        "  Model: Opus 5.5  v2.1.318  Ctx: 46.1k",
        "  Cost: $0.33  5h: 0.0%  Weekly: 78.0%",
        "  ⏸ manual mode on · /tasks to see subagents",
    ]
    pane = "\n".join(_real_dialog_lines() + status)
    assert pane_shows_permission_dialog(pane) is True


def test_question_wrapped_over_seven_rows_still_holds() -> None:
    """LOW 2: a ~150-char absolute path in the question at ~26 columns."""
    path = "/home/someone/projects/" + "a_very_long_directory_name/" * 4 + "file_name.txt"
    assert len(path) >= 140
    question = _wrap(f"Do you want to insert this cell into {path}?", 24)
    assert len(question) >= 7
    lines = _real_dialog_lines()
    q = next(i for i, line in enumerate(lines) if "Do you want to insert" in line)
    pane = "\n".join(lines[:q] + question + lines[q + 1 :])
    assert pane_shows_permission_dialog(pane) is True


def test_footer_with_esc_after_a_separator_holds() -> None:
    """LOW 4: "Tab to amend · Esc to cancel" as the last row block."""
    pane = "\n".join(_real_dialog_lines()[:-1] + [" Tab to amend · Esc to cancel"])
    assert pane_shows_permission_dialog(pane) is True


def test_an_input_box_under_the_footer_is_not_a_dialog() -> None:
    """LOW 5: a dialog's footer is never above the live input box."""
    rule = "─" * 80
    pane = "\n".join(_real_dialog_lines() + [rule, "❯ ", rule, "  ? for shortcuts"])
    assert pane_shows_permission_dialog(pane) is False


@pytest.mark.asyncio
async def test_dialog_behind_an_ask_menu_is_not_typed_into(tmux_manager) -> None:
    """MEDIUM: dialog → (answered) → ask menu → #358's Esc → the next dialog: hold again."""
    state = {"n": 0, "esc": False, "after_esc": 0}
    typed_into: list[str] = []

    def pane_now() -> str:
        if typed_into:
            return DONE_PANE
        if state["esc"]:
            state["after_esc"] += 1
            return PERMISSION_PANE if state["after_esc"] <= 3 else ANSWERED_PANE
        state["n"] += 1
        return PERMISSION_PANE if state["n"] <= 3 else ASK_PANE

    shown = {"pane": ""}

    def capture(*_a: object, **_k: object) -> str:
        shown["pane"] = pane_now()
        return shown["pane"]

    def send_keys(_tid: int, key: str) -> bool:
        assert shown["pane"] is ASK_PANE, f"{key} pressed while the pane was not the ask menu"
        state["esc"] = True
        return True

    def send_input(_tid: int, _prompt: str, **_kw: object) -> bool:
        typed_into.append(shown["pane"])
        return True

    tmux_manager.capture_pane.side_effect = capture
    tmux_manager.send_keys.side_effect = send_keys
    tmux_manager.send_input.side_effect = send_input
    runner = _runner(tmux_manager)
    events = []
    with _fast(), patch("c_lord.tmux._run"):
        async for event in runner.run("<text>"):
            events.append(event)

    assert typed_into == [ANSWERED_PANE], "the request was typed into the dialog behind the menu"
    tmux_manager.send_keys.assert_called_once_with(THREAD_ID, "Escape")
