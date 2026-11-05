"""A first piece that reached the box whole, but drawn in parts, is sent (#1283).

#1263 checks the first ~3,000-byte piece of a long message right after typing it
(:func:`_first_piece_state`). When the TUI draws that piece in parts instead of one
``[Pasted text …]`` fold, the check called it "lost" although every character was
in the box, cleared it, retyped it, got the same picture and refused the send. The
user saw the same ❌ "tmux がこの入力を受け付けませんでした" as #1263 itself, and
the request was never sent. Two ways the piece was seen to arrive in parts:

* ``i1283_first_piece_as_text``: the piece as text, taller than the box. The box is
  at its maximum height (15 rows in a 40-row pane) and shows only the piece's last
  rows, so its start cannot be seen — the same reason #1263 stopped looking for the
  start after the whole message.
* ``i1283_first_piece_fold_plus_text``: ``[Pasted text #19 +17 lines]`` and then the
  piece's remaining lines as text. The fold alone is 17 lines, the piece has 20;
  the 3 lines of text make up the difference.

Frames are real ``capture-pane -p -J`` captures (CLI 2.1.318, 160x40, isolated
``tmux -L``) taken by the reviewer who filed #1283, after the first piece was
written in 200-byte bursts (as text) or as 2,500 + 500 bytes (fold + text).

The refusals #1263 pins stay: only the end of the piece in a box that is *not*
full, a fold + text whose line counts do not add up, a stale fold.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import (
    TmuxSessionManager,
    _chunk_for_send_keys,
    _first_piece_state,
    _input_box_is_full,
)

_FIX = Path(__file__).parent / "fixtures" / "panes"
AS_TEXT = (_FIX / "i1283_first_piece_as_text_v2_1_318.txt").read_text()
FOLD_PLUS_TEXT = (_FIX / "i1283_first_piece_fold_plus_text_v2_1_318.txt").read_text()
BOX_EMPTY = (_FIX / "input_box_empty.txt").read_text()

_LINE = (
    "c_lord/tmux.py の send_input を直す (see docs/specs/input-delivery.md)。"
    "長い依頼の本文で、指示を書いた行です。"
)
_TAIL = "最後に TAILMARK1263 と書いた。この依頼には ok とだけ返して"
_BODY = "".join(f"- 項目{i:03d}: {_LINE}\n" for i in range(140))
LONG = ("【HEADMARK1263 長い依頼の先頭】これは長い依頼の先頭です。\n" + _BODY)[: 5909 - 60] + (
    "\n" + _TAIL
)
PIECE = _chunk_for_send_keys(LONG)[0]


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("c_lord.tmux.time.sleep"):
        yield


def _box_rows(frame: str, keep: int) -> str:
    """*frame* with only the last *keep* rows of its input box left.

    The pane keeps its height: the box shrinks and blank rows fill in above it,
    as the TUI draws a shorter box.
    """
    lines = frame.splitlines()
    rules = [i for i, line in enumerate(lines) if line.startswith("────")]
    top, bottom = rules[-2], rules[-1]
    rows = lines[top + 1 : bottom]
    kept = rows[-keep:]
    kept[0] = "❯ " + kept[0][2:]
    pad = [""] * (len(rows) - len(kept))
    return "\n".join([*lines[:top], *pad, lines[top], *kept, *lines[bottom:]]) + "\n"


def _with_fold_count(frame: str, count: int) -> str:
    return frame.replace("[Pasted text #19 +17 lines]", f"[Pasted text #19 +{count} lines]")


@pytest.mark.parametrize("frame", [AS_TEXT, FOLD_PLUS_TEXT], ids=["as-text", "fold+text"])
def test_a_whole_first_piece_split_across_reads_is_not_lost(frame: str) -> None:
    assert _first_piece_state(frame, PIECE, BOX_EMPTY) != "lost"


@pytest.mark.parametrize("frame", [AS_TEXT, FOLD_PLUS_TEXT], ids=["as-text", "fold+text"])
def test_the_request_is_sent_when_the_first_piece_arrives_split(frame: str) -> None:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]  # noqa: ARG005
    mgr._vim_mode["w1"] = False
    state = {"frame": BOX_EMPTY}

    def run(args: list[str]) -> MagicMock:
        if "capture-pane" in args:
            return MagicMock(returncode=0, stdout=state["frame"], stderr="")
        if "-l" in args and args[-1] == PIECE:
            state["frame"] = frame
        elif "C-u" in args or "Enter" in args:
            state["frame"] = BOX_EMPTY
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("c_lord.tmux._run", side_effect=run):
        assert mgr.send_input(1283, LONG) is True
    assert mgr.take_send_failure(1283) is None


# ── the refusals #1263 pins still hold ─────────────────────────────────────


@pytest.mark.parametrize("keep", [1, 3, 14])
def test_only_the_end_of_the_piece_in_a_box_that_is_not_full_is_lost(keep: int) -> None:
    """The box's top row is visible when it is below its maximum height, so a
    piece whose start is not there really did lose it."""
    assert _first_piece_state(_box_rows(AS_TEXT, keep), PIECE, BOX_EMPTY) == "lost"


def test_a_full_box_with_a_replacement_character_is_lost() -> None:
    assert _first_piece_state(AS_TEXT.replace("項目012", "項目�"), PIECE, BOX_EMPTY) == "lost"


def test_a_full_box_that_is_not_the_end_of_the_piece_is_not_landed() -> None:
    """A full box of something else (a human's own long text) is no evidence."""
    other = AS_TEXT.replace("長い依頼の本文で", "別の文章の本文で")
    assert _first_piece_state(other, PIECE, BOX_EMPTY) != "landed"


@pytest.mark.parametrize("count", [14, 16, 18])
def test_a_fold_and_text_that_do_not_add_up_are_lost(count: int) -> None:
    assert _first_piece_state(_with_fold_count(FOLD_PLUS_TEXT, count), PIECE, BOX_EMPTY) == "lost"


def test_a_stale_fold_with_the_text_does_not_count() -> None:
    """The fold was there before typing; only the piece's last lines are new."""
    assert _first_piece_state(FOLD_PLUS_TEXT, PIECE, FOLD_PLUS_TEXT) != "landed"


def test_fold_and_text_that_is_not_the_end_of_the_piece_is_lost() -> None:
    other = FOLD_PLUS_TEXT.replace("項目018", "項目0X8")
    assert _first_piece_state(other, PIECE, BOX_EMPTY) == "lost"


# ── the box's maximum height (CLI 2.1.318, isolated tmux, 60 lines typed) ──

_FULL = {
    "h24": (_FIX / "i1283_box_full_h24_v2_1_318.txt").read_text(),
    "h40": AS_TEXT,
    "h60": (_FIX / "i1283_box_full_h60_v2_1_318.txt").read_text(),
}


@pytest.mark.parametrize("frame", list(_FULL.values()), ids=list(_FULL))
def test_a_box_at_its_maximum_height_is_full(frame: str) -> None:
    assert _input_box_is_full(frame) is True


@pytest.mark.parametrize("frame", list(_FULL.values()), ids=list(_FULL))
def test_a_box_one_row_short_of_its_maximum_is_not_full(frame: str) -> None:
    lines = frame.splitlines()
    rows = [i for i, line in enumerate(lines) if line.startswith("────")]
    assert _input_box_is_full(_box_rows(frame, rows[-1] - rows[-2] - 2)) is False


def test_short_boxes_are_not_full() -> None:
    assert _input_box_is_full(BOX_EMPTY) is False
    assert _input_box_is_full(FOLD_PLUS_TEXT) is False
