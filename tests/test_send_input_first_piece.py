"""The first piece of a long message is checked before the rest is typed (#872, #1263).

c-lord types a message longer than one ``send-keys`` in pieces of ~3,000 bytes
(#527). #872: in production the first piece twice never reached the input box,
Enter submitted the rest, and Claude started on half a sentence. #1263: the
guard then looked for the message's start in the box *after* every piece was
typed — but the box has a maximum height (15 rows in a 40-row pane, CLI
2.1.318) and scrolls, so any message taller than that "had lost its head" and a
5,909-character request could not be sent at all.

So the check moved to the one moment the box is small: right after the first
piece. It then holds either ``[Pasted text #N +M lines]`` — where M is the
piece's own newline count (measured: piece of 22 newlines → ``+22 lines``) — or
the piece's own first characters. Anything else (empty, only the end of the
piece, a U+FFFD, a fold of the wrong size) means the piece did not land whole:
clear the box, type it once more, and if it is still wrong, fail the send
without ever typing the rest or pressing Enter.

Frames: ``i1263_after_first_piece_*`` are real ``capture-pane -p -J`` frames
taken on an isolated tmux server (``-L``, #701) with CLI 2.1.318, right after
c-lord's first ``send-keys -l`` of each message. The "wrong box" frames are
those real frames with only the box line replaced (the CLI was not made to
lose text on purpose). ``input_box_head_lost`` and
``i1263_long_input_box_scrolled_v2_1_318`` are the #872 and #1263 frames.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import TmuxSessionManager, _chunk_for_send_keys, _first_piece_state

_FIX = Path(__file__).parent / "fixtures" / "panes"


def _frame(name: str) -> str:
    return (_FIX / f"{name}.txt").read_text()


BOX_EMPTY = _frame("input_box_empty")
P1_FOLD_872 = _frame("i1263_after_first_piece_872_v2_1_318")
P1_FOLD_1263 = _frame("i1263_after_first_piece_1263_v2_1_318")
HEAD_LOST_872 = _frame("input_box_head_lost")
PASTED_872 = _frame("input_box_multichunk_pasted")
SCROLLED_1263 = _frame("i1263_long_input_box_scrolled_v2_1_318")
# Idle boxes that are not empty to capture-pane: a greyed hint or a suggestion.
VIM_HINT = _frame("vim_enabled_insert")  # ❯ Try "refactor <filepath>"
SUGGEST_2026 = _frame("i858_input_box_suggestion_2026_v2_1_283")
SUGGEST_833 = _frame("i858_input_box_suggestion_8_33_v2_1_283")

_LINE_872 = "試作の発注文。ふりがなを小さくしてセーフエリアに収める指示を詳しく書いた行です。"
LONG_872 = (
    "【HEADMARK872 長い依頼の先頭】これは長い依頼の先頭です。\n"
    + "".join(f"- 項目{i:02d}: {_LINE_872}\n" for i in range(34))
    + "最後に TAILMARK872 と書いた。この依頼には ok とだけ返して"
)
_LINE_1263 = (
    "c_lord/tmux.py の send_input を直す (see docs/specs/input-delivery.md)。"
    "長い依頼の本文で、指示を書いた行です。"
)
_TAIL_1263 = "最後に TAILMARK1263 と書いた。この依頼には ok とだけ返して"
LONG_1263 = (
    "【HEADMARK1263 長い依頼の先頭】これは長い依頼の先頭です。\n"
    + "".join(f"- 項目{i:03d}: {_LINE_1263}\n" for i in range(140))
)[: 5909 - 60] + ("\n" + _TAIL_1263)

THREAD = 1263


def _with_box(frame: str, box: str) -> str:
    """*frame* with its input-box line replaced by ``❯ <box>``."""
    lines = frame.splitlines()
    i = next(i for i, line in enumerate(lines) if line.startswith("❯"))
    lines[i] = "❯ " + box
    return "\n".join(lines) + "\n"


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
    """A pane whose box shows ``after_first[n]`` once the first piece is typed
    for the n-th time, and ``after_rest`` once the message's last piece is.

    An ``after_first`` entry may be a list: one frame per capture, the last one
    staying — a slow TUI whose fold appears only on a later look. ``idle`` is
    what the box shows once emptied (an Enter or the clearing keys).
    """

    def __init__(
        self,
        payload: str,
        after_first: list,  # list[str | list[str]]
        after_rest: str,
        *,
        idle: str = BOX_EMPTY,
        initial: str | None = None,
    ) -> None:
        self.chunks = _chunk_for_send_keys(payload)
        self.after_first = after_first
        self.after_rest = after_rest
        self.idle = idle
        self.frames: list[str] = [initial if initial is not None else idle]
        self.first_typings = 0
        self.keys: list[list[str]] = []

    def _show(self, frame) -> None:  # noqa: ANN001
        self.frames = list(frame) if isinstance(frame, list) else [frame]

    def run(self, args: list[str]) -> MagicMock:
        if "capture-pane" in args:
            frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
            return MagicMock(returncode=0, stdout=frame, stderr="")
        if "send-keys" in args:
            self.keys.append(list(args))
            if "-l" in args:
                text = args[-1]
                if text == self.chunks[0]:
                    index = min(self.first_typings, len(self.after_first) - 1)
                    self._show(self.after_first[index])
                    self.first_typings += 1
                if text == self.chunks[-1]:
                    self._show(self.after_rest)
            elif "C-u" in args or "Enter" in args:
                self._show(self.idle)
        return MagicMock(returncode=0, stdout="", stderr="")

    @property
    def enters(self) -> int:
        return sum(1 for k in self.keys if "-l" not in k and k[-1] == "Enter")

    def typed(self, text: str) -> int:
        return sum(1 for k in self.keys if "-l" in k and k[-1] == text)


def _send(payload: str, after_first: list, after_rest: str, **kw):  # noqa: ANN202, ANN003
    pane = _Pane(payload, after_first, after_rest, **kw)
    mgr = _mgr()
    with patch("c_lord.tmux._run", side_effect=pane.run):
        sent = mgr.send_input(THREAD, payload)
    return sent, pane, mgr


# The hint line seen in this fix's own captures (CLI 2.1.318, isolated tmux).
HINT_1263 = _with_box(P1_FOLD_872, 'Try "edit <filepath> to..."')
IDLE_FRAMES = [HINT_1263, VIM_HINT, SUGGEST_2026, SUGGEST_833]
IDLE_IDS = ["hint-2.1.318", "vim-hint", "i858-2026", "i858-8.33"]


def _assert_refused(sent: bool, pane: _Pane, mgr: TmuxSessionManager) -> None:
    assert sent is False
    assert pane.enters == 0, "Enter must never submit a message whose first piece is damaged"
    for later in pane.chunks[1:]:
        assert pane.typed(later) == 0, "the rest must not be typed behind a damaged first piece"
    last_typing = max(i for i, k in enumerate(pane.keys) if "-l" in k)
    assert any("C-u" in k for k in pane.keys[last_typing:]), "the damaged piece must be cleared"
    assert mgr.take_send_failure(THREAD)


# ── delivered ─────────────────────────────────────────────────────────────


def test_the_5909_character_request_is_delivered() -> None:
    """#1263: the box after the last piece shows only its tail — that is fine."""
    sent, pane, _ = _send(LONG_1263, [P1_FOLD_1263], SCROLLED_1263)
    assert sent is True
    assert pane.first_typings == 1
    assert not any("C-u" in k for k in pane.keys)
    assert pane.enters == 1


def test_a_two_piece_message_is_delivered() -> None:
    sent, pane, _ = _send(LONG_872, [P1_FOLD_872], PASTED_872)
    assert sent is True
    assert pane.first_typings == 1
    assert pane.enters == 1


def test_a_box_whose_top_row_starts_at_a_piece_boundary_is_not_refused() -> None:
    """Review L1: the final box holding exactly pieces 2..n proves nothing once
    the first piece was seen to land."""
    sent, pane, _ = _send(LONG_872, [P1_FOLD_872], HEAD_LOST_872)
    assert sent is True
    assert pane.first_typings == 1
    assert pane.enters == 1


def test_a_lost_first_piece_is_typed_again_and_then_delivered() -> None:
    sent, pane, _ = _send(LONG_872, [BOX_EMPTY, P1_FOLD_872], PASTED_872)
    assert sent is True
    assert pane.first_typings == 2
    assert pane.typed(pane.chunks[1]) == 1, "the rest is typed once, after the first piece"
    assert pane.enters == 1


# ── refused (#872 and the review's partial losses) ───────────────────────


def test_a_first_piece_lost_twice_is_refused() -> None:
    """#872 itself."""
    _assert_refused(*_send(LONG_872, [BOX_EMPTY], HEAD_LOST_872))


def test_a_lost_head_with_a_rest_taller_than_the_box_is_refused() -> None:
    """Review M2: the rest scrolls the box, so only the early check can see it."""
    _assert_refused(*_send(LONG_1263, [BOX_EMPTY], SCROLLED_1263))


@pytest.mark.parametrize("kept", [1, 5, 50])
def test_only_the_end_of_the_first_piece_arriving_is_refused(kept: int) -> None:
    """Review M1: the last *kept* characters of piece 1 arrived, the rest did not."""
    piece = _chunk_for_send_keys(LONG_1263)[0]
    box = _with_box(P1_FOLD_1263, piece[-kept:].replace("\n", " "))
    _assert_refused(*_send(LONG_1263, [box], SCROLLED_1263))


def test_a_replacement_character_where_the_first_piece_should_be_is_refused() -> None:
    """Review M1: a multibyte character cut at a boundary shows as U+FFFD."""
    _assert_refused(*_send(LONG_1263, [_with_box(P1_FOLD_1263, "�")], SCROLLED_1263))


def test_a_lost_first_piece_with_an_invisible_char_dropped_from_the_rest_is_refused() -> None:
    """Review M1: an exact comparison of the rest is defeated by one dropped
    invisible character; the early check does not look at the rest at all."""
    chunks = _chunk_for_send_keys(LONG_872)
    payload = chunks[0] + chunks[1][:10] + "⁠" + chunks[1][10:]
    _assert_refused(*_send(payload, [BOX_EMPTY], HEAD_LOST_872))


def test_a_fold_of_the_wrong_size_is_refused() -> None:
    """Half of piece 1 folded is still a lost head: the fold names its line count."""
    box = _with_box(P1_FOLD_872, "[Pasted text #2 +9 lines]")
    _assert_refused(*_send(LONG_872, [box], PASTED_872))


# ── the classifier on real frames ─────────────────────────────────────────


def test_classifier_on_real_frames() -> None:
    p872 = _chunk_for_send_keys(LONG_872)[0]
    p1263 = _chunk_for_send_keys(LONG_1263)[0]
    assert _first_piece_state(P1_FOLD_872, p872) == "landed"
    assert _first_piece_state(P1_FOLD_1263, p1263) == "landed"
    assert _first_piece_state(P1_FOLD_872, p1263) == "lost", "+22 lines is not a 20-line piece"
    assert _first_piece_state(BOX_EMPTY, p872) == "empty"
    assert _first_piece_state(HEAD_LOST_872, p872) == "lost"
    assert _first_piece_state("no input box drawn", p872) is None


def test_classifier_accepts_the_piece_typed_as_text() -> None:
    """Should a CLI stop folding, the piece's own first characters are the evidence."""
    p872 = _chunk_for_send_keys(LONG_872)[0]
    assert _first_piece_state(_with_box(BOX_EMPTY, p872[:200]), p872) == "landed"


# ── review of 3038018: idle hints, \r line endings, stale folds ───────────


@pytest.mark.parametrize("idle", IDLE_FRAMES, ids=IDLE_IDS)
def test_an_idle_hint_or_suggestion_is_not_part_of_the_piece(idle: str) -> None:
    """capture-pane reads the greyed hint as text; it is no evidence of a loss."""
    for msg in (LONG_872, LONG_1263):
        assert _first_piece_state(idle, _chunk_for_send_keys(msg)[0]) == "empty"


@pytest.mark.parametrize("idle", IDLE_FRAMES, ids=IDLE_IDS)
def test_a_slow_first_piece_behind_a_hint_is_waited_for(idle: str) -> None:
    """The fold shows up on the third look; until then the box shows the hint."""
    sent, pane, _ = _send(LONG_872, [[idle, idle, P1_FOLD_872]], PASTED_872, idle=idle)
    assert sent is True
    assert pane.first_typings == 1, "a slow piece must not be cleared and typed again"
    assert not any("C-u" in k for k in pane.keys)
    assert pane.enters == 1


@pytest.mark.parametrize("idle", IDLE_FRAMES, ids=IDLE_IDS)
def test_a_lost_first_piece_behind_a_hint_is_typed_again(idle: str) -> None:
    """Clearing a box that then shows the hint is a successful clear."""
    sent, pane, _ = _send(LONG_872, [idle, P1_FOLD_872], PASTED_872, idle=idle)
    assert sent is True
    assert pane.first_typings == 2
    assert pane.enters == 1


@pytest.mark.parametrize("idle", IDLE_FRAMES, ids=IDLE_IDS)
def test_a_first_piece_lost_twice_behind_a_hint_is_refused(idle: str) -> None:
    _assert_refused(*_send(LONG_1263, [idle], SCROLLED_1263, idle=idle))


def test_a_piece_with_bare_cr_line_endings_matches_its_fold() -> None:
    """Review LOW 1: ``\r`` ends a line for the TUI too."""
    piece = "".join(f"行{i}: " + "あ" * 40 + "\r" for i in range(42))
    folded = _with_box(BOX_EMPTY, "[Pasted text #1 +42 lines]")
    assert _first_piece_state(folded, piece) == "landed"


def test_a_stale_fold_already_in_the_box_is_not_the_piece() -> None:
    """Review LOW 2: only a fold that appeared after typing counts."""
    piece = _chunk_for_send_keys(LONG_872)[0]
    assert _first_piece_state(P1_FOLD_872, piece, before=P1_FOLD_872) != "landed"
    fresh = _with_box(P1_FOLD_872, "[Pasted text #1 +22 lines][Pasted text #2 +22 lines]")
    assert _first_piece_state(fresh, piece, before=P1_FOLD_872) == "landed"


def test_a_lost_first_piece_behind_a_stale_fold_is_refused() -> None:
    """Review LOW 2 through send_input: the stale fold is there before and after."""
    _assert_refused(
        *_send(LONG_872, [P1_FOLD_872], PASTED_872, initial=P1_FOLD_872, idle=P1_FOLD_872)
    )
