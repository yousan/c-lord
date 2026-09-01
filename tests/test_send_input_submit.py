"""send_input must not report success for a message that never left the box (#560).

The production failure: a 1211-character multi-line message was typed into the
pane, the TUI folded it into a ``[Pasted text #2 +5 lines]`` placeholder, and the
Enter that ``send_input`` sent immediately afterwards was absorbed by the fold
instead of submitting.  The message sat in the input box for over 20 minutes.
``_type_literal`` and the Enter ``send-keys`` both exit 0, so ``send_input``
returned ``True`` and c-lord believed it had delivered the message — the turn
then died on an idle timeout with no error shown to anyone.

Two things are required and tested here:

* a settle window between the last chunk of text and the Enter, so the TUI has
  finished folding before the keypress arrives, and
* **verification**: after Enter, read the input box back.  If the message is
  still sitting there, press Enter again; if it still will not go, return
  ``False`` so the caller reports a delivery failure instead of a silent drop.

The pane fixtures are real ``capture-pane -p -J`` captures from Claude Code
v2.1.246 — an empty box, a box holding a folded paste placeholder, and a box
holding un-folded plain text.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager

_FIX = Path(__file__).parent / "fixtures" / "panes"

BOX_EMPTY = (_FIX / "input_box_empty.txt").read_text()
BOX_STUCK_PASTE = (_FIX / "input_box_stuck_pasted_placeholder.txt").read_text()
BOX_STUCK_PLAIN = (_FIX / "input_box_stuck_plain_text.txt").read_text()

# The payload that is actually sitting in BOX_STUCK_PLAIN's input box.
STUCK_PLAIN_PAYLOAD = "短い未送信メッセージ SENTINEL560 です"
# BOX_STUCK_PASTE holds this one, folded into a [Pasted text …] placeholder.
STUCK_PASTE_PAYLOAD = "\n".join(
    f"{j + 1}行目: 未送信のまま残った本文 SENTINEL560 " + "あ" * 150 for j in range(5)
)

LONG_MESSAGE = "\n".join(f"{j + 1}行目: " + "あ" * 200 for j in range(6))
SHORT_MESSAGE = "ok"


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]  # noqa: ARG005
    # The pane is known to be vim-less, so #544's probe stays out of the way.
    mgr._vim_mode["w1"] = False
    return mgr


class _Pane:
    """Replays scripted capture frames and records the keys sent."""

    def __init__(self, *frames: str) -> None:
        self._frames = list(frames)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> MagicMock:
        self.calls.append(list(args))
        if "capture-pane" in args:
            frame = self._frames[0] if len(self._frames) == 1 else self._frames.pop(0)
            return MagicMock(returncode=0, stdout=frame)
        return MagicMock(returncode=0, stdout="")

    @property
    def keys(self) -> list[list[str]]:
        return [c for c in self.calls if "send-keys" in c]

    @property
    def enters(self) -> int:
        return sum(1 for c in self.keys if "-l" not in c and c[-1] == "Enter")

    @property
    def literals(self) -> list[str]:
        return [c[-1] for c in self.keys if "-l" in c]


def _send(pane: _Pane, text: str, mgr: TmuxSessionManager | None = None) -> bool:
    with patch("c_lord.tmux._run", side_effect=pane), patch("c_lord.tmux.time.sleep") as slept:
        result = (mgr or _mgr()).send_input(12345, text)
    pane.slept = [c.args[0] for c in slept.call_args_list]  # type: ignore[attr-defined]
    return result


# --------------------------------------------------------------------------
# AC2 — never report success for a message still sitting in the box
# --------------------------------------------------------------------------


def test_folded_paste_left_in_box_is_reported_as_failure() -> None:
    """The #560 bug: Enter swallowed by the paste fold must not read as success.

    Every capture shows the placeholder still in the input box, so no number of
    Enter presses gets it out — send_input has to say so.
    """
    pane = _Pane(BOX_EMPTY, BOX_STUCK_PASTE)  # pre-send frame, then stuck forever
    assert _send(pane, STUCK_PASTE_PAYLOAD) is False, (
        "a message still sitting in the input box must never be reported as sent"
    )


def test_plain_text_left_in_box_is_reported_as_failure() -> None:
    """Short messages that fail to submit must be caught too, not just pastes."""
    pane = _Pane(BOX_EMPTY, BOX_STUCK_PLAIN)
    assert _send(pane, STUCK_PLAIN_PAYLOAD) is False


def test_stuck_message_is_retried_with_another_enter() -> None:
    """Before giving up, send_input must press Enter again."""
    pane = _Pane(BOX_EMPTY, BOX_STUCK_PASTE)
    _send(pane, STUCK_PASTE_PAYLOAD)
    assert pane.enters >= 2, f"expected a retry Enter, saw {pane.enters} Enter press(es)"


def test_retry_that_lands_reports_success() -> None:
    """A message that goes through on the second Enter is a success, not a failure."""
    pane = _Pane(BOX_EMPTY, BOX_STUCK_PASTE, BOX_EMPTY)
    assert _send(pane, STUCK_PASTE_PAYLOAD) is True
    assert pane.enters >= 2


def test_submitted_message_reports_success_without_extra_enter() -> None:
    """The normal path: box empty after Enter → done, no second keypress."""
    pane = _Pane(BOX_EMPTY, BOX_EMPTY)
    assert _send(pane, LONG_MESSAGE) is True
    assert pane.enters == 1, "a message that submitted must not get a spurious extra Enter"


# --------------------------------------------------------------------------
# AC1 — let the TUI finish folding before Enter arrives
# --------------------------------------------------------------------------


def test_long_message_waits_before_pressing_enter() -> None:
    """A paste-sized payload gets a settle window between the text and Enter."""
    pane = _Pane(BOX_EMPTY, BOX_EMPTY)
    _send(pane, LONG_MESSAGE)
    from c_lord.tmux import _PASTE_SETTLE

    assert _PASTE_SETTLE in pane.slept, (  # type: ignore[attr-defined]
        f"expected a {_PASTE_SETTLE}s paste settle before Enter, slept={pane.slept}"  # type: ignore[attr-defined]
    )


# --------------------------------------------------------------------------
# AC5 — short / single-line messages must not regress
# --------------------------------------------------------------------------


def test_short_message_is_not_delayed_by_the_paste_settle() -> None:
    """Below the fold threshold the TUI never pastes, so don't pay the wait."""
    pane = _Pane(BOX_EMPTY, BOX_EMPTY)
    assert _send(pane, SHORT_MESSAGE) is True
    from c_lord.tmux import _PASTE_SETTLE

    assert _PASTE_SETTLE not in pane.slept, (  # type: ignore[attr-defined]
        "short messages must not pay the paste settle"
    )


def test_short_message_still_sends_text_then_enter() -> None:
    pane = _Pane(BOX_EMPTY, BOX_EMPTY)
    _send(pane, SHORT_MESSAGE)
    assert any(SHORT_MESSAGE in lit for lit in pane.literals)
    assert pane.enters == 1


# --------------------------------------------------------------------------
# Never claim failure on evidence we do not have
# --------------------------------------------------------------------------


def test_unreadable_pane_does_not_fabricate_a_failure() -> None:
    """If the input box can't be located, don't report a delivery failure.

    Same principle as #544: act on positive evidence only. A frame we cannot
    parse is not proof the message failed, and a false failure would tell the
    user their message was dropped when it went through.
    """
    pane = _Pane(BOX_EMPTY, "no status bar, no rules, nothing parseable\n")
    assert _send(pane, LONG_MESSAGE) is True


def test_failed_enter_keypress_is_reported() -> None:
    """A tmux-level Enter failure is still a hard failure."""

    class _EnterFails(_Pane):
        def __call__(self, args: list[str]) -> MagicMock:
            self.calls.append(list(args))
            if "capture-pane" in args:
                return MagicMock(returncode=0, stdout=BOX_EMPTY)
            if "send-keys" in args and "-l" not in args and args[-1] == "Enter":
                return MagicMock(returncode=1, stdout="", stderr="no server")
            return MagicMock(returncode=0, stdout="")

    assert _send(_EnterFails(), LONG_MESSAGE) is False


# --------------------------------------------------------------------------
# AC6 — the >16KB chunked path (#527) must get the same treatment
# --------------------------------------------------------------------------


def test_chunked_payload_settles_and_is_verified() -> None:
    """A payload split across many send-keys still settles and gets confirmed."""
    from c_lord.tmux import _PASTE_SETTLE, _chunk_for_send_keys

    huge = "\n".join(f"{j + 1}行目: " + "あ" * 400 for j in range(60))
    assert len(_chunk_for_send_keys(huge)) > 1, "fixture must actually be chunked"

    pane = _Pane(BOX_EMPTY, BOX_EMPTY)
    assert _send(pane, huge) is True
    assert _PASTE_SETTLE in pane.slept  # type: ignore[attr-defined]
    # Every chunk typed, then exactly one Enter, then the confirming capture.
    assert len(pane.literals) > 1
    assert pane.enters == 1
    assert sum(1 for c in pane.calls if "capture-pane" in c) >= 2


def test_chunked_payload_stuck_in_box_is_reported_as_failure() -> None:
    """AC6 + AC2: a chunked message that never submits must not read as success."""
    huge = STUCK_PASTE_PAYLOAD * 20
    pane = _Pane(BOX_EMPTY, BOX_STUCK_PASTE)
    assert _send(pane, huge) is False


# --------------------------------------------------------------------------
# AC3 — the user is told, and told something that does not destroy their message
# --------------------------------------------------------------------------


def test_input_box_holds_reports_a_stuck_message() -> None:
    """The manager can answer "is my text still in the box?" for the error path."""
    mgr = _mgr()
    pane = _Pane(BOX_STUCK_PASTE)
    with patch("c_lord.tmux._run", side_effect=pane):
        assert mgr.input_box_holds(12345, STUCK_PASTE_PAYLOAD) is True

    pane = _Pane(BOX_EMPTY)
    with patch("c_lord.tmux._run", side_effect=pane):
        assert mgr.input_box_holds(12345, STUCK_PASTE_PAYLOAD) is False


def test_stuck_message_error_does_not_tell_the_user_to_restart() -> None:
    """``/claude-restart`` would discard the text sitting in the input box.

    The #527 wording leads with it, which is right when the pane never took the
    input and wrong when it did — so the stuck case gets its own wording.
    """
    from c_lord.claude.tmux_runner import _delivery_failure, _stuck_in_input_box

    stuck = _stuck_in_input_box("あ" * 500)
    assert "入力欄に残っています" in stuck, "must say where the message actually is"
    assert "送り直す" in stuck, "must offer the action that does not lose the message"
    # It may mention /restart-claude, but only as the discouraged option.
    if "/claude-restart" in stuck:
        assert "破棄" in stuck, "if /restart-claude is mentioned, say what it costs"

    dead_pane = _delivery_failure("メッセージの送信", "あ" * 500)
    assert stuck != dead_pane, "the two failure modes must not share one message"
