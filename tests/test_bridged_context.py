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
    full = "判断ポイントは三つあります。" * 12
    suffix = full[len(full) // 4 :]  # registered = trailing 3/4 of the flush
    reg.register(399, suffix)
    assert reg.consume_match(399, full) is True
