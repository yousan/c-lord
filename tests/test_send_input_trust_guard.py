"""send_input must ANSWER an open folder-trust dialog, never type into it (#716).

The delivery guard added in #485 dismisses an open menu with Esc before typing,
so a plain reply can never be read as a menu selection.  It only looks for
AskUserQuestion and plan-approval menus, and the folder-trust dialog is neither.

That blind spot is expensive because of *when* the dialog is up: a fresh session
dir opens it on the very first launch and it stays up for a median of 5 seconds
(p90 8s, max 27s, measured over 93 production starts).  A second message landing
inside that window is typed straight onto the dialog — and the current dialog's
cursor starts on ``❯ No, exit``, so the trailing Enter DECLINES trust and
``claude`` exits without running a single turn.  The corpse pane no longer
matches ``_has_trust_prompt``, so nothing downstream notices either: the thread
sits for 120s and reports "Claude exited without producing a response".  Twice
this week, unnoticed for 29 and 71 minutes.

Esc is not the fix here — Esc cancels the dialog, which also exits ``claude``.
The dialog has to be *approved* ("Yes, I trust this folder") before the message
is typed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager

_FIX = Path(__file__).parent / "fixtures" / "panes"

_MESSAGE = "これは起動直後に届いた2通目です。ダイアログに打ち込まれてはいけません"


def _fixture(name: str) -> str:
    return (_FIX / f"{name}.txt").read_text()


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]
    return mgr


class _Pane:
    """A tmux pane that reacts to keystrokes the way the real dialog does.

    ``Down`` moves the cursor onto "Yes, I trust this folder"; ``Enter`` confirms
    whatever the cursor is on.  Confirming "Yes" boots the TUI (the pane becomes
    a normal empty input box); confirming "No" — or an ``Escape`` — kills claude
    and leaves the declined corpse behind, which is the bug being fixed.
    """

    def __init__(self, start: str = "trust_prompt_live_cursor_on_no", *, steerable: bool = True):
        self._pane = _fixture(start)
        self._steerable = steerable
        self.keys: list[str] = []
        self.typed: list[str] = []
        self.claude_alive = True

    def run(self, args: list[str]) -> MagicMock:
        if "capture-pane" in args:
            return MagicMock(returncode=0, stdout=self._pane, stderr="")
        if "send-keys" in args:
            if "-l" in args:
                self.typed.append(args[-1])
            else:
                for key in args[args.index("-t") + 2 :]:
                    self._press(key)
        return MagicMock(returncode=0, stdout="", stderr="")

    def _press(self, key: str) -> None:
        self.keys.append(key)
        on_yes = "❯ Yes, I trust this folder" in self._pane or "❯ 1. Yes" in self._pane
        if key == "Down" and self._steerable:
            self._pane = _fixture("trust_prompt_unnumbered_cursor_on_yes")
        elif key == "Escape":
            self.claude_alive = False
            self._pane = _fixture("trust_prompt_declined_corpse")
        elif key == "Enter":
            if on_yes or "Yes, I trust this folder" not in self._pane:
                self._pane = _fixture("input_box_empty")
            else:
                self.claude_alive = False
                self._pane = _fixture("trust_prompt_declined_corpse")


def _send(pane: _Pane, text: str = _MESSAGE) -> bool:
    with (
        patch("c_lord.tmux._run", side_effect=pane.run),
        patch("c_lord.tmux._TRUST_NAV_DELAY", 0.0),
        patch("c_lord.tmux._TRUST_SETTLE", 0.0),
        patch("c_lord.tmux._MENU_DISMISS_SETTLE", 0.0),
        patch("c_lord.tmux._SUBMIT_SETTLE", 0.0),
        patch("c_lord.tmux._PASTE_SETTLE", 0.0),
    ):
        return _mgr().send_input(12345, text)


def test_trust_dialog_is_approved_before_the_message_is_typed() -> None:
    """AC1: the guard sees the dialog, steers onto "Yes" and confirms it."""
    pane = _Pane()

    ok = _send(pane)

    assert "Down" in pane.keys, (
        "the cursor starts on 'No, exit' — without a Down the confirm declines "
        "trust and claude exits (#716)"
    )
    assert "Enter" in pane.keys
    assert pane.keys.index("Down") < pane.keys.index("Enter")
    assert pane.claude_alive, "the dialog was answered 'No' — claude is gone"
    assert pane.typed, "the message must still be delivered after the dialog closes"
    assert ok is True


def test_the_message_is_never_typed_onto_the_open_dialog() -> None:
    """AC3: typing lands only after the dialog has been confirmed."""
    pane = _Pane()

    _send(pane)

    assert pane.typed, "the message must be delivered"
    # Every keystroke that answered the dialog comes first; the literal text is
    # typed only once the pane is a real input box.
    assert pane.keys[: pane.keys.index("Enter") + 1] == ["Down", "Enter"], pane.keys


def test_the_trust_dialog_is_never_dismissed_with_escape() -> None:
    """Esc cancels the dialog, which exits claude — the #485 reflex is wrong here."""
    pane = _Pane()

    _send(pane)

    assert "Escape" not in pane.keys, (
        "Esc on the folder-trust dialog exits claude; the dialog must be "
        "approved, not dismissed (#716)"
    )


def test_enter_is_withheld_when_the_cursor_will_not_leave_no_exit() -> None:
    """#684's discipline, applied on the delivery path.

    A dialog that cannot be steered must NOT be confirmed: that Enter is the
    keystroke that kills the session.  Report a delivery failure instead, so the
    thread says so rather than dying silently two minutes later.
    """
    pane = _Pane(steerable=False)

    ok = _send(pane)

    assert "Enter" not in pane.keys, "confirming here declines trust (#684)"
    assert not pane.typed, "the message must not be typed onto a dialog we cannot close"
    assert pane.claude_alive
    assert ok is False, "an undeliverable message must be reported, not silently dropped"


def test_the_numbered_dialog_takes_a_bare_enter() -> None:
    """Regression: the pre-2.1.248 dialog already has the cursor on "Yes"."""
    pane = _Pane("trust_prompt_live_v2_1_252")

    _send(pane)

    assert "Down" not in pane.keys, pane.keys
    assert pane.keys[0] == "Enter", pane.keys
    assert pane.typed


def test_a_declined_corpse_is_not_mistaken_for_an_open_dialog() -> None:
    """#630's liveness rule holds: a dead pane gets no keystrokes of its own."""
    pane = _Pane("trust_prompt_declined_corpse")

    _send(pane)

    assert "Down" not in pane.keys, pane.keys
    assert pane.typed, "delivery behaviour on a corpse pane is unchanged (#527 handles it)"


def test_a_normal_pane_gets_no_trust_keystrokes() -> None:
    """No spurious Down/Enter on the overwhelmingly common case."""
    pane = _Pane("input_box_empty")

    _send(pane)

    assert "Down" not in pane.keys, pane.keys
    assert pane.typed
