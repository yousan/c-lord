"""A resume that is slow to start is not a resume that failed (#994).

Production, thread 1564444583767703562, 2026-10-29 09:29 JST: a slept workspace
was restored into a brand-new window. c-lord typed ``claude --resume a07fc39e…``
at 09:29:11; at 09:29:14 — the 3s grace — the pane still ran zsh, so the verdict
read "--continue found no session" and the fallback wrote a new claim
(93b54394) and typed a fresh start. The resume claude exec'd at 09:29:15 and
went on writing ``a07fc39e.jsonl``; ``93b54394.jsonl`` was never created. From
the next bot restart on, the mirror resolved nothing and the thread went silent.

Two things were wrong, and each is pinned here:

* "claude has not appeared yet" was read as "claude exited". Only claude having
  been seen and then gone — or the CLI's own "No conversation found" — is a
  failure; a pane that has not exec'd claude yet is undecided.
* the fallback typed a fresh start without looking: if claude is running by
  then, the resume worked, and the typed command line would land in its input
  box (or be swallowed while it boots) while the claim moved to a session that
  never starts.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner

from .test_tmux_runner import _load_fixture, _make_pane

FAST = {
    "c_lord.claude.tmux_runner._POLL_INTERVAL": 0.01,
    "c_lord.claude.tmux_runner._MENU_NAV_DELAY": 0.0,
    "c_lord.claude.tmux_runner._STARTUP_TIMEOUT": 0.03,
    "c_lord.claude.tmux_runner._RESPONSE_STABLE_TIMEOUT": 0.05,
    "c_lord.claude.tmux_runner._POST_STARTUP_DELAY": 0.01,
    "c_lord.claude.tmux_runner._CONTINUE_CHECK_DELAY": 0.01,
    "c_lord.claude.tmux_runner._CONTINUE_SETTLE": 0.05,
    "c_lord.claude.tmux_runner._CONTINUE_VERDICT_TIMEOUT": 0.5,
}


class _SlowResume:
    """The pane after ``claude --resume``: zsh for *boot_polls* probes, then claude."""

    def __init__(self, boot_polls: int) -> None:
        self.boot_polls = boot_polls
        self.probes = 0
        self.starts: list[bool] = []
        self.up = _make_pane(["● Resumed."], with_input_prompt=True)

    def start_claude(self, *_args, **kwargs) -> bool:
        self.starts.append(bool(kwargs.get("try_continue")))
        self.probes = 0
        return True

    def _booted(self) -> bool:
        return bool(self.starts) and self.probes > self.boot_polls

    def is_claude_running(self, _thread_id=None) -> bool:
        if self.starts:
            self.probes += 1
        return self._booted()

    def capture_pane(self, _thread_id=None) -> str:
        return self.up if self._booted() else "$ "


def _runner(fake: _SlowResume) -> TmuxClaudeRunner:
    mgr = MagicMock()
    mgr.start_claude.side_effect = fake.start_claude
    mgr.is_claude_running.side_effect = fake.is_claude_running
    mgr.capture_pane.side_effect = fake.capture_pane
    fake.start_claude(try_continue=True)  # ``claude --resume <claim>`` was just typed
    return TmuxClaudeRunner(
        tmux_manager=mgr, thread_id=1564444583767703562, model="opus", try_continue=True
    )


async def _resume_failed(runner: TmuxClaudeRunner) -> bool:
    patches = [patch(name, value) for name, value in FAST.items()]
    for p in patches:
        p.start()
    try:
        return await runner._resume_failed()
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_a_resume_still_booting_at_the_grace_is_kept() -> None:
    """The #994 timeline: zsh at the 3s mark, claude a moment later."""
    fake = _SlowResume(boot_polls=5)

    assert await _resume_failed(_runner(fake)) is False, (
        "the resume was still booting and was judged failed — a fresh start would "
        "be typed and the claim moved to a session that never starts (#994)"
    )


@pytest.mark.asyncio
async def test_a_resume_that_exits_with_no_conversation_still_falls_back() -> None:
    """The other half (#123/#657): the CLI said so — fall back, and promptly."""
    fake = _SlowResume(boot_polls=10**6)  # never comes up
    exited = _load_fixture("continue_no_conversation_exited.txt")
    fake.capture_pane = lambda _tid=None: exited  # type: ignore[method-assign]
    runner = _runner(fake)

    assert await _resume_failed(runner) is True
    assert fake.probes <= 3, "the CLI's own answer should not wait out the verdict timeout"


@pytest.mark.asyncio
async def test_a_claude_that_never_appears_falls_back_at_the_ceiling() -> None:
    fake = _SlowResume(boot_polls=10**6)
    assert await _resume_failed(_runner(fake)) is True


@pytest.mark.asyncio
async def test_a_claude_seen_then_gone_is_a_failure() -> None:
    """#123's original shape: it started, found nothing, exited."""
    fake = _SlowResume(boot_polls=0)
    runner = _runner(fake)
    states = iter([True, True, False])
    runner._tmux.is_claude_running.side_effect = lambda _tid=None: next(states, False)
    runner._tmux.capture_pane.side_effect = lambda _tid=None: "Loading…"

    assert await _resume_failed(runner) is True


@pytest.mark.asyncio
async def test_the_fallback_does_not_type_over_a_claude_that_came_up() -> None:
    """Verdict says failed, but by the fallback claude is running: keep it."""
    fake = _SlowResume(boot_polls=0)
    runner = _runner(fake)

    async def judged_failed() -> bool:
        return False

    runner._continue_came_up = judged_failed  # type: ignore[method-assign]

    assert await _resume_failed(runner) is False, (
        "a fresh start would be typed into a running claude (#994)"
    )


STALE = (
    "$ claude --resume 11111111-1111-1111-1111-111111111111\n"
    "No conversation found with session ID: 11111111-1111-1111-1111-111111111111\n"
    "$ claude --session-id 22222222-2222-2222-2222-222222222222\n"
    "... (that session ran, then exited) ...\n"
)


@pytest.mark.asyncio
async def test_an_old_no_conversation_in_the_scrollback_is_not_this_resumes_answer() -> None:
    """#1002 review: ``capture_pane`` carries ~500 lines of scrollback.

    A window whose earlier resume failed (#123 then typed a fresh start in the
    same window) still shows that "No conversation found". Read as this
    resume's answer, it failed a resume that was still booting — #994 again.
    Only an answer that appeared after this resume was typed counts.
    """
    fake = _SlowResume(boot_polls=5)
    booted = fake.up
    fake.capture_pane = lambda _tid=None: booted if fake._booted() else STALE + "$ "  # type: ignore[method-assign]
    runner = _runner(fake)
    await runner._note_resume_baseline(STALE + "$ ")  # what the pane held when the resume was typed

    assert await _resume_failed(runner) is False


@pytest.mark.asyncio
async def test_a_new_no_conversation_under_an_old_one_still_fails() -> None:
    fake = _SlowResume(boot_polls=10**6)
    answered = STALE + "$ claude --resume 33333333-3333-3333-3333-333333333333\n" + (
        "No conversation found with session ID: 33333333-3333-3333-3333-333333333333\n$ "
    )
    fake.capture_pane = lambda _tid=None: answered  # type: ignore[method-assign]
    runner = _runner(fake)
    await runner._note_resume_baseline(STALE + "$ ")

    assert await _resume_failed(runner) is True
    assert fake.probes <= 3
