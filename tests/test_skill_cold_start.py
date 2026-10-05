"""A /skill that starts Claude must actually submit its slash command (#762).

``start_claude`` hands the prompt to ``claude`` as a CLI argument, prefixed with
the zero-width-space marker (#530). For an ordinary message that is harmless.
For a slash command it is not: Claude Code 2.1.283 puts ``\\u200b/skill …`` in
the input box and never submits it (measured on an isolated tmux server — the
same prompt without the marker runs; the marker on a plain sentence runs too).
So a /skill that opened a new thread started Claude, left ``/copywriting …``
sitting in the box, and the turn ended with nothing in the thread.

A /skill prompt is a command by construction, so it goes unmarked. Ordinary
messages keep the marker exactly as before.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner
from tests.test_coldstart_echo import _mgr, _staged, _typed_command


def _start(prompt: str, **kwargs: object) -> str:
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="")

    with patch("c_lord.tmux._run", side_effect=fake_run):
        assert _mgr().start_claude(12345, prompt, "sonnet", **kwargs) is True
    return _typed_command(calls)


def test_a_command_prompt_is_handed_over_unmarked() -> None:
    assert _staged(_start("/copywriting hello", as_command=True)) == "/copywriting hello"


def test_an_ordinary_prompt_starting_with_a_slash_stays_text() -> None:
    """#861: only an explicit command runs as one; the rest is shielded."""
    assert _staged(_start("/not a command")) == " /not a command"


@pytest.mark.parametrize("slash_command", [True, False])
async def test_runner_tells_start_claude_whether_the_prompt_is_a_command(
    slash_command: bool,
) -> None:
    tmux = MagicMock()
    tmux.is_claude_running = MagicMock(return_value=False)
    tmux.start_claude = MagicMock(return_value=False)  # stop right after the call
    runner = TmuxClaudeRunner(tmux_manager=tmux, thread_id=1, slash_command=slash_command)
    runner._fleet_tmux_error = AsyncMock(return_value=None)  # type: ignore[method-assign]
    runner._start_failure_reason = AsyncMock(return_value="x")  # type: ignore[method-assign]

    async for _ in runner.run("/copywriting hi"):
        pass

    assert tmux.start_claude.call_args.kwargs["as_command"] is slash_command


def test_skill_runner_marks_its_prompt_as_a_command() -> None:
    from tests.test_skill_command_window import _make_cog

    cog, tmux, _ = _make_cog()
    runner = cog._make_runner(tmux, 777, "/sessions/999/777")
    assert runner._slash_command is True
