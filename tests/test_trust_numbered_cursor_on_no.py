"""The numbered trust dialog with its cursor on "2. No, exit" is answered "Yes" (#990).

``tests/fixtures/panes/trust_prompt_numbered_cursor_on_no.txt`` is the real live
numbered dialog (``trust_prompt_at_top.txt``) with the cursor on option 2 —
the live shape quoted in #630's comment "実測2" (``   1. Yes, I trust this
folder`` / `` ❯ 2. No, exit``). How the cursor gets there live is not known.

Before: Yes sits *above* the cursor, the Down count came out 0, and 0 meant
"already on Yes" — so a bare Enter answered "No, exit" and claude exited.

Decided 2026-10-29 (おぷー): never lean towards ending claude. Select "Yes"
explicitly — move up to it and confirm only once the cursor reads "Yes" — and
leave #630's corpse alone: with claude gone, an Up typed into the shell recalls
its last command, so keys are sent only to a pane positively running claude.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner
from c_lord.tmux import TmuxSessionManager

_P = Path(__file__).parent / "fixtures" / "panes"
ON_NO = (_P / "trust_prompt_numbered_cursor_on_no.txt").read_text()
ON_YES = (_P / "trust_prompt_at_top.txt").read_text()
UNNUMBERED_ON_NO = (_P / "trust_prompt_live_cursor_on_no.txt").read_text()
UNNUMBERED_ON_YES = (_P / "trust_prompt_unnumbered_cursor_on_yes.txt").read_text()


def test_the_fixture_is_the_shape_in_question() -> None:
    assert TmuxClaudeRunner._has_trust_prompt(ON_NO)
    assert TmuxClaudeRunner._trust_cursor_label(ON_NO) == "No, exit"


class _Dialog:
    """A live dialog: Up/Down move the cursor (unless *stuck*), Enter confirms."""

    def __init__(self, start: str, *, stuck: bool = False, alive: bool = True) -> None:
        self.pane = start
        self.stuck = stuck
        self.alive = alive
        self.keys: list[str] = []

    def send_keys(self, _tid, key) -> bool:
        self.keys.append(key)
        if self.stuck:
            return True
        if key == "Up" and self.pane == ON_NO:
            self.pane = ON_YES
        elif key == "Down" and self.pane == UNNUMBERED_ON_NO:
            self.pane = UNNUMBERED_ON_YES
        return True

    def capture_pane(self, _tid=None) -> str:
        return self.pane


def _accept(dialog: _Dialog) -> bool:
    mgr = MagicMock()
    mgr.send_keys.side_effect = dialog.send_keys
    mgr.capture_pane.side_effect = dialog.capture_pane
    mgr.is_claude_running.return_value = dialog.alive
    runner = TmuxClaudeRunner(tmux_manager=mgr, thread_id=990, model="sonnet")
    with patch("c_lord.claude.tmux_runner._MENU_NAV_DELAY", 0.0):
        return asyncio.run(runner._accept_trust_prompt(dialog.pane))


class TestRunnerAccept:
    def test_cursor_on_numbered_no_moves_up_then_confirms(self) -> None:
        dialog = _Dialog(ON_NO)
        assert _accept(dialog) is True
        assert dialog.keys == ["Up", "Enter"]

    def test_still_on_no_after_up_sends_no_enter(self) -> None:
        dialog = _Dialog(ON_NO, stuck=True)
        assert _accept(dialog) is False
        assert "Enter" not in dialog.keys
        assert set(dialog.keys) == {"Up"}

    def test_a_pane_not_running_claude_gets_no_keys(self) -> None:
        """#630: with claude gone an Up lands in the shell and recalls history."""
        dialog = _Dialog(ON_NO, alive=False)
        assert _accept(dialog) is False
        assert dialog.keys == []

    def test_cursor_already_on_yes_is_a_bare_enter(self) -> None:
        dialog = _Dialog(ON_YES)
        assert _accept(dialog) is True
        assert dialog.keys == ["Enter"]

    def test_current_unnumbered_dialog_is_down_then_enter(self) -> None:
        dialog = _Dialog(UNNUMBERED_ON_NO)
        assert _accept(dialog) is True
        assert dialog.keys == ["Down", "Enter"]


class TestSendInputGuard:
    """The delivery path (#716) answers the same dialog before typing a message."""

    def _clear(self, dialog: _Dialog) -> tuple[bool, list[str]]:
        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True

        def run(args: list[str]) -> MagicMock:
            if "send-keys" in args:
                dialog.send_keys(None, args[-1])
                return MagicMock(returncode=0, stdout="", stderr="")
            if "capture-pane" in args:
                return MagicMock(returncode=0, stdout=dialog.pane, stderr="")
            return MagicMock(returncode=0, stdout="claude\n", stderr="")

        with (
            patch("c_lord.tmux._run", side_effect=run),
            patch("c_lord.tmux._TRUST_NAV_DELAY", 0.0),
            patch.object(TmuxSessionManager, "is_claude_running", return_value=dialog.alive),
            patch.object(TmuxSessionManager, "_await_trust_dialog_closed", return_value=True),
        ):
            ok = mgr._clear_trust_dialog("t:w1", 990, dialog.pane)
        return ok, dialog.keys

    def test_numbered_no_moves_up_then_confirms(self) -> None:
        ok, keys = self._clear(_Dialog(ON_NO))
        assert ok is True
        assert keys == ["Up", "Enter"]

    def test_still_on_no_withholds_the_message(self) -> None:
        ok, keys = self._clear(_Dialog(ON_NO, stuck=True))
        assert ok is False
        assert "Enter" not in keys

    @pytest.mark.parametrize(("start", "expected"), [(ON_YES, ["Enter"])])
    def test_cursor_on_yes_is_unchanged(self, start: str, expected: list[str]) -> None:
        assert self._clear(_Dialog(start))[1] == expected
