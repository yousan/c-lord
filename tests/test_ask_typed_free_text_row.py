"""#925: a menu answered with a sentence must not be bridged a second time.

staging-3, 2026-10-28, Claude Code v2.1.312. A typed answer is delivered by
walking down to the "Type something." row and typing onto it, then Enter. While
the CLI takes the answer, the menu stays drawn with the sentence IN that row:

    ❯ 3. 標準出力でお願いします
    ─────────────
      4. Chat about this
    Enter to select · ↑/↓ to navigate · ctrl+g to edit in Vim · Esc to cancel

Read naively that row is a third, real option. The poll loop recognises "the
menu it already bridged" by its options, so it saw a NEW menu, posted a second
card for the same question, and 5s later — the menu gone — rewrote it to
「✅ 端末で回答済み」. 3 runs out of 3 on staging (and reproduced again here).

The fixture is that real capture, taken between typing and Enter.
"""

from __future__ import annotations

from pathlib import Path

from c_lord.claude.tmux_runner import _parse_ask_from_pane
from c_lord.claude.types import FREE_TEXT_ROW
from c_lord.menu_ledger import menu_fingerprint

_FIX = Path(__file__).parent / "fixtures" / "panes" / "i925_ask_free_text_typed_v2_1_312.txt"
_TYPED_ROW = "❯ 3. 標準出力でお願いします"


def _typed() -> str:
    return _FIX.read_text(encoding="utf-8")


def _untyped() -> str:
    """The same menu as first drawn: cursor on 1, the row still 'Type something.'."""
    text = _typed()
    assert _TYPED_ROW in text
    return (
        text.replace("  1. ファイル", "❯ 1. ファイル", 1)
        .replace(_TYPED_ROW, "  3. Type something.", 1)
        .replace(" · ctrl+g to edit in Vim", "", 1)
    )


def test_the_typed_sentence_is_not_an_option() -> None:
    """RED before #925: ['ファイル', '標準出力', '標準出力でお願いします']."""
    q = _parse_ask_from_pane(_typed())
    assert q is not None
    assert [o.label for o in q.options] == ["ファイル", "標準出力"]


def test_the_typed_menu_is_the_same_menu_as_the_one_bridged() -> None:
    """AC1: the poll loop and the #633/#717 ledger both identify a menu by its
    options — the typed-in frame must read as the menu already on Discord."""
    before = _parse_ask_from_pane(_untyped())
    after = _parse_ask_from_pane(_typed())
    assert before is not None and after is not None
    assert [o.label for o in after.options] == [o.label for o in before.options]
    assert menu_fingerprint(after) == menu_fingerprint(before)


def test_the_typed_menu_still_takes_free_text() -> None:
    """The 'Type something' label is gone while the row is edited; the row is
    still the free-text affordance."""
    q = _parse_ask_from_pane(_typed())
    assert q is not None
    assert q.allow_other is True
    assert q.free_text_mode == FREE_TEXT_ROW


def test_a_real_option_under_the_cursor_is_kept() -> None:
    """Only the row being EDITED is the free-text row: with the cursor on a real
    option and no edit hint, every option stays."""
    q = _parse_ask_from_pane(_untyped())
    assert q is not None
    assert [o.label for o in q.options] == ["ファイル", "標準出力"]
