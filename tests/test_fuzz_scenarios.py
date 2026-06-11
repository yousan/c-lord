"""Unit tests for scripts.fuzz.scenarios — LLM scenario parsing (Issue #377).

The generator asks the ``claude`` CLI for a JSON array of scenarios. The CLI
output is untrusted text (prose, code fences, partial JSON), so parsing must be
robust: extract what it can, drop what it cannot, never crash the run.
"""

from __future__ import annotations

from scripts.fuzz.scenarios import (
    Scenario,
    build_generation_prompt,
    parse_scenarios,
)


def test_parse_delimited_block_format_with_unescaped_payload() -> None:
    # Primary format: delimiter blocks where the text is RAW (no escaping). This
    # is what survives adversarial payloads — unescaped quotes/backslashes/braces
    # that break LLM-emitted JSON (the real failure observed with haiku, #377).
    raw = (
        "===FUZZ===\n"
        "category: broken-quotes\n"
        "intent: probes unescaped quotes\n"
        "---TEXT---\n"
        'He said "hello" and \\ stuff {not: json} ```py\nx=1\n'
        "===FUZZ===\n"
        "category: emoji\n"
        "intent: render\n"
        "---TEXT---\n"
        "🎨🎨 ❤️\n"
    )
    s = parse_scenarios(raw)
    assert len(s) == 2
    assert s[0].category == "broken-quotes"
    assert '"hello"' in s[0].text
    assert "{not: json}" in s[0].text
    assert s[1].text.strip() == "🎨🎨 ❤️"


def test_parse_delimited_ignores_wrapping_code_fence() -> None:
    raw = "```\n===FUZZ===\ncategory: c\nintent: i\n---TEXT---\nhello world\n```\n"
    s = parse_scenarios(raw)
    assert len(s) == 1
    assert s[0].text.strip() == "hello world"


def test_parse_plain_json_array() -> None:
    raw = '[{"category":"x","text":"hello","intent":"i"}]'
    s = parse_scenarios(raw)
    assert len(s) == 1
    assert isinstance(s[0], Scenario)
    assert s[0].text == "hello"
    assert s[0].category == "x"
    assert s[0].intent == "i"
    assert s[0].id  # auto-assigned when absent


def test_parse_fenced_json_with_surrounding_prose() -> None:
    raw = 'Sure!\n```json\n[{"text":"a"},{"text":"b"}]\n```\nHope that helps.'
    s = parse_scenarios(raw)
    assert [x.text for x in s] == ["a", "b"]
    assert s[0].id != s[1].id  # ids are unique within a parse


def test_parse_object_with_scenarios_key_keeps_custom_id() -> None:
    raw = '{"scenarios":[{"text":"a","category":"c","intent":"i","id":"custom"}]}'
    s = parse_scenarios(raw)
    assert s[0].id == "custom"
    assert s[0].category == "c"


def test_parse_drops_invalid_items_and_respects_limit() -> None:
    raw = (
        '[{"text":"a"},'
        '{"category":"no-text-here"},'  # dropped: no text
        '{"text":""},'  # dropped: empty text
        '{"text":"b"},'
        '{"text":"c"}]'
    )
    s = parse_scenarios(raw, limit=2)
    assert [x.text for x in s] == ["a", "b"]


def test_parse_array_with_embedded_code_fence_in_text() -> None:
    # Regression: a scenario's `text` itself contains a ```python fence. A naive
    # non-greedy ```...``` extractor stops at the embedded fence and truncates the
    # JSON. The bracket-span extractor must still recover the whole array.
    raw = (
        "```json\n"
        '[{"category":"md",'
        '"text":"see ```python\\ndef f():\\n  return 1\\n no close","intent":"i"},'
        '{"category":"x","text":"second"}]\n'
        "```\n"
    )
    s = parse_scenarios(raw)
    assert [x.text for x in s][1] == "second"
    assert s[0].text.startswith("see ```python")


def test_parse_garbage_returns_empty_list() -> None:
    assert parse_scenarios("not json at all, sorry") == []
    assert parse_scenarios("") == []


def test_parse_missing_optional_fields_defaults() -> None:
    s = parse_scenarios('[{"text":"only text"}]')
    assert s[0].text == "only text"
    assert s[0].category == "uncategorized"
    assert s[0].intent == ""


def test_build_generation_prompt_mentions_count_and_json() -> None:
    p = build_generation_prompt(7)
    assert "7" in p
    assert "json" in p.lower()


def test_build_generation_prompt_includes_focus_when_given() -> None:
    p = build_generation_prompt(3, focus="multilingual edge cases")
    assert "multilingual edge cases" in p
