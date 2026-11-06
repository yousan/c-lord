"""#1247 (review 17 of PR #1277): what the shape check still got wrong.

W57's real captures (Claude Code 2.1.318, isolated ``tmux -L``, background
agents, resized while open, c-lord's own ``capture-pane -e -p -J -S -500``,
every dialog closed with Esc):

- FALSE HOLD: a stopped request that Claude Code put back into the input box
  (#879) and that quotes "Do you want to proceed?" read as a dialog — the
  next request was held for 30 minutes behind nothing. A real dialog replaces
  the input box; it never sits inside or above a live one.
- MISSED: the WebFetch dialog has no "Esc to cancel" footer (its last option
  ends "(esc)") and its question wraps at <= 50 columns; in a 10-row pane the
  Write-overwrite and NotebookEdit dialogs are clipped from the top, so the
  question is not on screen at all — only "❯ 1. Yes", options and the footer.
- /compact or /clear while a request was held: the request was dropped
  without a word and the reply blamed a turn that "did not stop".
"""

# ruff: noqa: F811 — the imported ``tmux_manager`` fixture is used by name as a parameter

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import (
    TmuxClaudeRunner,
    _normalize_capture,
    _permission_dialog_open,
    pane_shows_permission_dialog,
)
from tests.test_permission_dialog_hold import (  # noqa: F401 — fixture re-export
    PERMISSION_PANE,
    THREAD_ID,
    _runner,
    _scripted_run,
    tmux_manager,
)

PANES = Path(__file__).parent / "fixtures" / "panes"


def _pane(name: str) -> str:
    return (PANES / f"i1247_w57r17_{name}_v2_1_318.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        "overwrite_w25_h10",  # Write overwrite dialog, question clipped off the top
        "notebook_w25_h10",  # NotebookEdit insert dialog, question clipped off the top
        "fetch_w50_h24",  # WebFetch, no footer, question wrapped
        "fetch_w40_h24",
        "fetch_w30_h15",
        "fetch_w25_h10",
    ],
)
def test_real_dialog_is_seen_by_the_pre_send_hold(name: str) -> None:
    assert pane_shows_permission_dialog(_pane(name)) is True


@pytest.mark.parametrize("name", ["overwrite_w30_h10", "fetch_w80_h24", "bash_rt_w25_h10"])
def test_control_same_dialogs_that_are_seen(name: str) -> None:
    """Control: the same dialogs at sizes where the PR's check works."""
    assert pane_shows_permission_dialog(_pane(name)) is True


DONE_PANE = """\
● done

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["overwrite_w25_h10", "fetch_w50_h24"])
async def test_request_is_not_typed_into_the_dialog(name: str) -> None:
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
        "the request was typed into an open permission dialog; its Enter picks "
        "'1. Yes' without a human"
    )


_RESTORED = (PANES / "interrupt_restored_prompt.txt").read_text(encoding="utf-8")
_ORIG_ROW = "Bash で sleep 20 を実行してから ok と返して"
assert _RESTORED.count(_ORIG_ROW) == 2  # the sent copy (scrollback) and the box


def _restored_with(text: str) -> str:
    """The restored-prompt pane whose box (and sent copy) carries *text* instead."""
    return _RESTORED.replace(_ORIG_ROW, text)


QUOTES = {
    "proceed": "「Do you want to proceed?」が出たら教えて",
    "approval": "This command requires approval と出たら止めて",
    "edit": "Do you want to make this edit to README.md? が出たら No で",
    "continue": "失敗したら Continue anyway? と聞いて",
}


def test_marker_on_the_first_box_row_too() -> None:
    """Single-line case: the marker on the ``❯`` row itself."""
    pane = _RESTORED.replace("> 一通目の引用です。", "Do you want to proceed? の意味は？")
    assert not pane_shows_permission_dialog(pane)


@pytest.mark.parametrize("key", list(QUOTES))
def test_restored_prompt_quoting_a_dialog_is_not_a_dialog(key: str) -> None:
    pane = _restored_with(QUOTES[key])
    assert not pane_shows_permission_dialog(pane)
    assert not _permission_dialog_open(_normalize_capture(pane))


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["proceed", "edit"])
async def test_next_request_after_stop_is_sent_not_held(tmux_manager, key: str) -> None:
    """The box holds the stopped prompt; nothing else changes. The request must go."""
    events, typed_into = await _scripted_run(tmux_manager, [_restored_with(QUOTES[key])])
    holds = [e for e in events if getattr(e, "permission_dialog_hold", None)]
    final = [e for e in events if e.message_type.name == "RESULT"]
    assert not holds, f"held behind no dialog: {holds[0].permission_dialog_hold!r}"
    assert typed_into, f"never sent; turn ended with {final[-1].error if final else None!r}"


_IDLE = (PANES / "i1247_idle_after_compact_v2_1_318.txt").read_text(encoding="utf-8")
_PROSE = (
    "⏺ 背景のエージェントがこう聞いてきます:\n"
    "\n"
    "  Do you want to proceed?\n"
    "  1. Yes\n"
    "  2. No\n"
    "  Esc to cancel · Tab to amend\n"
    "\n"
)


def test_answer_shaped_like_a_dialog_above_idle_box_is_not_a_dialog() -> None:
    marker = "Compacted (ctrl+o to see full summary)\x1b[0m"
    assert marker in _IDLE
    pane = _IDLE.replace(marker, marker + "\n\n" + _PROSE.rstrip("\n"))
    assert not pane_shows_permission_dialog(pane)


# A REAL capture (Claude Code 2.1.318, isolated ``tmux -L review17-1277b``): the
# request 「Do you want to proceed?」と出たら、どう答えればいいか説明して was sent and
# Ctrl+C'd 0.6s later; Claude Code put it back into the input box (#879).
REAL_RESTORED = (PANES / "i1247_nodialog_restored_prompt_quoting_dialog_v2_1_318.txt").read_text(
    encoding="utf-8"
)


def test_real_restored_prompt_is_not_a_dialog() -> None:
    assert not pane_shows_permission_dialog(REAL_RESTORED)


@pytest.mark.asyncio
async def test_real_restored_prompt_does_not_hold_the_next_request(tmux_manager) -> None:
    events, typed_into = await _scripted_run(tmux_manager, [REAL_RESTORED])
    holds = [e for e in events if getattr(e, "permission_dialog_hold", None)]
    assert not holds, f"held behind no dialog: {holds[0].permission_dialog_hold!r}"
    assert typed_into


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/compact", "/clear"])
async def test_slash_during_hold_names_the_dialog_and_keeps_the_request(command: str) -> None:
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
    runner = _runner(tmux)
    runner._held_for_permission = True  # what _hold_for_permission_dialog sets
    chat._active_runners[THREAD_ID] = runner

    with (
        patch.object(cc, "_SLASH_IDLE_TIMEOUT", 0.05),
        patch.object(cc, "_SLASH_POLL_INTERVAL", 0.01),
    ):
        reply = await chat._deliver_slash_command(thread, tmux, command)

    tmux.send_literal.assert_not_called()
    tmux.send_interrupt.assert_not_called()
    assert reply is not None and "許可ダイアログ" in reply, reply
    assert not runner._stopped, "the held request was dropped without a word"


# --- Suspicions checked on a real CLI (Claude Code 2.1.318, isolated
# ``tmux -L clord-fixture-1247c``) ---------------------------------------------
#
# (a) Two Esc presses on an idle prompt open Rewind: measured 0.3 s and 0.6 s
#     apart → Rewind; 0.8 s, 0.9 s, 1.0 s, 1.5 s apart → nothing. One Esc closes
#     it (its footer: "Enter to continue · Esc to cancel").

DONE_PANE = "● done\n\n" + "─" * 80 + "\n❯\n" + "─" * 80 + "\n  ? for shortcuts\n"
REWIND_PANE = (PANES / "i1247_nodialog_rewind_double_esc_0_6s_v2_1_318.txt").read_text(
    encoding="utf-8"
)
IDLE_PANE = (PANES / "i1247_idle_after_clear_v2_1_318.txt").read_text(encoding="utf-8")


def _esc_harness(tmux_manager: MagicMock, panes_before_esc: list[str], after_esc: str):
    """Pane shows ``panes_before_esc`` until an Esc, then ``after_esc``; records Esc."""
    state = {"n": 0, "esc_at": []}
    shown = {"pane": ""}

    def capture(*_a: object, **_k: object) -> str:
        if tmux_manager.send_input.called:
            shown["pane"] = DONE_PANE  # Claude took the request (keeps the poll loop short)
        elif state["esc_at"]:
            shown["pane"] = after_esc
        else:
            shown["pane"] = panes_before_esc[min(state["n"], len(panes_before_esc) - 1)]
            state["n"] += 1
        return shown["pane"]

    def send_keys(_tid: int, key: str) -> bool:
        import time

        state["esc_at"].append((time.monotonic(), key, shown["pane"]))
        return True

    tmux_manager.capture_pane.side_effect = capture
    tmux_manager.send_keys.side_effect = send_keys
    return state, shown


@pytest.mark.asyncio
async def test_rewind_open_before_a_send_is_closed_then_the_request_goes(tmux_manager) -> None:
    """(a) The Rewind screen is backed out of with ONE Esc, seen first; then the send."""
    from tests.test_permission_dialog_hold import _fast

    state, shown = _esc_harness(tmux_manager, [REWIND_PANE], IDLE_PANE)
    typed_into: list[str] = []
    tmux_manager.send_input.side_effect = lambda *_a, **_k: typed_into.append(shown["pane"]) or True
    with _fast(), patch("c_lord.tmux._run"):
        async for _event in _runner(tmux_manager).run("<text>"):
            pass
    assert [(k, p is REWIND_PANE) for _t, k, p in state["esc_at"]] == [("Escape", True)]
    assert typed_into == [IDLE_PANE], "the request was typed into the Rewind screen"


@pytest.mark.asyncio
async def test_rewind_that_will_not_close_is_never_typed_into(tmux_manager) -> None:
    from c_lord.claude.types import MessageType
    from tests.test_permission_dialog_hold import _fast

    state, _shown = _esc_harness(tmux_manager, [REWIND_PANE], REWIND_PANE)
    events = []
    with _fast(), patch("c_lord.tmux._run"):
        async for event in _runner(tmux_manager).run("<text>"):
            events.append(event)
    assert not tmux_manager.send_input.called
    assert all(p is REWIND_PANE for _t, _k, p in state["esc_at"])  # never Esc on anything else
    final = [e for e in events if e.message_type == MessageType.RESULT]
    assert final and final[-1].error and "送られていません" in final[-1].error
    assert "Rewind" in final[-1].error


@pytest.mark.asyncio
async def test_menu_esc_presses_are_spaced_past_the_double_esc_window(tmux_manager) -> None:
    """(a) A menu that stays up gets its Esc presses at least the gap apart."""
    from tests.test_permission_dialog_hold import _fast

    ask = (PANES / "i751_ask_menu_open_v2_1_280.txt").read_text(encoding="utf-8")
    state, _shown = _esc_harness(tmux_manager, [ask], ask)
    with (
        _fast(),
        patch("c_lord.claude.tmux_runner._PRE_SEND_ESC_GAP", 0.2, create=True),
        patch("c_lord.tmux._run"),
    ):
        async for _event in _runner(tmux_manager).run("<text>"):
            pass
    times = [t for t, _k, _p in state["esc_at"]]
    assert len(times) >= 2
    assert min(b - a for a, b in zip(times, times[1:], strict=False)) >= 0.2


def test_a_remapped_cancel_key_still_reads_as_the_footer() -> None:
    """(c) not reproduced on a real CLI (it needs a throwaway HOME with credentials),
    but the footer's key names come from the keybinding-aware hint component, so a
    remapped Esc would read e.g. "Ctrl+Q to cancel"."""
    raw = (PANES / "i1247_notebook_insert_v2_1_318.txt").read_text(encoding="utf-8")
    pane = _normalize_capture(raw).replace(
        " Esc to cancel · Tab to amend", " Ctrl+Q to cancel · Tab to amend"
    )
    assert "Ctrl+Q to cancel" in pane
    assert pane_shows_permission_dialog(pane) is True


@pytest.mark.parametrize(
    "name",
    [
        "i1247_mcp_tool_dialog_v2_1_318.txt",
        "i1247_mcp_tool_dialog_w30_h15_v2_1_318.txt",
        "i1247_mcp_tool_dialog_w25_h10_v2_1_318.txt",
    ],
)
def test_real_mcp_tool_dialogs_hold(name: str) -> None:
    """(b) An MCP tool's dialog keeps its "Esc to cancel" footer even at 25 columns."""
    assert pane_shows_permission_dialog((PANES / name).read_text(encoding="utf-8")) is True


# --- Review of 38ea0c9 --------------------------------------------------------

IDLE_PANE_TEXT = _normalize_capture(
    (PANES / "i742_idle_after_turn_v2_1_271.txt").read_text(encoding="utf-8")
)


def test_an_idle_pane_quoted_above_a_real_dialog_does_not_turn_the_hold_off() -> None:
    """MEDIUM: Claude's earlier output shows a captured idle pane (rule / ❯ / rule);
    the real dialog below it still has the keyboard."""
    from c_lord.claude.tmux_runner import _input_box_rows

    pane = IDLE_PANE_TEXT.rstrip() + "\n\n" + _normalize_capture(PERMISSION_PANE)
    assert _input_box_rows(pane.splitlines()), "the quoted pane must contain a framed box"
    assert pane_shows_permission_dialog(pane) is True


@pytest.mark.asyncio
async def test_request_is_held_behind_a_dialog_under_a_quoted_idle_pane(tmux_manager) -> None:
    pane = IDLE_PANE_TEXT.rstrip() + "\n\n" + _normalize_capture(PERMISSION_PANE)
    events, typed_into = await _scripted_run(tmux_manager, [pane] * 3 + [IDLE_PANE_TEXT])
    assert [e for e in events if getattr(e, "permission_dialog_hold", None)]
    assert typed_into == [IDLE_PANE_TEXT]


def _rewind_rows() -> list[str]:
    return _normalize_capture(REWIND_PANE).rstrip().splitlines()


def test_rewind_with_its_title_scrolled_off_is_still_rewind() -> None:
    """LOW 1: many rewind points push the title out of a short window, or it is clipped."""
    from c_lord.claude.tmux_runner import _rewind_open

    rows = _rewind_rows()
    title = next(i for i, r in enumerate(rows) if r.strip() == "Rewind")
    points = [f"     Point {n}: an earlier request" for n in range(30)]
    clipped = rows[:title] + rows[title + 2 : -2] + points + rows[-2:]  # title + subtitle gone
    assert not any(r.strip() == "Rewind" for r in clipped)
    assert _rewind_open("\n".join(clipped)) is True


@pytest.mark.asyncio
async def test_held_for_permission_stays_true_between_two_holds(tmux_manager) -> None:
    """LOW 4: a /compact in the gap between two dialogs must still see the hold."""
    from tests.test_permission_dialog_hold import ANSWERED_PANE, SECOND_DIALOG_PANE

    runner = _runner(tmux_manager)
    script = [PERMISSION_PANE] * 3 + [ANSWERED_PANE, SECOND_DIALOG_PANE, SECOND_DIALOG_PANE]
    script += [SECOND_DIALOG_PANE] * 2 + [ANSWERED_PANE]
    state = {"n": 0, "noticed": False}
    seen: list[bool] = []

    def capture(*_a: object, **_k: object) -> str:
        if tmux_manager.send_input.called:
            return DONE_PANE
        if state["noticed"]:
            seen.append(runner.held_for_permission)
        pane = script[min(state["n"], len(script) - 1)]
        state["n"] += 1
        return pane

    tmux_manager.capture_pane.side_effect = capture
    from tests.test_permission_dialog_hold import _fast

    with _fast(), patch("c_lord.tmux._run"):
        async for event in runner.run("<text>"):
            if getattr(event, "permission_dialog_hold", None):
                state["noticed"] = True
    assert tmux_manager.send_input.called
    assert seen and all(seen), f"held_for_permission dropped between holds: {seen}"
    assert runner.held_for_permission is False  # sent: nothing is held any more
