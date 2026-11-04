"""Text rules of the design-comp renderer that need no font — #1035 / #1036.

Kept apart from ``test_conversation_renderer_inline.py`` on purpose: that file
skips wholesale without the ``[table]`` font, and these rules (what counts as
emoji, how a fence is split) must be checked on CI too.
"""

from __future__ import annotations

import pytest

from c_lord.discord_ui.conversation_renderer import (
    ConvMessage,
    _messages_contain_emoji,
    _split_code_blocks,
    conversation_from_spec,
    render_conversation_png,
)
from c_lord.discord_ui.fonts import load_text_font


class TestEmojiDetectionIsEmojiOnly:
    """#1035 — 絵文字の無い日本語・矢印・罫線を「絵文字がある」と描かなかった。

    ``ord(ch) >= 0x2190`` で判定していたので、日本語も ``→`` も罫線も絵文字扱いに
    なり、``emoji`` パッケージの無い環境（CI も）ではほぼ全ての日本語 spec が
    描けなかった。おぷー決定（2026-10-29）: 絵文字の範囲だけで判定する。
    """

    @pytest.mark.parametrize(
        "text",
        [
            "```\n日本語のテーブル\n```",  # #623 の再現 spec そのまま
            "a → b",  # 矢印 (U+2192)
            "─┼─ │ ┌┐",  # 罫線
            "ひらがな・カタカナ・漢字",
            "全角！？（）",
        ],
    )
    def test_emoji_free_text_is_not_emoji(self, text: str) -> None:
        assert not _messages_contain_emoji([ConvMessage(author="a", content=text)])

    @pytest.mark.parametrize(
        "text",
        [
            "💤 スリープしました",  # U+1F4A4
            "⏳ 待機中",  # U+23F3 — c-lord の stall 表示
            "✅ Done",  # U+2705
            "⭐",  # U+2B50
            "❤️",  # U+2764 + VS16
            "☀",  # U+2600
            "🇯🇵",  # regional indicators
            "↪ 引用返信",  # U+21AA — 本文フォントに無く豆腐になる（実測）
        ],
    )
    def test_emoji_is_still_emoji(self, text: str) -> None:
        assert _messages_contain_emoji([ConvMessage(author="a", content=text)])

    def test_the_623_spec_renders_without_the_library(self, monkeypatch) -> None:
        """Issue 本文の再現: 絵文字なし・日本語の spec が emoji パッケージ無しで描ける。"""
        if load_text_font(20) is None:
            pytest.skip("needs a usable font")
        monkeypatch.setattr(
            "c_lord.discord_ui.conversation_renderer.emoji_support_available", lambda: False
        )
        msgs = conversation_from_spec(
            {
                "messages": [
                    {"author": "C-lord", "is_bot": True, "content": "```\n日本語のテーブル\n```"}
                ]
            }
        )
        assert render_conversation_png(msgs) is not None

    def test_an_emoji_spec_is_still_refused_without_the_library(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "c_lord.discord_ui.conversation_renderer.emoji_support_available", lambda: False
        )
        msg = ConvMessage(author="C-lord", content="日本語 → ⏳ 待機中")
        assert render_conversation_png([msg]) is None


class TestSingleLineCodeFence:
    """#1036 — 同じ行で閉じるコードフェンス（```ls -la```）の中身が消えた。

    Discord はこれを1行のコードブロックとして描く。おぷー決定（2026-10-29）:
    案A — モックも1行のコードブロックとして描く。
    """

    def test_the_content_is_kept_as_code(self) -> None:
        assert _split_code_blocks("```ls -la```") == [
            ("text", ""),
            ("code", "ls -la"),
            ("text", ""),
        ]

    def test_what_follows_is_text_again(self) -> None:
        assert _split_code_blocks("```ls -la```\nafter text") == [
            ("text", ""),
            ("code", "ls -la"),
            ("text", "after text"),
        ]

    def test_text_before_and_a_normal_block_after_still_work(self) -> None:
        content = "見て:\n```git status```\nそして\n```\nmulti\nline\n```\nおわり"
        assert _split_code_blocks(content) == [
            ("text", "見て:"),
            ("code", "git status"),
            ("text", "そして"),
            ("code", "multi\nline"),
            ("text", "おわり"),
        ]

    def test_a_plain_opening_fence_is_unchanged(self) -> None:
        assert _split_code_blocks("```python\nprint(1)\n```") == [
            ("text", ""),
            ("code", "print(1)"),
            ("text", ""),
        ]


B = "`" * 3


class TestFenceReopenedOnTheSameLine:
    """#1096 — 1行で閉じたフェンスの後ろで同じ行に次のフェンスを開くと反転した（#1057 の回帰）。"""

    def test_issue_repro_matches_discord(self) -> None:
        # Discord: code "a", text " then ", code "more", text "after"
        blocks = _split_code_blocks(f"{B}a{B} then {B}\nmore\n{B}\nafter")
        assert ("text", "after") in blocks
        assert ("code", "more") in blocks
        assert [b for b in blocks if b[1].strip()] == [
            ("code", "a"),
            ("text", " then "),
            ("code", "more"),
            ("text", "after"),
        ]

    def test_two_one_line_blocks_on_one_line(self) -> None:
        blocks = _split_code_blocks(f"{B}a{B} and {B}b{B} done\nnext")
        assert [b for b in blocks if b[1].strip()] == [
            ("code", "a"),
            ("text", " and "),
            ("code", "b"),
            ("text", " done\nnext"),
        ]

    def test_the_reopened_fence_keeps_its_language_tag_out(self) -> None:
        blocks = _split_code_blocks(f"{B}a{B} {B}py\nprint(1)\n{B}\nend")
        assert [b for b in blocks if b[1].strip()] == [
            ("code", "a"),
            ("code", "print(1)"),
            ("text", "end"),
        ]


class TestInlineCodeAfterAOneLineBlock:
    """#1175 — 1行のコードブロックの後ろのインラインコード `` ``` `` を開きフェンスと数えた。

    #1168（#1096 の修正）で、1行ブロックの後ろの ``` を開きフェンスとして数える
    ようになったが、インラインコードの中の ``` まで数えたので、以降の本文と
    コードが入れ替わって描かれた。Discord ではインラインコードの中身は
    フェンスにならない。
    """

    def test_inline_code_holding_a_fence_after_a_one_line_block(self) -> None:
        content = f"{B}a{B} see `` {B} `` here\nbody\n{B}\ncode\n{B}"
        blocks = _split_code_blocks(content)
        assert ("code", "code") in blocks
        assert not any(k == "code" and "body" in v for k, v in blocks), blocks

    def test_single_backtick_span_holding_a_fence(self) -> None:
        blocks = _split_code_blocks(f"{B}a{B} see `x {B} y` here\nbody\n{B}\ncode\n{B}")
        assert ("code", "code") in blocks
        assert not any(k == "code" and "body" in v for k, v in blocks), blocks

    def test_unclosed_backtick_does_not_hide_a_real_opener(self) -> None:
        blocks = _split_code_blocks(f"{B}a{B} it`s {B}\nmore\n{B}\nafter")
        assert ("code", "more") in blocks
        assert ("text", "after") in blocks


class TestTofuSymbolsAreEmoji:
    """#1097 — #1057 で外れた ◻ ◼ ◽ ◾ ⤴ ⤵ と、もとから素通りの ℹ を断る側に戻す。"""

    @pytest.mark.parametrize("ch", ["◻", "◼", "◽", "◾", "⤴", "⤵", "ℹ"])
    def test_counted_as_emoji(self, ch: str) -> None:
        assert _messages_contain_emoji([ConvMessage(author="a", content=f"{ch} 状態")])

    @pytest.mark.parametrize("ch", ["◻", "◼", "◽", "◾", "⤴"])
    def test_refused_without_the_library(self, monkeypatch, ch: str) -> None:
        # #588: without the `emoji` lib a spec holding emoji must not render as tofu.
        # (Noto Sans JP — the Linux body face — has no glyph for these; some
        # macOS faces do, so whether it *would* be tofu is not asserted here.)
        monkeypatch.setattr(
            "c_lord.discord_ui.conversation_renderer.emoji_support_available", lambda: False
        )
        assert render_conversation_png([ConvMessage(author="a", content=f"{ch} 状態")]) is None

    @pytest.mark.parametrize("text", ["□ 未完了", "■ 完了", "◯ ok"])
    def test_neighbouring_geometric_shapes_are_not(self, text: str) -> None:
        # The body face draws these; only the four emoji-presentation squares were added.
        assert not _messages_contain_emoji([ConvMessage(author="a", content=text)])
