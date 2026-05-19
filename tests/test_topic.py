"""Tests for c_lord.topic (heuristic fallback + LLM wrapper)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from c_lord import topic


def test_heuristic_topic_strips_url():
    out = topic.heuristic_topic("https://example.com を読んで")
    assert "http" not in out
    assert "読んで" in out


def test_heuristic_topic_strips_code_fences():
    out = topic.heuristic_topic("見て ```python\nprint('x')\n``` どう")
    assert "print" not in out
    assert "見て" in out
    assert "どう" in out


def test_heuristic_topic_strips_mentions():
    out = topic.heuristic_topic("<@1234567890> よろしくお願いします")
    assert "<@" not in out
    assert "よろしく" in out


def test_heuristic_topic_collapses_whitespace():
    out = topic.heuristic_topic("これは    複数の\n空白を\t含む")
    assert "  " not in out


def test_heuristic_topic_truncates_to_20_chars():
    long = "あ" * 100
    out = topic.heuristic_topic(long)
    assert len(out) == 20


def test_heuristic_topic_empty_input_returns_fallback():
    assert topic.heuristic_topic("") == "新しいスレッド"
    assert topic.heuristic_topic("   ") == "新しいスレッド"
    # URLs and code only → all stripped, still falls back
    assert topic.heuristic_topic("https://example.com") == "新しいスレッド"


async def test_generate_topic_uses_heuristic_when_llm_fails():
    async def fake_invoke(_msg: str) -> str | None:
        return None

    with patch.object(topic, "_invoke_claude_haiku", fake_invoke):
        body, source = await topic.generate_topic("test 入力 メッセージ")
    assert source == "heuristic"
    assert body  # non-empty
    assert "test" in body or "入力" in body


async def test_generate_topic_uses_llm_when_available():
    async def fake_invoke(_msg: str) -> str | None:
        return "LLMが返した要約"

    with patch.object(topic, "_invoke_claude_haiku", fake_invoke):
        body, source = await topic.generate_topic("元のメッセージ")
    assert source == "llm"
    assert body == "LLMが返した要約"


async def test_generate_topic_falls_back_on_empty_llm_response():
    async def fake_invoke(_msg: str) -> str | None:
        return ""

    with patch.object(topic, "_invoke_claude_haiku", fake_invoke):
        body, source = await topic.generate_topic("元のメッセージ")
    assert source == "heuristic"
    assert body  # non-empty


async def test_generate_topic_never_raises_on_internal_exception():
    async def boom(_msg: str) -> str | None:
        raise RuntimeError("synthetic")

    with patch.object(topic, "_invoke_claude_haiku", boom):
        body, source = await topic.generate_topic("こんにちは")
    assert source == "heuristic"
    assert body


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
