"""Tests for the unmarked-pane-input registry (#682).

``send_literal`` types a menu's free-text answer without the jsonl bridge ZWSP
(#172 / #650), so the mirror cannot tell it from human pane input and posted the
user's own sentence back at them with a 👤. The registry is the marker's
companion for that path: c-lord records what it typed, and the mirror asks.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from c_lord.transcript.formatter import ZWSP_MARKER
from c_lord.transcript.pane_echo import PaneEchoRegistry


class TestPaneEchoRegistry:
    def test_registered_text_is_matched_once(self) -> None:
        reg = PaneEchoRegistry()
        reg.register(1, "既存のIssueとは競合してない？")
        assert reg.consume_match(1, "既存のIssueとは競合してない？") is True
        # One-shot: a second, genuinely new copy of the same sentence is a real
        # message and must not be swallowed too.
        assert reg.consume_match(1, "既存のIssueとは競合してない？") is False

    def test_unregistered_text_never_matches(self) -> None:
        """AC4: a human typing in the pane must still reach Discord."""
        reg = PaneEchoRegistry()
        reg.register(1, "c-lord が打った回答")
        assert reg.consume_match(1, "人がペインで打った文") is False

    def test_entries_are_scoped_per_thread(self) -> None:
        reg = PaneEchoRegistry()
        reg.register(1, "はい")
        assert reg.consume_match(2, "はい") is False
        assert reg.consume_match(1, "はい") is True

    def test_wrapping_whitespace_and_stray_marker_do_not_defeat_the_match(self) -> None:
        reg = PaneEchoRegistry()
        reg.register(1, "どれでもいい、動くやつを選んで")
        assert reg.consume_match(1, f"どれでもいい、\n{ZWSP_MARKER}動くやつを 選んで") is True

    def test_matching_is_exact_not_containment(self) -> None:
        """A longer human sentence that merely quotes the answer stays visible."""
        reg = PaneEchoRegistry()
        reg.register(1, "はい")
        assert reg.consume_match(1, "はい、それでお願いします") is False

    def test_short_answers_are_registered(self) -> None:
        """No length floor: menu answers are routinely two characters (#682)."""
        reg = PaneEchoRegistry()
        reg.register(1, "2番")
        assert reg.consume_match(1, "2番") is True

    def test_empty_text_is_not_registered(self) -> None:
        reg = PaneEchoRegistry()
        reg.register(1, "   \n ")
        assert reg.consume_match(1, "") is False

    def test_expired_entries_are_dropped(self) -> None:
        reg = PaneEchoRegistry()
        with patch("c_lord.transcript.pane_echo.time.monotonic", return_value=0.0):
            reg.register(1, "古い回答")
        with patch("c_lord.transcript.pane_echo.time.monotonic", return_value=10_000.0):
            assert reg.consume_match(1, "古い回答") is False

    def test_per_thread_entries_are_capped(self) -> None:
        reg = PaneEchoRegistry()
        for i in range(20):
            reg.register(1, f"answer-{i}")
        assert reg.consume_match(1, "answer-0") is False
        assert reg.consume_match(1, "answer-19") is True

    def test_clear_drops_everything(self) -> None:
        reg = PaneEchoRegistry()
        reg.register(1, "x")
        reg.clear()
        assert reg.consume_match(1, "x") is False


class TestSendLiteralRegisters:
    """The tmux layer is the producer — every unmarked send is recorded."""

    @staticmethod
    def _mgr():
        from c_lord.tmux import TmuxSessionManager

        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        mgr._thread_to_window[12345] = "work1"
        return mgr

    def test_send_literal_registers_the_typed_text(self) -> None:
        from c_lord.transcript.pane_echo import pane_echo

        pane_echo.clear()
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12345\n"),
                MagicMock(returncode=0),
            ]
            assert self._mgr().send_literal(12345, "どれでもいい、動くやつを選んで") is True

        assert pane_echo.consume_match(12345, "どれでもいい、動くやつを選んで") is True
        pane_echo.clear()

    def test_send_literal_does_not_alter_the_answer_text(self) -> None:
        """AC3: no ZWSP regression (#172 / #650) — the TUI gets the raw string."""
        from c_lord.transcript.pane_echo import pane_echo

        pane_echo.clear()
        with patch.dict("os.environ", {"CLORD_BRIDGE_MODE": "jsonl"}):
            with patch("c_lord.tmux._run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="12345\n"),
                    MagicMock(returncode=0),
                ]
                assert self._mgr().send_literal(12345, "メロン") is True
            typed = mock_run.call_args_list[1][0][0]
        assert "メロン" in typed
        assert ZWSP_MARKER not in "".join(typed)
        pane_echo.clear()

    def test_failed_send_registers_nothing(self) -> None:
        """No window means no keystrokes, so no echo can follow."""
        from c_lord.transcript.pane_echo import pane_echo

        pane_echo.clear()
        from c_lord.tmux import TmuxSessionManager

        mgr = TmuxSessionManager(mapping_path="")
        mgr._available = True
        with patch("c_lord.tmux._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert mgr.send_literal(99999, "届かなかった回答") is False

        assert pane_echo.consume_match(99999, "届かなかった回答") is False
