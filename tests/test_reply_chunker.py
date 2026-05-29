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
