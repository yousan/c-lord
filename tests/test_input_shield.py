"""c-lord types a Discord message so that one Enter sends it, as text (#861).

Until #861 every message went in prefixed with a zero-width space (#71), the
"c-lord typed this" marker the transcript mirror once relied on. Claude Code
2.1.278+ treats an invisible character in the input box as suspicious: the first
Enter only removes it and shows ``Removed 1 invisible character · review and
press Enter to send``. Every message in production then depended on #560's
re-press — 112 of 112 sends from 9/25 to 10/2 went out on the 2nd Enter, and a
slow redraw turned a delivered message into "❌ 送信に失敗しました".

The marker had already stopped carrying its weight: the CLI strips it before
writing the transcript, so the mirror finds its own transcript by claim (#773)
and recognises its own echo by the copy in ``pane_echo`` (#808). Its other job,
keeping a message that starts with ``/`` from running as a slash command, was
gone too — measured on 2.1.283, ``\\u200b/cost`` + Enter + Enter runs ``/cost``.

So the marker is dropped, and that second job is done by something the CLI
does leave alone: a message whose first character switches the input box into
another mode (``/`` command, ``!`` shell, ...) gets one leading space, which the
CLI submits as ordinary text on the first Enter (measured: `` /cost`` and
`` !echo hi`` both go to Claude as text). A real command — a /skill — is typed
as it is.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner
from c_lord.tmux import TmuxSessionManager, shield_input
from tests.test_coldstart_echo import _mgr, _staged, _typed_command

_EMPTY_BOX_PANE = (
    "● done\n"
    "─────────────────────────────\n"
    "❯ \n"
    "─────────────────────────────\n"
    "   Model: Opus 4.7  v2.1.283  Style: default\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)


def _send(text: str, *, as_command: bool = False) -> tuple[str, int]:
    """What send_input typed, and how many Enters it pressed."""
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]
    mgr._ensure_insert_mode = lambda *_a, **_k: None  # type: ignore[method-assign]
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout=_EMPTY_BOX_PANE)

    with (
        patch("c_lord.tmux._run", side_effect=fake_run),
        patch("c_lord.tmux.time.sleep"),
    ):
        assert mgr.send_input(12345, text, as_command=as_command) is True
    enters = sum(1 for c in calls if "send-keys" in c and c[-1] == "Enter")
    return _typed_command(calls), enters


# ── shield_input: the one rule ──────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["hello", "おねがい！", "a/b", "  indented", "", "line1\n/line2", "@file"],
)
def test_ordinary_text_is_typed_unchanged(text: str) -> None:
    assert shield_input(text) == text


@pytest.mark.parametrize("text", ["/compact", "/cost now", "!rm -rf x", "#note", "&bg", "?"])
def test_a_mode_key_at_the_start_gets_one_leading_space(text: str) -> None:
    assert shield_input(text) == f" {text}"


def test_a_real_command_is_left_alone() -> None:
    assert shield_input("/copywriting hi", as_command=True) == "/copywriting hi"


def test_no_invisible_character_is_ever_added() -> None:
    for text in ("hello", "/x", "!x"):
        assert "​" not in shield_input(text)


# ── send_input ──────────────────────────────────────────────────────


def test_send_input_types_the_message_without_the_marker() -> None:
    typed, enters = _send("ok とだけ返して")
    assert typed == "ok とだけ返して"
    assert enters == 1


def test_send_input_shields_a_leading_slash() -> None:
    typed, _ = _send("/compact してほしい？")
    assert typed == " /compact してほしい？"


def test_send_input_types_a_real_command_as_is() -> None:
    typed, _ = _send("/copywriting hi", as_command=True)
    assert typed == "/copywriting hi"


def test_input_box_holds_matches_what_send_input_typed() -> None:
    """The delivery-failure path (#560) must recognise the shielded text."""
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]
    box = (
        "─────────────────────────────\n"
        "❯  /compact してほしい？\n"
        "─────────────────────────────\n"
        "   Model: Opus 4.7  v2.1.283  Style: default\n"
    )

    with patch("c_lord.tmux._run", return_value=MagicMock(returncode=0, stdout=box)):
        assert mgr.input_box_holds(12345, "/compact してほしい？") is True


# ── start_claude ────────────────────────────────────────────────────


def _start(prompt: str, *, as_command: bool = False) -> str:
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="")

    with patch("c_lord.tmux._run", side_effect=fake_run):
        assert _mgr().start_claude(12345, prompt, "sonnet", as_command=as_command) is True
    return _staged(_typed_command(calls))


def test_start_claude_hands_over_the_prompt_without_the_marker() -> None:
    assert _start("最初のメッセージ") == "最初のメッセージ"


def test_start_claude_shields_a_leading_slash() -> None:
    assert _start("/not a command") == " /not a command"


def test_start_claude_hands_a_real_command_over_as_is() -> None:
    assert _start("/copywriting hello", as_command=True) == "/copywriting hello"


# ── the runner says which it is ─────────────────────────────────────


@pytest.mark.parametrize("slash_command", [True, False])
async def test_runner_tells_send_input_whether_the_prompt_is_a_command(
    slash_command: bool,
) -> None:
    """A /skill sent to a thread whose Claude is already running (#762's warm path)."""
    tmux = MagicMock()
    tmux.is_claude_running = MagicMock(return_value=True)
    tmux.send_input = MagicMock(return_value=False)  # stop right after the call
    runner = TmuxClaudeRunner(tmux_manager=tmux, thread_id=1, slash_command=slash_command)
    runner.peek_pending_ask = AsyncMock(return_value=None)  # type: ignore[method-assign]
    runner._fleet_tmux_error = AsyncMock(return_value=None)  # type: ignore[method-assign]

    async for _ in runner.run("/copywriting hi"):
        pass

    assert tmux.send_input.call_args.kwargs["as_command"] is slash_command
