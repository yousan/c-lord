"""Tests for c_lord.discord_ui.reply_chunker.

Regression for the "reply text is cut off at the end" bug: the ``/api/reply``
path sent ``content`` to Discord with a single ``send`` call, so any answer
longer than Discord's 2000-char limit was truncated / rejected. The chunker
splits long content into multiple Discord-sendable pieces.
"""

from __future__ import annotations

import pytest

from c_lord.discord_ui.reply_chunker import DISCORD_MAX, chunk_discord_content


class TestChunkDiscordContent:
    def test_short_content_passes_through_unchanged(self) -> None:
        assert chunk_discord_content("hello") == ["hello"]

    def test_content_exactly_at_limit_is_single_chunk(self) -> None:
        text = "x" * DISCORD_MAX
        chunks = chunk_discord_content(text)
        assert chunks == [text]

    def test_long_plain_text_splits_into_multiple_chunks(self) -> None:
        # 5000 chars of newline-separated lines.
        text = "\n".join(f"line {i} " + "y" * 40 for i in range(120))
        assert len(text) > DISCORD_MAX
        chunks = chunk_discord_content(text)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c) <= DISCORD_MAX
            assert c != ""

    def test_no_content_is_lost_when_splitting_plain_lines(self) -> None:
        lines = [f"line-{i}" for i in range(2000)]
        text = "\n".join(lines)
        chunks = chunk_discord_content(text)
        # Every original line survives somewhere, in order.
        rejoined = "\n".join(chunks)
        for ln in lines:
            assert ln in rejoined

    def test_oversized_single_line_is_hard_split(self) -> None:
        text = "z" * (DISCORD_MAX * 2 + 17)
        chunks = chunk_discord_content(text)
        assert len(chunks) >= 3
        for c in chunks:
            assert len(c) <= DISCORD_MAX
        assert "".join(chunks) == text

    def test_code_fence_is_balanced_in_every_chunk(self) -> None:
        # A code block big enough to span multiple chunks.
        body = "\n".join("a" * 80 for _ in range(60))
        text = f"```python\n{body}\n```"
        assert len(text) > DISCORD_MAX
        chunks = chunk_discord_content(text)
        assert len(chunks) >= 2
        for c in chunks:
            # Even number of fence markers => every chunk is self-contained.
            assert c.count("```") % 2 == 0
            assert len(c) <= DISCORD_MAX

    def test_invalid_limit_raises(self) -> None:
        with pytest.raises(ValueError):
            chunk_discord_content("x", limit=0)


class TestCodeBlockOverflowRegression:
    """Regression for #266 — a single long line inside a code block.

    Real trigger: pasting a Factorio Blueprint string (one 2081-char line) in a
    fenced block produced (a) a 69-char chunk that was just an *empty* code
    block and (b) a 2004-char chunk that exceeded Discord's 2000 limit and was
    rejected, so the code body never arrived and a duplicate empty block was
    posted. The existing fence test only used short lines, so ``_hard_split``
    never ran and the bug slipped through.
    """

    def _bp_reply(self, code_len: int = 2081) -> str:
        prose = (
            "了解です、ゲーム内撮影が一番確実です。手順をまとめます。\n\n"
            "**1. コードをコピー**（修正版・これを使ってください）\n\n"
        )
        return prose + "```\n" + ("A" * code_len) + "\n```"

    def test_every_chunk_within_limit(self) -> None:
        content = self._bp_reply()
        assert len(content) > DISCORD_MAX
        chunks = chunk_discord_content(content)
        for c in chunks:
            assert len(c) <= DISCORD_MAX, f"chunk over limit: {len(c)}"

    def test_no_empty_code_block_chunk(self) -> None:
        chunks = chunk_discord_content(self._bp_reply())
        for c in chunks:
            # An "open-then-immediately-close" fence renders as broken empty
            # block. No chunk should be (or end with) just that.
            assert c.strip() != "```\n```"
            assert not c.rstrip().endswith("```\n```")

    def test_code_body_is_not_lost(self) -> None:
        code = "A" * 2081
        chunks = chunk_discord_content(self._bp_reply(2081))
        # Strip fences and rejoin: the full code body must survive.
        recovered = "".join(c.replace("```", "") for c in chunks)
        assert code in recovered.replace("\n", "")

    def test_every_chunk_has_balanced_fences(self) -> None:
        chunks = chunk_discord_content(self._bp_reply())
        for c in chunks:
            assert c.count("```") % 2 == 0

    def test_long_single_line_code_block_at_various_sizes(self) -> None:
        # Sweep sizes around the chunk boundary to catch off-by-one regressions.
        for code_len in (1996, 1997, 2000, 2001, 4100, 6005):
            content = "```\n" + ("Z" * code_len) + "\n```"
            chunks = chunk_discord_content(content)
            for c in chunks:
                assert len(c) <= DISCORD_MAX, f"size={code_len} chunk={len(c)}"
                assert c.count("```") % 2 == 0
            recovered = "".join(c.replace("```", "") for c in chunks).replace("\n", "")
            assert "Z" * code_len in recovered
