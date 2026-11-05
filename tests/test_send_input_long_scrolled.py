"""A very long request must not be refused as "first part missing" (#1263).

A 5,909-character request (typed in four ~3,000-byte pieces) was judged twice
by the #872 guard to have lost its first part, so it was never sent: the user
got ❌ "tmux がこの入力を受け付けませんでした" and the orderer a ``failure``.

The first part had not been lost. Claude Code's input box has a maximum height
(15 rows in a 40-row pane, measured on 2.1.318) and scrolls to the cursor, so a
message taller than that shows only its last rows — the ``[Pasted text …]``
fold and the message's first characters are above the box's viewport. The
#872 guard read "end visible, start not" as "start lost".

The fixture is a real ``capture-pane -p -J`` frame from CLI 2.1.318 on an
isolated tmux server (``-L``, #701), 160x40 like c-lord's windows, idle after a
turn: the first piece typed in one burst (folded), the rest typed as text. The
message, delivered by Enter on the same CLI, reached the transcript whole.

#872's own failure (the box holds exactly the pieces after the first) must
still be refused — tests/test_send_input_head_lost.py stays green.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import TmuxSessionManager, _chunk_for_send_keys, _input_box_lost_head

_FIX = Path(__file__).parent / "fixtures" / "panes"

BOX_EMPTY = (_FIX / "input_box_empty.txt").read_text()
SCROLLED = (_FIX / "i1263_long_input_box_scrolled_v2_1_318.txt").read_text()
HEAD_LOST_872 = (_FIX / "input_box_head_lost.txt").read_text()

# The message the fixture was made from: 5,888 characters, 9,104 bytes, 4 pieces.
_LINE = (
    "c_lord/tmux.py の send_input を直す (see docs/specs/input-delivery.md)。"
    "長い依頼の本文で、指示を書いた行です。"
)
_TAIL = "最後に TAILMARK1263 と書いた。この依頼には ok とだけ返して"
_BODY = "".join(f"- 項目{i:03d}: {_LINE}\n" for i in range(140))
LONG_1263 = ("【HEADMARK1263 長い依頼の先頭】これは長い依頼の先頭です。\n" + _BODY)[: 5909 - 60] + (
    "\n" + _TAIL
)

# The #872 fixture's message (see tests/test_send_input_head_lost.py).
_LINE_872 = "試作の発注文。ふりがなを小さくしてセーフエリアに収める指示を詳しく書いた行です。"
LONG_872 = (
    "【HEADMARK872 長い依頼の先頭】これは長い依頼の先頭です。\n"
    + "".join(f"- 項目{i:02d}: {_LINE_872}\n" for i in range(34))
    + "最後に TAILMARK872 と書いた。この依頼には ok とだけ返して"
)

THREAD = 1263


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("c_lord.tmux.time.sleep"):
        yield


def test_fixture_message_is_typed_in_four_pieces() -> None:
    assert len(_chunk_for_send_keys(LONG_1263)) == 4


def test_a_scrolled_box_has_not_lost_the_head() -> None:
    """The box shows the message's last rows only — not evidence of a lost head."""
    assert _input_box_lost_head(SCROLLED, LONG_1263) is False


def test_the_872_failure_is_still_a_lost_head() -> None:
    assert _input_box_lost_head(HEAD_LOST_872, LONG_872) is True


class _Pane:
    def __init__(self, after_typing: str) -> None:
        self.after_typing = after_typing
        self.frame = BOX_EMPTY
        self.keys: list[list[str]] = []
        self._chunks_left = 0
        self.typings = 0

    def run(self, args: list[str]) -> MagicMock:
        if "capture-pane" in args:
            return MagicMock(returncode=0, stdout=self.frame, stderr="")
        if "send-keys" in args:
            self.keys.append(list(args))
            if "-l" in args:
                if self._chunks_left == 0:
                    self._chunks_left = len(_chunk_for_send_keys(LONG_1263))
                self._chunks_left -= 1
                if self._chunks_left == 0:
                    self.frame = self.after_typing
                    self.typings += 1
            elif "C-u" in args or "Enter" in args:
                self.frame = BOX_EMPTY
        return MagicMock(returncode=0, stdout="", stderr="")


def test_a_5909_character_request_is_sent() -> None:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]  # noqa: ARG005
    mgr._vim_mode["w1"] = False
    pane = _Pane(SCROLLED)
    with patch("c_lord.tmux._run", side_effect=pane.run):
        assert mgr.send_input(THREAD, LONG_1263) is True
    assert pane.typings == 1, "the message must be typed once, not cleared and retyped"
    assert not any("C-u" in k for k in pane.keys)
    assert sum(1 for k in pane.keys if "-l" not in k and k[-1] == "Enter") == 1
    assert mgr.take_send_failure(THREAD) is None
