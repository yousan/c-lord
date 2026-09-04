"""Tests for c_lord.discord_ui.bridged_context — the #399 AC3 dedup registry.

When the pane-ask bridge posts the prose context above a menu, the Claude CLI
later flushes the SAME text to the transcript jsonl (it buffers the chunk until
the menu resolves). The mirror must not re-post it. The two copies are not
byte-identical: the pane carries the TUI *rendering* (markdown stripped,
hard-wrapped), the jsonl carries the raw markdown — so matching is normalized.

The realistic pair below is a real captured pane (CLI v2.1.173) and the real
markdown text the same CLI flushed to the jsonl after the menu resolved.
"""

from __future__ import annotations

from pathlib import Path

from c_lord.claude.tmux_runner import _parse_ask_from_pane
from c_lord.discord_ui.bridged_context import BridgedContextRegistry

_FIXTURES = Path(__file__).parent / "fixtures"


def _pane_context() -> str:
    pane = (_FIXTURES / "panes" / "ask_context_prose_above_menu.txt").read_text()
    q = _parse_ask_from_pane(pane)
    assert q is not None and q.context
    return q.context


def _flushed_markdown() -> str:
    return (_FIXTURES / "transcripts" / "i399_prose_flushed_markdown.txt").read_text()


def test_pane_context_matches_flushed_markdown() -> None:
    """The money case: TUI rendering registered, raw markdown consumed."""
    reg = BridgedContextRegistry()
    reg.register(399, _pane_context())
    assert reg.consume_match(399, _flushed_markdown()) is True


def test_consume_is_one_shot() -> None:
    reg = BridgedContextRegistry()
    reg.register(399, _pane_context())
    assert reg.consume_match(399, _flushed_markdown()) is True
    assert reg.consume_match(399, _flushed_markdown()) is False


def test_no_match_for_other_thread() -> None:
    reg = BridgedContextRegistry()
    reg.register(399, _pane_context())
    assert reg.consume_match(400, _flushed_markdown()) is False
    # The entry is still there for its own thread.
    assert reg.consume_match(399, _flushed_markdown()) is True


def test_unrelated_text_does_not_match() -> None:
    reg = BridgedContextRegistry()
    reg.register(399, _pane_context())
    assert reg.consume_match(399, "全く別の最終回答です。" * 20) is False
    # The non-match must not consume the entry.
    assert reg.consume_match(399, _flushed_markdown()) is True


def test_short_text_never_registered_nor_matched() -> None:
    """Tiny texts ("了解です。") are too ambiguous to suppress — never match."""
    reg = BridgedContextRegistry()
    reg.register(399, "了解です。")
    assert reg.consume_match(399, "了解です。") is False


def test_expired_entry_does_not_match(monkeypatch) -> None:
    from c_lord.discord_ui import bridged_context as mod

    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    reg = BridgedContextRegistry()
    reg.register(399, _pane_context())
    now[0] += mod._TTL_SECONDS + 1
    assert reg.consume_match(399, _flushed_markdown()) is False


def test_long_reply_quoting_context_is_not_suppressed() -> None:
    """A FINAL reply that merely QUOTES the registered context must not be
    suppressed — containment only counts when the two texts are comparable in
    size (the flushed twin), never when the context is a snippet of a much
    longer, genuinely new message."""
    reg = BridgedContextRegistry()
    ctx = "私の推しは (A) です。理由は通常時のオーバーヘッドがほぼゼロだからです。"
    reg.register(399, ctx)
    long_reply = (
        "実装が完了しました。設計の経緯を振り返ると、"
        + ctx
        + " その方針に沿って楽観ロックを repository 層に実装し、テストも追加しました。"
        + "詳細は PR を参照してください。" * 10
    )
    assert reg.consume_match(399, long_reply) is False
    # The comparable-size flushed twin still matches afterwards.
    assert reg.consume_match(399, ctx) is True


def test_truncated_registration_still_matches_comparable_flush() -> None:
    """Watchdog captures are capped (120 lines): the registered pane text can
    be a large suffix of the flushed text. Containment at comparable size
    (<=1.5x) must still match."""
    reg = BridgedContextRegistry()
    full = "判断ポイントは三つあります。" * 20
    suffix = full[len(full) * 15 // 100 :]  # registered = trailing 85% of the flush
    reg.register(399, suffix)
    assert reg.consume_match(399, full) is True


def test_clear_thread_removes_only_that_thread() -> None:
    reg = BridgedContextRegistry()
    reg.register(399, _pane_context())
    reg.register(400, _pane_context())
    reg.clear_thread(399)
    assert reg.consume_match(399, _flushed_markdown()) is False
    assert reg.consume_match(400, _flushed_markdown()) is True


def test_decision_flipped_restatement_not_suppressed_after_boundary_clear() -> None:
    """#399 review blocker 3 (registry half): clear_thread is the API the
    mirror calls at turn boundaries so a never-consumed entry cannot linger
    and swallow a similar-but-different REAL message later."""
    reg = BridgedContextRegistry()
    reg.register(399, _pane_context())
    reg.clear_thread(399)
    assert reg.consume_match(399, _flushed_markdown()) is False


# -- #399 plan double-post fix: order-independent (bidirectional) dedup --------
# The plan path flushes the pre-menu prose to jsonl BEFORE the menu resolves, so
# the mirror posts it FIRST (before the pane-bridge registers). The dedup must
# work in BOTH orders: each source skips/suppresses the OTHER source's entry,
# never its own (no self-suppression of a legitimate repeat).


def test_pane_skips_when_mirror_already_posted() -> None:
    """plan order: mirror posts the prose first (registers source='mirror');
    the pane-bridge must then find it and skip its own post."""
    reg = BridgedContextRegistry()
    ctx = _pane_context()
    reg.register(399, _flushed_markdown(), source="mirror")
    # pane-bridge looks for a mirror-sourced delivery of the same prose
    assert reg.consume_match(399, ctx, source="mirror") is True


def test_mirror_suppresses_when_pane_already_posted() -> None:
    """ask order (unchanged): pane posts first (source='pane'); the mirror
    flush must find it and suppress."""
    reg = BridgedContextRegistry()
    reg.register(399, _pane_context(), source="pane")
    assert reg.consume_match(399, _flushed_markdown(), source="pane") is True


def test_no_self_suppression_across_sources() -> None:
    """A mirror entry must NOT be consumed by a mirror-side check, and a pane
    entry must NOT be consumed by a pane-side check — each side only matches the
    other's source, so a legitimate same-source repeat is never swallowed."""
    reg = BridgedContextRegistry()
    reg.register(399, _pane_context(), source="mirror")
    # The mirror checking for pane entries finds nothing → it will post.
    assert reg.consume_match(399, _flushed_markdown(), source="pane") is False
    # The pane checking for mirror entries still finds it.
    assert reg.consume_match(399, _pane_context(), source="mirror") is True


def test_default_source_is_pane_backward_compatible() -> None:
    """Legacy 2-arg calls keep the original ask semantics (pane registers,
    mirror consumes pane)."""
    reg = BridgedContextRegistry()
    reg.register(399, _pane_context())  # defaults to source='pane'
    assert reg.consume_match(399, _flushed_markdown()) is True  # defaults to matching 'pane'


# -- #680: what is already in the thread ------------------------------------
# The dedup above is cross-source AND one-shot: the pane cannot see its own
# entries, and the mirror's entry is gone after the first bridge matches it. One
# AskUserQuestion with N questions bridges N menus off the SAME pane prose, so
# both holes put a second copy of it in the thread. The ledger below answers the
# question both cases actually ask — is this text already in the thread?


def test_a_delivered_text_is_recognised() -> None:
    """#680 AC2: a second delivery of the same prose is recognised."""
    reg = BridgedContextRegistry()
    ctx = _pane_context()
    assert reg.already_delivered(680, ctx) is False
    reg.note_delivered(680, ctx)
    assert reg.already_delivered(680, ctx) is True


def test_already_delivered_is_not_one_shot() -> None:
    """Q2, Q3, Q4 of the same ask must all see the Q1 delivery."""
    reg = BridgedContextRegistry()
    reg.note_delivered(680, _pane_context())
    assert [reg.already_delivered(680, _pane_context()) for _ in range(3)] == [True] * 3


def test_ledger_survives_the_mirror_consuming_the_dedup_entry() -> None:
    """The flush may land between two questions and consume the pane entry; the
    ledger must still remember that the prose is in the thread."""
    reg = BridgedContextRegistry()
    ctx = _pane_context()
    reg.register(680, ctx, source="pane")
    reg.note_delivered(680, ctx)
    assert reg.consume_match(680, _flushed_markdown(), source="pane") is True
    assert reg.already_delivered(680, ctx) is True


def test_ledger_is_source_blind() -> None:
    """A copy is a copy: the mirror's delivery must silence the pane's copy of
    the same prose (staging 2026-09-04: mirror posted it, then question 3's
    pane bridge posted it again once the one-shot entry was gone)."""
    reg = BridgedContextRegistry()
    reg.note_delivered(680, _flushed_markdown())  # delivered by the mirror
    assert reg.already_delivered(680, _pane_context()) is True  # pane's copy


def test_ledger_is_per_thread() -> None:
    reg = BridgedContextRegistry()
    reg.note_delivered(680, _pane_context())
    assert reg.already_delivered(681, _pane_context()) is False


def test_unrelated_prose_is_still_posted() -> None:
    """Over-suppression here means a menu with no 経緯 — only the SAME text is
    suppressed."""
    reg = BridgedContextRegistry()
    reg.note_delivered(680, _pane_context())
    assert reg.already_delivered(680, "全く別の経緯です。" * 20) is False


def test_short_text_is_never_ledgered() -> None:
    reg = BridgedContextRegistry()
    reg.note_delivered(680, "了解です。")
    assert reg.already_delivered(680, "了解です。") is False


def test_turn_boundary_clears_the_ledger() -> None:
    """A later turn may legitimately repeat the same prose — the ledger is
    scoped to the turn, like the dedup entries."""
    reg = BridgedContextRegistry()
    reg.note_delivered(680, _pane_context())
    reg.clear_thread(680)
    assert reg.already_delivered(680, _pane_context()) is False


def test_ledger_entry_expires(monkeypatch) -> None:
    from c_lord.discord_ui import bridged_context as mod

    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    reg = BridgedContextRegistry()
    reg.note_delivered(680, _pane_context())
    now[0] += mod._TTL_SECONDS + 1
    assert reg.already_delivered(680, _pane_context()) is False


def test_clear_drops_the_ledger() -> None:
    reg = BridgedContextRegistry()
    reg.note_delivered(680, _pane_context())
    reg.clear()
    assert reg.already_delivered(680, _pane_context()) is False
