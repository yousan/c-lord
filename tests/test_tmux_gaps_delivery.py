"""Delivery and capture decisions in ``c_lord/tmux.py`` that had no test.

Each scenario comes from the incident that motivated the guarded code:

* #716 — the folder-trust dialog: after it is confirmed, the message is held
  while the dialog is still up, and delivered once it is gone (even if the
  input box is slow to draw).
* #560 — the read-back after Enter: unreadable panes count as delivered (#544's
  positive-evidence rule); a box that still holds the message under an open
  menu gets no second Enter (#485).
* #872 — the first piece of a long message lost: a retype that tmux refuses is
  a reported failure, never an Enter on half a message.
* #809 — tmux's own error is what the user is told; key names are passed after
  ``--``; refused text is never registered as a pane echo (#682/#808).
* #468 / #471 — the transient resize for a tall capture always restores the
  window, settles early on two identical frames, and never resizes when the
  current size cannot be read.

No real tmux server is touched: ``_run`` is replaced in every test.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import (
    TmuxSessionManager,
    _input_box_lost_head,
    _input_box_retains,
)

_FIX = Path(__file__).parent / "fixtures" / "panes"


def _fixture(name: str) -> str:
    return (_FIX / f"{name}.txt").read_text()


BOX_EMPTY = _fixture("input_box_empty")
BOX_STUCK_PLAIN = _fixture("input_box_stuck_plain_text")
HEAD_LOST = _fixture("input_box_head_lost")
TRUST_LIVE = _fixture("trust_prompt_unnumbered_cursor_on_yes")

# What #560's stuck-box fixture holds (see tests/test_send_input_submit.py).
STUCK_PLAIN_PAYLOAD = "短い未送信メッセージ SENTINEL560 です"

# The #872 production message shape: same text the head-lost fixture was made from.
_LINE = "試作の発注文。ふりがなを小さくしてセーフエリアに収める指示を詳しく書いた行です。"
LONG_872 = (
    "【HEADMARK872 長い依頼の先頭】これは長い依頼の先頭です。\n"
    + "".join(f"- 項目{i:02d}: {_LINE}\n" for i in range(34))
    + "最後に TAILMARK872 と書いた。この依頼には ok とだけ返して"
)


def _ok(stdout: str = "") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "") -> MagicMock:
    return MagicMock(returncode=1, stdout="", stderr=stderr)


def _mgr(window: str | None = "w1") -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: window  # type: ignore[method-assign]
    return mgr


def _argvs(mock_run: MagicMock) -> list[list[str]]:
    return [c.args[0] for c in mock_run.call_args_list]


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("c_lord.tmux.time.sleep"):
        yield


# ── #716: after the trust dialog is confirmed ────────────────────────────


class TestAwaitTrustDialogClosed:
    def test_dialog_still_up_at_the_deadline_withholds_the_message(self) -> None:
        """Typing onto a dialog that never closed would *answer* it (#716).

        The trailing Enter of the message would confirm whatever the cursor is
        on — with the current dialog that can be "No, exit".
        """
        with (
            patch("c_lord.tmux._TRUST_BOOT_TIMEOUT", 0.02),
            patch("c_lord.tmux._run", return_value=_ok(TRUST_LIVE)),
        ):
            assert _mgr()._await_trust_dialog_closed("t:w1", 716) is False

    def test_dialog_gone_but_box_slow_still_delivers(self, caplog) -> None:  # noqa: ANN001
        """Only the dialog *being up* blocks delivery; a slow TUI does not.

        Claude is still the pane's foreground process, the dialog has gone,
        but the status bar has not been drawn by the deadline. Holding the
        message hostage on that would turn a slow boot into a lost turn.
        """

        def run(argv: list[str]) -> MagicMock:
            if "list-panes" in argv:
                return _ok("claude\n")
            return _ok("\n\n  booting…\n")

        with (
            patch("c_lord.tmux._TRUST_BOOT_TIMEOUT", 0.02),
            patch("c_lord.tmux._run", side_effect=run),
        ):
            assert _mgr()._await_trust_dialog_closed("t:w1", 716) is True
        assert any("never appeared" in r.getMessage() for r in caplog.records)


# ── #560: confirm the message left the box ───────────────────────────────


class TestConfirmSubmitted:
    def test_unreadable_pane_counts_as_submitted(self) -> None:
        """#544/#560: no positive evidence of failure → report success."""
        with patch("c_lord.tmux._run", return_value=_fail("can't find pane")) as mock_run:
            assert _mgr()._confirm_submitted("t:w1", STUCK_PLAIN_PAYLOAD, 560) is True
        assert not any("Enter" in a for a in _argvs(mock_run))

    def test_open_menu_over_a_retained_box_gets_no_second_enter(self) -> None:
        """#485: an Enter on an open menu answers it — never fabricate an answer."""
        with (
            patch("c_lord.tmux._run", return_value=_ok(BOX_STUCK_PLAIN)) as mock_run,
            patch("c_lord.tmux._pane_has_open_menu", return_value=True),
        ):
            assert _mgr()._confirm_submitted("t:w1", STUCK_PLAIN_PAYLOAD, 560) is True
        assert not any(a[-1] == "Enter" for a in _argvs(mock_run))

    def test_input_box_holds_reads_the_stuck_message(self) -> None:
        """#560: tells "typed in but will not submit" apart from "never arrived"."""
        with patch("c_lord.tmux._run", return_value=_ok(BOX_STUCK_PLAIN)):
            assert _mgr().input_box_holds(560, STUCK_PLAIN_PAYLOAD) is True
        with patch("c_lord.tmux._run", return_value=_ok(BOX_EMPTY)):
            assert _mgr().input_box_holds(560, STUCK_PLAIN_PAYLOAD) is False

    def test_input_box_holds_is_unknown_when_it_cannot_look(self) -> None:
        with patch("c_lord.tmux._run", return_value=_fail()):
            assert _mgr().input_box_holds(560, "x") is None
        assert _mgr(window=None).input_box_holds(560, "x") is None
        mgr = _mgr()
        mgr._available = False
        assert mgr.input_box_holds(560, "x") is None

    def test_whitespace_only_payload_is_not_evidence(self) -> None:
        """A non-empty box plus a payload with no characters proves nothing."""
        assert _input_box_retains(BOX_STUCK_PLAIN, " \n ") is False


# ── #872: head-lost detection and the retype ─────────────────────────────


class TestHeadLost:
    def test_short_payload_is_never_judged_head_lost(self) -> None:
        """Only a payload long enough to have a distinct head and tail qualifies."""
        assert _input_box_lost_head(HEAD_LOST, "TAILMARK872 と書いた") is False

    def test_box_holding_the_head_has_not_lost_it(self) -> None:
        assert _input_box_lost_head(HEAD_LOST, "- 項目22: " + _LINE * 2) is False

    def test_the_production_frame_is_head_lost(self) -> None:
        assert _input_box_lost_head(HEAD_LOST, LONG_872) is True

    def test_refused_retype_is_a_reported_failure_and_no_enter(self) -> None:
        """#872 + #809: the retype tmux refuses ends the send, with tmux's reason.

        Pressing Enter here would submit the second half on its own — the very
        outcome #872 exists to stop.
        """
        mgr = _mgr()
        state = {"frame": HEAD_LOST}

        def run(argv: list[str]) -> MagicMock:
            if "capture-pane" in argv:
                return _ok(state["frame"])
            if "send-keys" in argv and "-l" in argv:
                return _fail("command too long")
            if "send-keys" in argv and "C-u" in argv:
                state["frame"] = BOX_EMPTY
            return _ok()

        with patch("c_lord.tmux._run", side_effect=run) as mock_run:
            assert mgr._ensure_head_landed("t:w1", LONG_872, 872) is False

        assert mgr.take_send_failure(872) == "command too long"
        assert not any(a[-1] == "Enter" for a in _argvs(mock_run))


# ── #809: keys, literals and the reasons reported ─────────────────────────


class TestSendKeys:
    def test_no_keys_is_a_no_op_even_without_tmux(self) -> None:
        mgr = _mgr()
        mgr._available = False
        with patch("c_lord.tmux._run") as mock_run:
            assert mgr.send_keys(1) is True
        mock_run.assert_not_called()

    def test_key_names_follow_a_double_dash(self) -> None:
        """#809: a key name is never an option."""
        with patch("c_lord.tmux._run", return_value=_ok()) as mock_run:
            assert _mgr().send_keys(1, "Down", "Enter") is True
        argv = _argvs(mock_run)[-1]
        assert argv[argv.index("--") + 1 :] == ["Down", "Enter"]

    def test_refused_keys_report_tmux_own_error(self) -> None:
        """#809: the user is told tmux's reason, not a guessed "no window"."""
        mgr = _mgr()
        with patch("c_lord.tmux._run", return_value=_fail("unknown key: Dwon")):
            assert mgr.send_keys(1, "Dwon") is False
        assert mgr.take_send_failure(1) == "unknown key: Dwon"
        assert mgr.take_send_failure(1) is None, "the reason is handed out once"

    def test_refusal_without_stderr_still_has_a_reason(self) -> None:
        mgr = _mgr()
        with patch("c_lord.tmux._run", return_value=MagicMock(returncode=2, stderr="")):
            assert mgr.send_keys(1, "Enter") is False
        assert mgr.take_send_failure(1) == "send-keys exited 2"

    def test_no_window_is_false(self) -> None:
        assert _mgr(window=None).send_keys(1, "Enter") is False


class TestSendLiteral:
    def test_bulleted_answer_is_typed_after_double_dash(self) -> None:
        """#809: 9/24 Factorio thread — a menu answer written as ``- …`` bullets."""
        with (
            patch("c_lord.tmux._run", return_value=_ok()) as mock_run,
            patch("c_lord.transcript.pane_echo.pane_echo.register") as register,
        ):
            assert _mgr().send_literal(1, "- サーバリストを更新") is True
        argv = _argvs(mock_run)[0]
        assert argv[-2:] == ["--", "- サーバリストを更新"]
        register.assert_called_once_with(1, "- サーバリストを更新")

    def test_refused_text_is_not_registered_as_an_echo(self) -> None:
        """#682/#808: nothing reached the pane, so nothing can echo back."""
        mgr = _mgr()
        with (
            patch("c_lord.tmux._run", return_value=_fail("invalid flag -")),
            patch("c_lord.transcript.pane_echo.pane_echo.register") as register,
        ):
            assert mgr.send_literal(1, "- 箇条書き") is False
        register.assert_not_called()
        assert mgr.take_send_failure(1) == "invalid flag -"

    def test_no_window_or_tmux_is_false(self) -> None:
        assert _mgr(window=None).send_literal(1, "x") is False
        mgr = _mgr()
        mgr._available = False
        assert mgr.send_literal(1, "x") is False


class TestStartClaudeFailures:
    """A start that tmux refused must not look like a started turn."""

    @pytest.fixture
    def prompt_file(self, tmp_path: Path):
        path = tmp_path / "clord-prompt-test.txt"

        def write(prompt: str) -> Path:
            path.write_text(prompt, encoding="utf-8")
            return path

        with patch("c_lord.tmux._write_prompt_file", side_effect=write):
            yield path

    def test_refused_command_is_reported_with_tmux_reason(self, prompt_file: Path) -> None:
        """#527/#809: no Enter on a half-typed command, and tmux's reason kept."""
        mgr = _mgr()

        def run(argv: list[str]) -> MagicMock:
            if "display-message" in argv:
                return _fail()  # pane path unknown → no claim written (#773)
            if "send-keys" in argv and "-l" in argv:
                return _fail("command too long")
            return _ok()

        with (
            patch("c_lord.tmux._run", side_effect=run) as mock_run,
            patch("c_lord.transcript.pane_echo.pane_echo.register") as register,
        ):
            assert mgr.start_claude(773, "最初の依頼") is False

        assert not any(a[-1] == "Enter" for a in _argvs(mock_run))
        register.assert_not_called()
        assert mgr.take_send_failure(773) == "command too long"

    def test_failed_enter_is_not_a_started_turn(self, prompt_file: Path) -> None:
        def run(argv: list[str]) -> MagicMock:
            if "display-message" in argv:
                return _fail()
            if argv[-1] == "Enter":
                return _fail("can't find pane")
            return _ok()

        with (
            patch("c_lord.tmux._run", side_effect=run),
            patch("c_lord.transcript.pane_echo.pane_echo.register") as register,
        ):
            assert _mgr().start_claude(773, "最初の依頼") is False
        register.assert_not_called()

    def test_unknown_pane_path_falls_back_to_continue_on_resume(self) -> None:
        """#773: without a pane path no claim can be read — the old ``--continue``."""
        assert _mgr()._session_flags(None, try_continue=True) == ["--continue"]
        assert _mgr()._session_flags("", try_continue=False) == []


# ── pane lookups that answer "don't know" ────────────────────────────────


class TestUnknownIsNone:
    """#510/#651/#803: "no window" / "tmux unavailable" is None, never a guess."""

    def test_pane_foreground_command(self) -> None:
        with patch("c_lord.tmux._run", return_value=_ok("claude\n")):
            assert _mgr().pane_foreground_command(1) == "claude"
        with patch("c_lord.tmux._run", return_value=_ok("\n")):
            assert _mgr().pane_foreground_command(1) is None
        assert _mgr(window=None).pane_foreground_command(1) is None
        mgr = _mgr()
        mgr._available = False
        assert mgr.pane_foreground_command(1) is None

    def test_pane_working_dir(self) -> None:
        with patch("c_lord.tmux._run", return_value=_ok("/sessions/ch/thr\n")) as mock_run:
            assert _mgr().pane_working_dir(1) == "/sessions/ch/thr"
        assert "t:w1" in _argvs(mock_run)[0]
        assert _mgr(window=None).pane_working_dir(1) is None
        mgr = _mgr()
        mgr._available = False
        assert mgr.pane_working_dir(1) is None

    def test_project_dir_for(self) -> None:
        from c_lord.transcript.resolver import derive_project_dir

        with patch("c_lord.tmux._run", return_value=_ok("/sessions/ch/thr\n")):
            assert _mgr().project_dir_for(1) == derive_project_dir("/sessions/ch/thr")
        with patch("c_lord.tmux._run", return_value=_fail()):
            assert _mgr().project_dir_for(1) is None
        assert _mgr(window=None).project_dir_for(1) is None
        mgr = _mgr()
        mgr._available = False
        assert mgr.project_dir_for(1) is None


# ── #468 / #471: transient resize for a tall capture ─────────────────────


class TestCaptureAfterGrow:
    def _resizes(self, mock_run: MagicMock) -> list[str]:
        return [a[-1] for a in _argvs(mock_run) if "resize-window" in a]

    def test_settles_on_two_identical_frames(self) -> None:
        """The redraw is done once two consecutive captures match."""
        frames = iter(["", "partial", "full", "full", "never read"])

        def run(argv: list[str]) -> MagicMock:
            if "display-message" in argv:
                return _ok("160 40\n")
            if "capture-pane" in argv:
                return _ok(next(frames))
            return _ok()

        with patch("c_lord.tmux._run", side_effect=run) as mock_run:
            text = _mgr()._capture_after_grow(
                "t:w1", ["tmux", "capture-pane", "-p"], 240, lambda: "fallback"
            )

        assert text == "full"
        assert sum("capture-pane" in a for a in _argvs(mock_run)) == 4
        assert self._resizes(mock_run) == ["240", "40"]

    def test_window_is_restored_even_when_the_capture_raises(self) -> None:
        """An attached human must never be left with a 240-row window."""

        def run(argv: list[str]) -> MagicMock:
            if "display-message" in argv:
                return _ok("160 40\n")
            if "capture-pane" in argv:
                raise OSError("tmux vanished")
            return _ok()

        with (
            patch("c_lord.tmux._run", side_effect=run) as mock_run,
            pytest.raises(OSError),
        ):
            _mgr()._capture_after_grow("t:w1", ["tmux", "capture-pane", "-p"], 240, lambda: "")

        assert self._resizes(mock_run) == ["240", "40"]

    @pytest.mark.parametrize("size", [_fail(), _ok("garbage\n")])
    def test_unreadable_size_never_resizes(self, size: MagicMock) -> None:
        """Without the original size there is nothing to restore to."""
        with patch("c_lord.tmux._run", return_value=size) as mock_run:
            text = _mgr()._capture_after_grow(
                "t:w1", ["tmux", "capture-pane", "-p"], 240, lambda: "fallback"
            )
        assert text == "fallback"
        assert self._resizes(mock_run) == []
