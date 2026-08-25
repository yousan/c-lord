"""A thread's first message must not be posted back to the thread (#530).

The jsonl mirror decides "did c-lord type this, or did a human type it into the
pane?" by looking for a zero-width-space marker on the ``user`` event. Only
``send_input`` was adding it. ``start_claude`` — the cold-start path, used for
a thread's first message and after a pane dies — passes the prompt as a CLI
argument and added nothing, so the mirror read c-lord's own prompt as pane
input and echoed the whole thing back.

Small and merely noisy for a one-line message; with #527 letting large input
through, a 40KB attachment came back as ~12 messages and buried the answer.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager
from c_lord.transcript.formatter import ZWSP_MARKER, render_event


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]
    return mgr


def _typed_command(calls: list[list[str]]) -> str:
    return "".join(c[-1] for c in calls if "send-keys" in c and "-l" in c)


def _start(prompt: str, *, jsonl: bool) -> str:
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="")

    with (
        patch("c_lord.tmux._run", side_effect=fake_run),
        patch("c_lord.transcript.mirror.bridge_mode_jsonl", return_value=jsonl),
    ):
        assert _mgr().start_claude(12345, prompt, "sonnet") is True
    return _typed_command(calls)


def _user_event(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


# ── the marker reaches the cold-start prompt ────────────────────────


def test_cold_start_prompt_is_marked_as_clord_originated() -> None:
    cmd = _start("最初のメッセージ", jsonl=True)
    assert f"{ZWSP_MARKER}最初のメッセージ" in cmd, (
        "start_claude must mark its prompt the way send_input does (#530)"
    )


def test_cold_start_prompt_is_unmarked_when_the_jsonl_bridge_is_off() -> None:
    """Under skill mode there is no mirror to fool — do not touch the prompt."""
    cmd = _start("最初のメッセージ", jsonl=False)
    assert ZWSP_MARKER not in cmd


def test_the_marker_sits_inside_the_quoted_prompt_not_on_the_command() -> None:
    """It must ride on the prompt argument, never on the `claude` command."""
    cmd = _start("hello", jsonl=True)
    assert not cmd.startswith(ZWSP_MARKER)
    assert cmd.startswith("unalias claude")
    assert f"'{ZWSP_MARKER}hello'" in cmd


# ── and the mirror then keeps quiet about it ────────────────────────


def test_a_marked_prompt_is_not_mirrored_back() -> None:
    assert render_event(_user_event(f"{ZWSP_MARKER}最初のメッセージ")) is None


def test_input_typed_by_a_human_in_the_pane_is_still_mirrored() -> None:
    rendered = render_event(_user_event("人がペインで直接打った入力"))
    assert rendered is not None
    assert rendered.kind == "user_input"
