"""A message sent after an interrupt must not carry the interrupted prompt (#879).

Claude Code puts the prompt of a turn it stopped on Ctrl+C **back into the
input box** when the turn had not produced anything yet (the turn is rewound —
it does not even reach the transcript). c-lord used to type the next message
straight after it, so Claude received ``<interrupted prompt><new message>``
with no separator: the instruction the user meant to take back ran anyway, and
the joined text no longer matched ``pane_echo`` so it came back with a 👤.

The pane fixtures are real ``capture-pane -p`` captures from Claude Code
v2.1.295, taken 1.5s after a Ctrl+C on an isolated tmux server (#701):

* ``interrupt_restored_prompt.txt`` — a two-line prompt put back as text
* ``interrupt_restored_paste.txt`` — a long prompt put back as its
  ``[Pasted text #N +M lines]`` fold
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord import tmux as tmux_mod
from c_lord.tmux import TmuxSessionManager

_FIX = Path(__file__).parent / "fixtures" / "panes"

RESTORED_PROMPT = (_FIX / "interrupt_restored_prompt.txt").read_text()
RESTORED_PASTE = (_FIX / "interrupt_restored_paste.txt").read_text()
BOX_EMPTY = (_FIX / "input_box_empty.txt").read_text()

# What sits in RESTORED_PROMPT's input box.
FIRST = "> 一通目の引用です。\nBash で sleep 20 を実行してから ok と返して"
# What sits, folded, in RESTORED_PASTE's input box.
LONG_FIRST = (
    "長文の先頭です。"
    + "".join(f"- 箇条 {i}: これは試験用の文です\n" for i in range(40))
    + "Bash で sleep 20 を実行してから ok と返して"
)
SECOND = "二通目です"

THREAD = 4242


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("c_lord.tmux.time.sleep"):
        yield


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]  # noqa: ARG005
    mgr._vim_mode["w1"] = False
    return mgr


class _Pane:
    """A pane whose input box holds *restored* until the box is cleared.

    ``clears_needed`` C-u presses empty it; ``None`` means it never empties.
    After the new message is typed (and Enter pressed) the box is empty again,
    so the #560 submit check sees a delivered message.
    """

    def __init__(self, restored: str, clears_needed: int | None = 1) -> None:
        self.frame = restored
        self.clears_needed = clears_needed
        self.clears = 0
        self.keys: list[list[str]] = []

    def run(self, args: list[str]) -> MagicMock:
        if "capture-pane" in args:
            return MagicMock(returncode=0, stdout=self.frame, stderr="")
        if "send-keys" in args:
            self.keys.append(list(args))
            if "C-u" in args:
                self.clears += args.count("C-u")
                if self.clears_needed is not None and self.clears >= self.clears_needed:
                    self.frame = BOX_EMPTY
            if "Enter" in args:
                self.frame = BOX_EMPTY
        return MagicMock(returncode=0, stdout="", stderr="")

    def typed(self) -> list[str]:
        return [k[-1] for k in self.keys if "-l" in k]

    def index_of(self, pred) -> int | None:  # noqa: ANN001
        return next((i for i, k in enumerate(self.keys) if pred(k)), None)


def _send_first(mgr: TmuxSessionManager, text: str) -> None:
    """Deliver *text* on an empty pane, then interrupt the turn it started."""
    pane = _Pane(BOX_EMPTY)
    with patch("c_lord.tmux._run", side_effect=pane.run):
        assert mgr.send_input(THREAD, text)
        assert mgr.send_interrupt(THREAD)


def test_restored_prompt_is_cleared_before_the_next_message() -> None:
    """AC1: the second message reaches Claude alone."""
    mgr = _mgr()
    _send_first(mgr, FIRST)

    pane = _Pane(RESTORED_PROMPT, clears_needed=2)
    with patch("c_lord.tmux._run", side_effect=pane.run):
        assert mgr.send_input(THREAD, SECOND)

    clear = pane.index_of(lambda k: "C-u" in k)
    text = pane.index_of(lambda k: "-l" in k)
    assert clear is not None, "the restored prompt must be cleared (#879)"
    assert text is not None
    assert clear < text, "the box must be cleared BEFORE the new message is typed"
    assert pane.typed() == [SECOND], "only the new message is typed"


def test_restored_paste_placeholder_is_cleared() -> None:
    """A long interrupted prompt comes back as ``[Pasted text …]``; that goes too."""
    mgr = _mgr()
    _send_first(mgr, LONG_FIRST)

    pane = _Pane(RESTORED_PASTE)
    with patch("c_lord.tmux._run", side_effect=pane.run):
        assert mgr.send_input(THREAD, SECOND)

    clear = pane.index_of(lambda k: "C-u" in k)
    text = pane.index_of(lambda k: "-l" in k)
    assert clear is not None and text is not None and clear < text
    assert pane.typed() == [SECOND]


def test_nothing_restored_means_nothing_is_cleared() -> None:
    """AC2: a turn that had started answering leaves the box empty — touch nothing."""
    mgr = _mgr()
    _send_first(mgr, FIRST)

    pane = _Pane(BOX_EMPTY)
    with patch("c_lord.tmux._run", side_effect=pane.run):
        assert mgr.send_input(THREAD, SECOND)

    assert not any("C-u" in k or "BSpace" in k for k in pane.keys)
    assert pane.typed() == [SECOND], "the message must arrive whole"


def test_unrelated_text_in_the_box_is_left_alone() -> None:
    """Only c-lord's own earlier prompt is evidence enough to delete text."""
    mgr = _mgr()
    _send_first(mgr, "全然別の依頼でした。これは入力欄にありません")

    pane = _Pane(RESTORED_PROMPT)
    with patch("c_lord.tmux._run", side_effect=pane.run):
        assert mgr.send_input(THREAD, SECOND)

    assert not any("C-u" in k for k in pane.keys)


def test_a_box_that_will_not_clear_fails_the_send_instead_of_joining() -> None:
    """Typing behind text we could not remove is the bug itself — report it."""
    mgr = _mgr()
    _send_first(mgr, FIRST)

    pane = _Pane(RESTORED_PROMPT, clears_needed=None)
    with patch("c_lord.tmux._run", side_effect=pane.run):
        assert mgr.send_input(THREAD, SECOND) is False

    assert pane.typed() == [], "nothing may be typed behind the stale prompt"
    assert mgr.take_send_failure(THREAD), "the failure must carry a reason"


def test_start_claude_prompt_counts_as_c_lords_own() -> None:
    """The first turn's prompt rides on the command line; it can be put back too."""
    tmux_mod._remember_typed(THREAD, FIRST)
    mgr = _mgr()
    pane = _Pane(RESTORED_PROMPT, clears_needed=2)
    with patch("c_lord.tmux._run", side_effect=pane.run):
        assert mgr.send_input(THREAD, SECOND)
    clear = pane.index_of(lambda k: "C-u" in k)
    text = pane.index_of(lambda k: "-l" in k)
    assert clear is not None and text is not None and clear < text
    assert pane.typed() == [SECOND]


def test_send_waits_for_the_interrupt_to_put_the_prompt_back() -> None:
    """The CLI restores the prompt a moment after Ctrl+C; do not look before that."""
    mgr = _mgr()
    _send_first(mgr, FIRST)

    pane = _Pane(BOX_EMPTY)
    with (
        patch("c_lord.tmux._run", side_effect=pane.run),
        patch("c_lord.tmux.time.sleep") as sleep,
        patch("c_lord.tmux.time.monotonic", return_value=tmux_mod._interrupted_at[THREAD]),
    ):
        assert mgr.send_input(THREAD, SECOND)

    waits = [c.args[0] for c in sleep.call_args_list]
    assert any(w >= tmux_mod._INTERRUPT_RESTORE_SETTLE - 1e-6 for w in waits)
