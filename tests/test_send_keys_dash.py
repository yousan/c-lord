"""Text that starts with ``-`` must reach Claude as text (#809).

``_type_literal`` ran ``tmux send-keys -l -t <target> <chunk>`` with no ``--``
before the chunk, so tmux parsed a chunk that began with ``-`` as options:

* ``- サーバリスト…`` → ``command send-keys: invalid flag -`` (a bulleted answer
  to an AskUserQuestion menu, 2026-09-24 21:50);
* the 2nd 3,000-byte chunk of a long message starting with ``-`` → same error
  (a 3,712-byte order, 2026-09-24 03:43 — the PM withdrew it);
* ``-R`` → accepted (exit 0) and reset the terminal: *nothing* was typed.

And the user was told something else entirely — "the tmux window was not found"
or "the pane is dead, run /claude-restart" — so they rebuilt healthy sessions
or withdrew the request.  These tests pin both halves: the text arrives, and
when tmux does refuse, the refusal is what gets reported.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

import c_lord.tmux as tmux_mod
from c_lord.claude.tmux_runner import TmuxClaudeRunner
from c_lord.tmux import _SEND_KEYS_CHUNK_BYTES, TmuxSessionManager

_DASH_TEXTS = [
    "- サーバリストに乗っているIPがplayit.gg なら問題がない？",
    "-R",
    "-N 3 x",
    "--help",
]


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]
    return mgr


def _long_with_dash_chunk() -> str:
    """A payload whose 2nd send-keys chunk begins with ``-`` (the 03:43 case)."""
    head = "​" + "あ" * ((_SEND_KEYS_CHUNK_BYTES - 3) // 3)
    assert len(head.encode("utf-8")) == _SEND_KEYS_CHUNK_BYTES
    return head + "- 箇条書きの2行目"


# ── argv shape ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text", [*_DASH_TEXTS, _long_with_dash_chunk()], ids=[*_DASH_TEXTS, "long-2nd-chunk"]
)
def test_every_literal_chunk_is_preceded_by_double_dash(text: str) -> None:
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(tmux_mod, "_run", side_effect=fake_run):
        assert _mgr()._type_literal("t:w1", text, what="test") is True

    literal = [c for c in calls if "send-keys" in c and "-l" in c]
    assert literal, "expected at least one send-keys -l"
    for call in literal:
        assert call[-2] == "--", f"the text must follow `--`, got {call!r}"
    assert "".join(c[-1] for c in literal) == text


def test_send_keys_puts_double_dash_before_key_names() -> None:
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(tmux_mod, "_run", side_effect=fake_run):
        assert _mgr().send_keys(1, "Down", "Enter") is True
    assert calls[-1][-3:] == ["--", "Down", "Enter"]


def test_claude_prompt_argument_follows_double_dash() -> None:
    """CLAUDE.md Security: ``--`` before the prompt argument of ``claude``."""
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    mgr = _mgr()
    with (
        patch.object(tmux_mod, "_run", side_effect=fake_run),
        patch.object(mgr, "_pane_path", return_value=None),
    ):
        assert mgr.start_claude(1, "-x hello") is True
    typed = "".join(c[-1] for c in calls if "send-keys" in c and "-l" in c)
    assert ' -- "$CLORD_PROMPT"' in typed or " -- '" in typed, typed


# ── failure reason is kept ──────────────────────────────────────────


def test_rejected_literal_records_tmux_error_for_the_thread() -> None:
    def fake_run(args):
        if "send-keys" in args and "-l" in args:
            return MagicMock(returncode=1, stdout="", stderr="command send-keys: boom\n")
        return MagicMock(returncode=0, stdout="", stderr="")

    mgr = _mgr()
    with patch.object(tmux_mod, "_run", side_effect=fake_run):
        assert mgr.send_literal(77, "text") is False
    assert mgr.take_send_failure(77) == "command send-keys: boom"
    assert mgr.take_send_failure(77) is None, "a reason is reported once"


def test_missing_window_records_no_tmux_error() -> None:
    mgr = _mgr()
    mgr._find_window_for_thread = lambda _tid: None  # type: ignore[method-assign]
    assert mgr.send_literal(77, "text") is False
    assert mgr.take_send_failure(77) is None


# ── runner / Discord wording ────────────────────────────────────────


def _runner_with_rejected_literal(
    err: str | None, *, window: bool = True
) -> tuple[TmuxClaudeRunner, MagicMock]:
    tmux = MagicMock()
    tmux.send_keys.return_value = True
    tmux.send_literal.return_value = False
    # The runner clears a stale reason first, then reads this send's reason.
    tmux.take_send_failure.side_effect = [None, err]
    tmux.session_exists.return_value = window
    return TmuxClaudeRunner(tmux_manager=tmux, thread_id=4242), tmux


@pytest.mark.asyncio
async def test_rejected_answer_is_not_blamed_on_a_missing_window(caplog) -> None:
    runner, _ = _runner_with_rejected_literal("command send-keys: invalid flag -")
    with patch("c_lord.claude.tmux_runner.asyncio.sleep"):
        assert await runner.answer_menu_text(2, "- あ") is False
    reason = runner.undelivered_reason
    assert "invalid flag -" in reason
    assert "見つかりません" not in reason
    assert "no tmux window" not in caplog.text


@pytest.mark.asyncio
async def test_rejected_literal_does_not_press_enter_on_the_menu() -> None:
    """Enter on the untouched "Type something." row records a refusal."""
    runner, tmux = _runner_with_rejected_literal("command send-keys: invalid flag -")
    with patch("c_lord.claude.tmux_runner.asyncio.sleep"):
        await runner.answer_menu_text(2, "- あ")
    keys = [c.args[1] for c in tmux.send_keys.call_args_list]
    assert "Enter" not in keys


@pytest.mark.asyncio
async def test_missing_window_still_says_so() -> None:
    runner, _ = _runner_with_rejected_literal(None, window=False)
    with patch("c_lord.claude.tmux_runner.asyncio.sleep"):
        assert await runner.answer_menu_text(2, "x") is False
    assert "見つかりません" in runner.undelivered_reason


def test_undeliverable_notice_uses_the_given_reason() -> None:
    from c_lord.discord_ui.ask_handler import _answer_undeliverable_notice

    text = _answer_undeliverable_notice(["- あ"], "tmux が入力を拒否しました")
    assert "tmux が入力を拒否しました" in text
    assert "見つかりませんでした" not in text


@pytest.mark.asyncio
async def test_send_input_rejection_names_tmux_not_a_dead_pane() -> None:
    from c_lord.claude.types import MessageType

    tmux = MagicMock()
    tmux.server_fingerprint.return_value = "fp"
    tmux.is_claude_running.return_value = True
    tmux.send_input.return_value = False
    tmux.input_box_holds.return_value = False
    tmux.take_send_failure.side_effect = [None, "command send-keys: invalid flag -"]
    runner = TmuxClaudeRunner(tmux_manager=tmux, thread_id=4242)
    with (
        patch.object(runner, "peek_pending_ask", return_value=None),
        patch.object(runner, "_duplicate_window_names", return_value=[]),
        patch.object(runner, "_fleet_tmux_error", return_value=None),
    ):
        events = [e async for e in runner.run("x")]
    errors = [e.error for e in events if e.message_type == MessageType.RESULT and e.error]
    assert errors, events
    assert "invalid flag -" in errors[0]
    assert "ペインが落ちているか応答しない" not in errors[0]


# ── the real thing (isolated tmux server, #701) ─────────────────────


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_real_tmux_types_dash_leading_text_verbatim() -> None:
    """AC1: against a live tmux on its own socket — never the fleet's server."""
    sock = f"clord-809-{uuid.uuid4().hex[:8]}"
    base = ["tmux", "-L", sock, "-f", "/dev/null"]
    subprocess.run(
        [*base, "new-session", "-d", "-s", "t", "-x", "400", "-y", "80", "cat"], check=True
    )
    orig = tmux_mod._run

    def isolated(cmd, *a, **k):
        if cmd and cmd[0] == "tmux":
            cmd = [*base, *cmd[1:]]
        return orig(cmd, *a, **k)

    try:
        mgr = _mgr()
        texts = [*_DASH_TEXTS, _long_with_dash_chunk()]
        with patch.object(tmux_mod, "_run", side_effect=isolated):
            for text in texts:
                assert mgr._type_literal("t:0", text, what="test") is True, text
                subprocess.run([*base, "send-keys", "-t", "t:0", "Enter"], check=True)
        time.sleep(0.5)
        pane = subprocess.run(
            [*base, "capture-pane", "-p", "-J", "-S", "-", "-t", "t:0"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for text in texts:
            assert text.replace("​", "") in pane.replace("​", ""), (text, pane)
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True)
