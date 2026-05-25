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


# ── _looks_like_instruction tests ─────────────────────────────────────────────

def test_looks_like_instruction_short_returns_false():
    assert topic._looks_like_instruction("OK") is False
    assert topic._looks_like_instruction("ありがとう") is False
    assert topic._looks_like_instruction("わかった") is False


def test_looks_like_instruction_question_ending_returns_false():
    assert topic._looks_like_instruction("この部分はどういう意味ですか？") is False
    assert topic._looks_like_instruction("what does this do?") is False


def test_looks_like_instruction_long_instruction_returns_true():
    assert topic._looks_like_instruction("認証まわりのリファクタをしてください") is True
    assert topic._looks_like_instruction("ユーザー登録フローを実装してほしい") is True


def test_looks_like_instruction_medium_length_instruction():
    assert topic._looks_like_instruction("Dockerfileを修正して最適化して") is True


# ── maybe_retitle tests ────────────────────────────────────────────────────────

async def test_maybe_retitle_skips_short_message():
    result = await topic.maybe_retitle("OK", "認証リファクタ")
    assert result is None


async def test_maybe_retitle_skips_question():
    result = await topic.maybe_retitle("この実装はなぜこうなっているのですか？", "認証リファクタ")
    assert result is None


async def test_maybe_retitle_verbatim_returns_none():
    # LLM returns same topic verbatim → no change
    async def fake_call(_prompt: str) -> str | None:
        return "認証リファクタ"

    with patch.object(topic, "_call_claude_p", fake_call):
        result = await topic.maybe_retitle("認証のリファクタを続けて進めてください", "認証リファクタ")
    assert result is None  # verbatim → no change


async def test_maybe_retitle_changed_returns_new_topic():
    # LLM detects work changed → returns new topic
    async def fake_call(_prompt: str) -> str | None:
        return "Dockerfileの最適化"

    with patch.object(topic, "_call_claude_p", fake_call):
        result = await topic.maybe_retitle(
            "次はDockerfileを最適化してCI/CDを改善してください", "認証リファクタ"
        )
    assert result == "Dockerfileの最適化"


async def test_maybe_retitle_llm_failure_returns_none():
    async def fake_call(_prompt: str) -> str | None:
        return None

    with patch.object(topic, "_call_claude_p", fake_call):
        result = await topic.maybe_retitle("新しい作業に切り替えてください", "認証リファクタ")
    assert result is None


async def test_maybe_retitle_llm_exception_returns_none():
    async def boom(_prompt: str) -> str | None:
        raise RuntimeError("network error")

    with patch.object(topic, "_call_claude_p", boom):
        result = await topic.maybe_retitle("新しい作業に切り替えてください", "認証リファクタ")
    assert result is None


async def test_maybe_retitle_passes_current_topic_in_prompt():
    """Verify the current topic is embedded in the retitle prompt."""
    captured: list[str] = []

    async def fake_call(prompt: str) -> str | None:
        captured.append(prompt)
        return "別のトピック"

    with patch.object(topic, "_call_claude_p", fake_call):
        await topic.maybe_retitle("全然違う作業をお願いしたいのですが", "認証リファクタ")

    assert captured, "LLM should have been called"
    assert "認証リファクタ" in captured[0], "current topic must appear in prompt"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
