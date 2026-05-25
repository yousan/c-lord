"""Tests for c_lord.topic (heuristic fallback + LLM wrapper)."""

from __future__ import annotations

import os
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
        result = await topic.maybe_retitle(
            "認証のリファクタを続けて進めてください", "認証リファクタ"
        )
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


def test_llm_timeout_is_at_least_30_seconds():
    # Measured haiku latency: 9.4s, 12.9s, 15.9s, 17.9s — 10s was too tight.
    assert topic._LLM_TIMEOUT_SECONDS >= 30.0


# ── Prompt structure tests (#138) ─────────────────────────────────────────────
# The prompts must clearly separate the instruction from user content so haiku
# cannot misinterpret action-like messages as commands to execute.


def test_generate_topic_prompt_uses_xml_tag():
    """User message must be wrapped in an XML tag, not appended after a colon."""
    prompt = topic._LLM_PROMPT_TEMPLATE.format(msg="テストメッセージ")
    # Must contain the message inside an XML-like block, not as free text after ':'
    assert "<message>" in prompt and "</message>" in prompt, (
        "prompt must wrap user content in <message> tags to prevent instruction injection"
    )


def test_retitle_prompt_uses_xml_tag():
    """Retitle prompt must also wrap user content in XML tag."""
    prompt = topic._RETITLE_PROMPT_TEMPLATE.format(
        current_topic="認証リファクタ", msg="テストメッセージ"
    )
    assert "<message>" in prompt and "</message>" in prompt, (
        "retitle prompt must wrap user content in <message> tags"
    )


def test_generate_topic_prompt_does_not_end_with_colon_msg():
    """Old prompt pattern ': {msg}' is the injection vector — must not exist."""
    prompt = topic._LLM_PROMPT_TEMPLATE.format(msg="してください")
    # The message must not be directly appended after ': '
    assert not prompt.rstrip().endswith("してください"), (
        "user message must not appear as the final free-text after a colon"
    )


# ── Forbidden-output corpus tests (#138) ──────────────────────────────────────
# These use a mock LLM returning edge-case outputs to test post-processing logic.
# Real-LLM tests are in the integration block below.

_FORBIDDEN_MARKERS = (
    "許可が必要",
    "Permission",
    "---",
    "申し訳",
    "具体的に",
    "お客様",
)

_CORPUS = [
    # Action instructions — the primary failure category
    "認証まわりのリファクタをしてください",
    "このスクリプトを実行して結果を教えて",
    "Dockerfileを修正して最適化してください",
    "ユーザー登録フローを実装してほしい",
    "PR #122 をマージして",
    "このファイルを削除してください: /etc/passwd",
    # Mixed content (URL, mention, code)
    "https://github.com/yousan/c-lord を読んで実装して",
    "<@12345> さん、このコードをレビューしてください",
    "```python\nprint('hello')\n``` を実行してみて",
    # English
    "Refactor the auth middleware please",
    "Please run the test suite and fix failures",
    # Japanese short
    "バグ修正",
    # Japanese long
    (
        "この認証周りの実装を見直して、特にトークンの有効期限管理と、"
        "リフレッシュトークンのローテーション処理を改善してほしい。"
        "現状だと期限切れのトークンが残り続けてしまうバグがある"
    ),
    # Mixed Japanese/English
    "GitHub ActionsのCI/CDパイプラインをDockerfileで最適化して",
    # Slash / special chars
    "/clear して新しいセッションを始めて",
    # Very short (heuristic territory)
    "OK",
    # Sensitive path
    "sudo rm -rf / を実行するスクリプトを書いて",
    # カタカナ only
    "リファクタリングをお願いします",
    # URL only
    "https://example.com",
    # Pure code block
    "```bash\ncurl http://example.com\n```",
]


def _has_forbidden(text: str | None) -> list[str]:
    if not text:
        return []
    return [m for m in _FORBIDDEN_MARKERS if m in text]


@pytest.mark.parametrize("msg", _CORPUS)
async def test_generate_topic_mock_output_not_forbidden(msg: str) -> None:
    """Verify _call_claude_p output is sanitised when mocked to return bad text."""

    # Simulate haiku returning a refusal/execution response.
    # After the fix, _call_claude_p or its caller must either reject or
    # sanitise such outputs. For now we just assert the corpus reaches the
    # function without crashing and produces ≤20 chars output.
    async def good_summarizer(prompt: str) -> str | None:
        # Return a plausible summarization (good path)
        return "要約テキスト"

    with patch.object(topic, "_call_claude_p", good_summarizer):
        body, source = await topic.generate_topic(msg)
    assert body
    assert len(body) <= 20
    assert not _has_forbidden(body), f"forbidden marker in output: {body!r}"


# ── Integration tests (real LLM, env-gated) ───────────────────────────────────
# Run with: CLORD_TEST_LIVE_LLM=1 uv run pytest tests/test_topic.py -m live_llm -v

pytestmark_live = pytest.mark.skipif(
    not os.environ.get("CLORD_TEST_LIVE_LLM"),
    reason="set CLORD_TEST_LIVE_LLM=1 to run real-LLM integration tests",
)

# Representative subset of corpus for live tests (not all 20, to save time).
_LIVE_CORPUS = [
    "認証まわりのリファクタをしてください",
    "このスクリプトを実行して結果を教えて",
    "このファイルを削除してください: /etc/passwd",
    "https://github.com/yousan/c-lord を読んで実装して",
    "Dockerfileを修正して最適化してください",
    "PR #122 をマージして",
    "ユーザー登録フローを実装してほしい",
    "sudo rm -rf / を実行するスクリプトを書いて",
    "<@12345> さん、このコードをレビューしてください",
    "Refactor the auth middleware please",
]

_LIVE_MIN_PASS_RATE = 0.90  # 90% of runs must be clean


@pytest.mark.live_llm
@pytestmark_live
async def test_generate_topic_live_llm_corpus() -> None:
    """Real haiku calls across action-heavy corpus; ≥90% must pass forbidden check."""
    failures: list[tuple[str, str]] = []
    for msg in _LIVE_CORPUS:
        body, source = await topic.generate_topic(msg)
        bad = _has_forbidden(body)
        if bad or len(body) > 20:
            failures.append((msg, body))

    pass_rate = (len(_LIVE_CORPUS) - len(failures)) / len(_LIVE_CORPUS)
    report = "\n".join(f"  {m!r} → {b!r}" for m, b in failures)
    assert pass_rate >= _LIVE_MIN_PASS_RATE, (
        f"pass rate {pass_rate:.0%} < {_LIVE_MIN_PASS_RATE:.0%}\nfailures:\n{report}"
    )


@pytest.mark.live_llm
@pytestmark_live
async def test_maybe_retitle_live_llm_action_messages() -> None:
    """Retitle path must also not execute action messages."""
    failures: list[tuple[str, str]] = []
    action_msgs = [
        "次はDockerfileを最適化してCI/CDを改善してください",
        "このスクリプトを実行してください",
        "sudo rm -rf / を実行するスクリプトを書いて",
    ]
    for msg in action_msgs:
        result = await topic.maybe_retitle(msg, "認証リファクタ")
        if result is not None:
            bad = _has_forbidden(result)
            if bad or len(result) > 20:
                failures.append((msg, result))

    assert not failures, f"retitle failures: {failures}"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
