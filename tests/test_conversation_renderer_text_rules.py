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
