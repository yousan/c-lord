"""Pin how a tmux turn ends and how the poll loop answers the pane (coverage gaps).

These tests pin CURRENT behaviour of ``c_lord/claude/tmux_runner.py`` that the
existing suite did not reach, so the next fix that changes it fails CI:

* the poll loop's permission auto-accept (#152/#156) — the branch that keeps
  the turn moving instead of stalling or alerting "unknown prompt";
* the #468 tall-capture recovery of pre-menu context, inside the loop;
* the #166 pane_ask dedup (one bridge per menu, not one per poll);
* the #649 "ambiguous window" diagnosis on the ``send_input`` path;
* small decision helpers (unknown-prompt signature, plan menu bail-outs,
  trust option offset on the #630 corpse).

Every pane comes from ``tests/fixtures/panes/`` (real captures); where a
capture is cut down, the comment says from which one and why.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from c_lord.claude.tmux_runner import (
    TmuxClaudeRunner,
    _normalize_capture,
    _parse_ask_from_pane,
    _parse_plan_from_pane,
    _unknown_prompt_signature,
)
from c_lord.claude.types import MessageType
from c_lord.turn_end_bus import turn_end_bus

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "panes"


def _load_fixture(name: str) -> str:
    return (_FIXTURES_DIR / name).read_text()


# Same shapes as ``TestTranscriptTurnEnd`` in tests/test_tmux_runner.py: a pane
# that is visibly generating, and one that is alive with no scrapable answer.
def _generating(n: int) -> str:
    return f"\n✽ Generating… ({n}s · ↑ 1.2k tokens)\n────────\n❯\n────────\n-- INSERT --"


def _ticking(n: int) -> str:
    return (
        f"  Running 2 background tasks ({n}s)\n"
        "────────────────────────────────\n"
        "❯\n"
        "────────────────────────────────\n"
        "-- INSERT -- ⏵⏵ bypass permissions on"
    )


@pytest.fixture
def tmux_manager():
    """Mock TmuxSessionManager — same healthy defaults as test_tmux_runner.py."""
    from unittest.mock import MagicMock

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


async def _drain(runner: TmuxClaudeRunner, prompt: str = "test", timeout: float = 5.0) -> list:
    events: list = []

    async def _go() -> None:
        async for event in runner.run(prompt):
            events.append(event)

    await asyncio.wait_for(_go(), timeout=timeout)
    return events


# -- Permission auto-accept inside the poll loop (#152/#156) -----------------


class TestPermissionPromptInPollLoop:
    """A live permission prompt mid-turn is answered in the pane, not bridged.

    ``real_yn_prompt_at_bottom.txt`` is the real ``git push --force`` security
    warning (``Continue anyway? [y/N]``) at the bottom of the pane (#152).  The
    loop must press ``y`` (Enter would pick the default N), must NOT surface it
    as an unknown TUI prompt, and the turn must still end normally afterwards.
    """

    @pytest.mark.asyncio
    async def test_yn_prompt_mid_turn_is_accepted_with_y_and_turn_ends(self, tmux_manager) -> None:
        thread_id = 156001
        turn_end_bus.forget(thread_id)
        runner = TmuxClaudeRunner(
            tmux_manager=tmux_manager, thread_id=thread_id, model="sonnet", timeout_seconds=600
        )
        yn_pane = _load_fixture("real_yn_prompt_at_bottom.txt")
        sent: list[list[str]] = []
        calls = {"n": 0}

        def capture_fn(_tid):
            calls["n"] += 1
            n = calls["n"]
            if n == 2:
                turn_end_bus.note_prompt(thread_id)
            if n <= 2:
                return _generating(n)
            if not sent:
                return yn_pane
            # Answered: the prompt is gone and the transcript closes the turn.
            turn_end_bus.mark(thread_id)
            return _ticking(n)

        tmux_manager.capture_pane.side_effect = capture_fn
        try:
            with (
                patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.01),
                patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
                patch("c_lord.claude.tmux_runner._UNKNOWN_ALERT_DELAY", 0.0),
                patch("c_lord.tmux._run", side_effect=lambda args, **_k: sent.append(args)),
            ):
                events = await _drain(runner)
        finally:
            turn_end_bus.forget(thread_id)

        # #649: addressed by the unique target, and "y" — not Enter (#152).
        assert sent == [["tmux", "send-keys", "-t", "@7", "y"]]
        assert not any(e.unknown_tui_prompt for e in events), (
            "a known permission prompt must be auto-accepted, not alerted as unknown"
        )
        assert not any(e.pane_ask for e in events)
        result = [e for e in events if e.is_complete]
        assert len(result) == 1
        assert result[0].message_type == MessageType.RESULT
        assert result[0].error is None

    @pytest.mark.asyncio
    async def test_marker_quoted_in_conversation_is_not_accepted(self, tmux_manager) -> None:
        """#156: ``Continue anyway? [y/N]`` inside Claude's answer is not a prompt.

        ``bug_156_yn_in_conversation.txt`` is the real self-review answer that
        quoted the marker; pressing ``y`` there would type into the input box.
        """
        thread_id = 156002
        turn_end_bus.forget(thread_id)
        runner = TmuxClaudeRunner(
            tmux_manager=tmux_manager, thread_id=thread_id, model="sonnet", timeout_seconds=600
        )
        pane = _load_fixture("bug_156_yn_in_conversation.txt")
        calls = {"n": 0}

        def capture_fn(_tid):
            calls["n"] += 1
            n = calls["n"]
            if n == 2:
                turn_end_bus.note_prompt(thread_id)
            if n == 8:
                turn_end_bus.mark(thread_id)
            return _generating(n) if n <= 2 else pane

        tmux_manager.capture_pane.side_effect = capture_fn
        sent: list = []
        try:
            with (
                patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.01),
                patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
                patch("c_lord.tmux._run", side_effect=lambda args, **_k: sent.append(args)),
            ):
                events = await _drain(runner)
        finally:
            turn_end_bus.forget(thread_id)

        assert sent == [], "no keystroke may be sent for a marker quoted in prose"
        assert not any(e.unknown_tui_prompt for e in events)
        result = [e for e in events if e.is_complete]
        assert len(result) == 1
        assert result[0].error is None

    @pytest.mark.asyncio
    async def test_no_window_means_no_keystroke(self, tmux_manager) -> None:
        """No window for the thread → nothing is typed anywhere (#649)."""
        runner = TmuxClaudeRunner(tmux_manager=tmux_manager, thread_id=156003, model="sonnet")
        tmux_manager._find_window_for_thread.return_value = None
        with patch("c_lord.tmux._run") as run:
            await runner._accept_permission_prompt(_load_fixture("real_yn_prompt_at_bottom.txt"))
        run.assert_not_called()


# -- #468 tall-capture recovery + #166 dedup, inside the poll loop -----------


class TestAskBridgeInPollLoop:
    """The loop bridges an AskUserQuestion menu once, with its context.

    ``bug_468_long_prose_40rows.txt`` / ``bug_468_long_prose_tall.txt`` are the
    same real menu captured at 40 rows (prose scrolled off → no context) and
    after a resize to 240 rows (prose redrawn).  The parse-level halves are in
    tests/test_tmux_tall_capture.py; these pin the loop's use of them.
    """

    SHORT = "bug_468_long_prose_40rows.txt"
    TALL = "bug_468_long_prose_tall.txt"

    async def _first_ask(self, tmux_manager, thread_id: int, *, polls_with_menu: int = 6):
        """Run with the menu up for ``polls_with_menu`` polls; return pane_ask events."""
        turn_end_bus.forget(thread_id)
        runner = TmuxClaudeRunner(
            tmux_manager=tmux_manager, thread_id=thread_id, model="sonnet", timeout_seconds=600
        )
        short = _load_fixture(self.SHORT)
        calls = {"n": 0}

        def capture_fn(_tid):
            calls["n"] += 1
            n = calls["n"]
            if n == 1:
                # Pre-send peek: no menu yet (else #358's Esc would be sent).
                return _generating(n)
            if n == 2:
                turn_end_bus.note_prompt(thread_id)
            if n < 2 + polls_with_menu:
                return short
            turn_end_bus.mark(thread_id)
            return _ticking(n)

        tmux_manager.capture_pane.side_effect = capture_fn
        try:
            with (
                patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.01),
                patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
                patch("c_lord.claude.tmux_runner._ASK_ALERT_DELAY", 0.0),
            ):
                events = await _drain(runner)
        finally:
            turn_end_bus.forget(thread_id)
        return [e for e in events if e.pane_ask is not None], events

    @pytest.mark.asyncio
    async def test_empty_context_is_recovered_from_the_tall_capture(self, tmux_manager) -> None:
        tmux_manager.capture_pane_tall.return_value = _load_fixture(self.TALL)
        asks, events = await self._first_ask(tmux_manager, 468001)

        assert len(asks) == 1, "one menu → one bridge, however many polls it stays up (#166)"
        q = asks[0].pane_ask
        assert asks[0].message_type == MessageType.SYSTEM
        assert len(q.context) > 1000, "#468: the scrolled-off prose must reach Discord"
        short_q = _parse_ask_from_pane(_normalize_capture(_load_fixture(self.SHORT)))
        assert short_q is not None
        assert [o.label for o in q.options] == [o.label for o in short_q.options]
        tmux_manager.capture_pane_tall.assert_called_once()
        assert [e for e in events if e.is_complete][-1].error is None

    @pytest.mark.asyncio
    async def test_tall_capture_without_context_keeps_the_original_question(
        self, tmux_manager
    ) -> None:
        """A tall capture that recovers nothing must not replace (or drop) the menu."""
        # A real pane with no menu at all: recovery yields None.
        tmux_manager.capture_pane_tall.return_value = _load_fixture("input_box_empty.txt")
        asks, _ = await self._first_ask(tmux_manager, 468002)

        assert len(asks) == 1, "the menu is still bridged — only the context is missing"
        assert asks[0].pane_ask.context == ""
        assert asks[0].pane_ask.options

    @pytest.mark.asyncio
    async def test_empty_tall_capture_is_ignored(self, tmux_manager) -> None:
        tmux_manager.capture_pane_tall.return_value = ""
        asks, _ = await self._first_ask(tmux_manager, 468003)
        assert len(asks) == 1
        assert asks[0].pane_ask.context == ""

    @pytest.mark.asyncio
    async def test_menu_with_context_does_not_resize_the_window(self, tmux_manager) -> None:
        """Only the failing case pays for the resize round-trip (#468)."""
        thread_id = 468004
        turn_end_bus.forget(thread_id)
        runner = TmuxClaudeRunner(
            tmux_manager=tmux_manager, thread_id=thread_id, model="sonnet", timeout_seconds=600
        )
        pane = _load_fixture("ask_context_prose_above_menu.txt")
        assert _parse_ask_from_pane(_normalize_capture(pane)).context, "fixture precondition"
        calls = {"n": 0}

        def capture_fn(_tid):
            calls["n"] += 1
            n = calls["n"]
            if n == 1:
                return _generating(n)
            if n == 2:
                turn_end_bus.note_prompt(thread_id)
            if n < 6:
                return pane
            turn_end_bus.mark(thread_id)
            return _ticking(n)

        tmux_manager.capture_pane.side_effect = capture_fn
        try:
            with (
                patch("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.01),
                patch("c_lord.claude.tmux_runner._POST_STARTUP_DELAY", 0.0),
                patch("c_lord.claude.tmux_runner._ASK_ALERT_DELAY", 0.0),
            ):
                events = await _drain(runner)
        finally:
            turn_end_bus.forget(thread_id)

        assert len([e for e in events if e.pane_ask is not None]) == 1
        tmux_manager.capture_pane_tall.assert_not_called()


# -- #649 on the send_input path ---------------------------------------------


class TestSendFailureDiagnosis:
    """Which reason a refused ``send_input`` reports (#560/#649/#809 ladder)."""

    @pytest.mark.asyncio
    async def test_duplicate_window_names_are_named_instead_of_restart_advice(
        self, tmux_manager
    ) -> None:
        tmux_manager.send_input.return_value = False
        tmux_manager.input_box_holds.return_value = False
        tmux_manager.take_send_failure.return_value = None
        tmux_manager.duplicate_window_names.return_value = ["w134"]
        runner = TmuxClaudeRunner(tmux_manager=tmux_manager, thread_id=649001, model="sonnet")

        events = await _drain(runner)

        err = events[-1].error or ""
        assert events[-1].is_complete
        assert "一意に特定できません" in err
        assert "`w134`" in err
        assert "thread=649001" in err
        # #649: the pane is fine; restarting claude in it cannot help.
        assert "では直りません" in err

    @pytest.mark.asyncio
    async def test_unreadable_duplicate_probe_falls_back_to_the_general_reason(
        self, tmux_manager
    ) -> None:
        """A failed probe must not assert a cause it did not verify (#649)."""
        tmux_manager.send_input.return_value = False
        tmux_manager.input_box_holds.return_value = False
        tmux_manager.take_send_failure.return_value = None
        tmux_manager.duplicate_window_names.side_effect = RuntimeError("tmux hiccup")
        runner = TmuxClaudeRunner(tmux_manager=tmux_manager, thread_id=649002, model="sonnet")

        events = await _drain(runner)

        err = events[-1].error or ""
        assert "一意に特定できません" not in err
        assert "claude-restart" in err
        assert "bytes" in err

    @pytest.mark.asyncio
    async def test_raising_send_failure_probe_reads_as_no_refusal(self, tmux_manager) -> None:
        """#809: an unreadable tmux refusal is ``None``, not a crash."""
        tmux_manager.take_send_failure.side_effect = RuntimeError("boom")
        runner = TmuxClaudeRunner(tmux_manager=tmux_manager, thread_id=809001, model="sonnet")
        assert runner._take_send_failure() is None


# -- Decision helpers --------------------------------------------------------


class TestUnknownPromptSignature:
    """#165 dedup identity: the parts of a prompt that stay put while it lingers."""

    def test_inline_yn_line_is_the_signature(self) -> None:
        pane = _load_fixture("real_yn_prompt_at_bottom.txt")
        assert _unknown_prompt_signature(pane) == "Continue anyway? [y/N]"

    def test_yn_quoted_in_prose_contributes_nothing(self) -> None:
        # The quote sits far above the bottom zone (#156).
        assert _unknown_prompt_signature(_load_fixture("bug_156_yn_in_conversation.txt")) == ""


class TestPlanMenuBailOuts:
    """``_parse_plan_from_pane`` refuses anything that is not a full plan menu.

    Both inputs are ``plan_approval_menu.txt`` (real) cut down to the frames a
    redraw can leave on screen.
    """

    def _plan_lines(self) -> list[str]:
        return _load_fixture("plan_approval_menu.txt").splitlines()

    def test_half_drawn_menu_with_one_option_is_not_bridged(self) -> None:
        lines = self._plan_lines()
        sig = next(i for i, line in enumerate(lines) if "Would you like to proceed?" in line)
        # Signature + only the first option row painted so far.
        frame = "\n".join(lines[: sig + 2])
        assert _parse_plan_from_pane(frame) is None

    def test_non_option_line_between_question_and_options_bails(self) -> None:
        lines = self._plan_lines()
        sig = next(i for i, line in enumerate(lines) if "Would you like to proceed?" in line)
        frame = "\n".join([*lines[: sig + 1], "● 計画を検討しています...", *lines[sig + 1 :]])
        assert _parse_plan_from_pane(frame) is None

    def test_full_menu_still_parses(self) -> None:
        q = _parse_plan_from_pane(_load_fixture("plan_approval_menu.txt"))
        assert q is not None
        assert [o.label for o in q.options] == [
            "Yes",
            "Yes, and auto-accept edits",
            "No, keep planning",
        ]
        assert q.allow_other is False


class TestTrustOptionOffset:
    """How many Downs the trust accept sends (#630/#684)."""

    def test_no_dialog_needs_no_down(self) -> None:
        assert TmuxClaudeRunner._trust_option_offset("") == 0
        assert TmuxClaudeRunner._trust_cursor_label("") is None

    def test_current_unnumbered_dialog_needs_one_down(self) -> None:
        pane = _load_fixture("trust_prompt_live_cursor_on_no.txt")
        assert TmuxClaudeRunner._trust_cursor_label(pane) == "No, exit"
        assert TmuxClaudeRunner._trust_option_offset(pane) == 1

    def test_corpse_cursor_is_read_off_the_last_dialog(self) -> None:
        """#630's declined dialog: the run ends at the footer and is still banked."""
        pane = _load_fixture("trust_prompt_residue_after_exit.txt")
        assert TmuxClaudeRunner._trust_cursor_label(pane) == "No, exit"
        # Cursor already BELOW "Yes" → no Down (never a negative count).
        assert TmuxClaudeRunner._trust_option_offset(pane) == 0
