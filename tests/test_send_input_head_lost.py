"""A long message must not be submitted with its first part missing (#872).

c-lord types a message longer than one ``send-keys`` allows in pieces of about
3,000 bytes (#527). In production (CLI 2.1.283, panes that had been idle for
20–80 minutes) the first piece twice never reached the input box: Claude was
handed only the second piece — 342 of 1,660 characters, starting in the middle
of a sentence — and started working on it. The cause in the CLI is not known
(not reproduced on an isolated tmux with 2.1.283 / 2.1.294 / 2.1.295, fresh or
idle), so the guard works from what the pane shows, whatever the cause:

* the first piece is ~3,000 bytes typed in one burst, which the TUI always folds
  into a ``[Pasted text #N +M lines]`` placeholder; the pieces after it follow
  as text. So before Enter, a multi-piece message must show the placeholder
  (or, should a CLI stop folding, its own first characters) in the box.
* a box holding the message's *end* but neither of those is the failure: the
  first piece is gone. Clear the box and type the message again, once. If it is
  still wrong, clear it and report a delivery failure — never press Enter on it.

Fixtures are real ``capture-pane -p`` frames from CLI 2.1.295 on an isolated
tmux server (#701): the normal pre-Enter state of a two-piece message, and the
same box holding only the second piece (the production failure).
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import TmuxSessionManager, _chunk_for_send_keys

_FIX = Path(__file__).parent / "fixtures" / "panes"

BOX_EMPTY = (_FIX / "input_box_empty.txt").read_text()
PASTED = (_FIX / "input_box_multichunk_pasted.txt").read_text()
HEAD_LOST = (_FIX / "input_box_head_lost.txt").read_text()

# The message both fixtures were made from (two pieces: 1,110 + 628 chars).
_LINE = "試作の発注文。ふりがなを小さくしてセーフエリアに収める指示を詳しく書いた行です。"
BODY = "".join(f"- 項目{i:02d}: {_LINE}\n" for i in range(34))
LONG = (
    "【HEADMARK872 長い依頼の先頭】これは長い依頼の先頭です。\n"
    + BODY
    + "最後に TAILMARK872 と書いた。この依頼には ok とだけ返して"
)

THREAD = 872


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
    """The box shows ``after_typing[n]`` once the n-th full typing has landed."""

    def __init__(self, *after_typing: str) -> None:
        self.after_typing = list(after_typing)
        self.frame = BOX_EMPTY
        self.typings = 0
        self.keys: list[list[str]] = []
        self._chunks_left = 0

    def run(self, args: list[str]) -> MagicMock:
        if "capture-pane" in args:
            return MagicMock(returncode=0, stdout=self.frame, stderr="")
        if "send-keys" in args:
            self.keys.append(list(args))
            if "-l" in args:
                if self._chunks_left == 0:
                    self._chunks_left = len(_chunk_for_send_keys(LONG))
                self._chunks_left -= 1
                if self._chunks_left == 0:
                    index = min(self.typings, len(self.after_typing) - 1)
                    self.frame = self.after_typing[index]
                    self.typings += 1
            elif "C-u" in args or "Enter" in args:
                self.frame = BOX_EMPTY
        return MagicMock(returncode=0, stdout="", stderr="")

    @property
    def enters(self) -> int:
        return sum(1 for k in self.keys if "-l" not in k and k[-1] == "Enter")

    def index_of(self, pred, start: int = 0) -> int | None:  # noqa: ANN001
        return next((i for i, k in enumerate(self.keys) if i >= start and pred(k)), None)


def test_fixture_message_is_typed_in_two_pieces() -> None:
    assert len(_chunk_for_send_keys(LONG)) == 2


def test_a_lost_first_piece_is_typed_again_before_enter(caplog) -> None:  # noqa: ANN001
    pane = _Pane(HEAD_LOST, PASTED)
    with caplog.at_level(logging.WARNING), patch("c_lord.tmux._run", side_effect=pane.run):
        assert _mgr().send_input(THREAD, LONG) is True

    assert pane.typings == 2, "the message must be typed a second time"
    clear = pane.index_of(lambda k: "C-u" in k)
    enter = pane.index_of(lambda k: k[-1] == "Enter" and "-l" not in k)
    assert clear is not None and enter is not None and clear < enter
    assert pane.enters == 1
    assert any("#872" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


def test_a_first_piece_lost_twice_is_a_delivery_failure_not_a_half_message() -> None:
    pane = _Pane(HEAD_LOST, HEAD_LOST)
    mgr = _mgr()
    with patch("c_lord.tmux._run", side_effect=pane.run):
        assert mgr.send_input(THREAD, LONG) is False

    assert pane.enters == 0, "Enter must never submit the second half on its own"
    last_typing = max(i for i, k in enumerate(pane.keys) if "-l" in k)
    assert pane.index_of(lambda k: "C-u" in k, start=last_typing) is not None, (
        "the half message must not be left in the box to join the next one"
    )
    assert mgr.take_send_failure(THREAD)


def test_a_normal_long_message_is_typed_once() -> None:
    pane = _Pane(PASTED)
    with patch("c_lord.tmux._run", side_effect=pane.run):
        assert _mgr().send_input(THREAD, LONG) is True
    assert pane.typings == 1
    assert not any("C-u" in k for k in pane.keys)
    assert pane.enters == 1


def test_an_unreadable_box_is_not_treated_as_a_lost_piece() -> None:
    """Positive evidence only (#544): a frame without an input box proves nothing."""
    pane = _Pane("no input box drawn in this frame")
    with patch("c_lord.tmux._run", side_effect=pane.run):
        _mgr().send_input(THREAD, LONG)
    assert pane.typings == 1
    assert not any("C-u" in k for k in pane.keys)
