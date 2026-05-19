"""Tests for c_lord.thread_name (build/parse)."""

from __future__ import annotations

from c_lord.thread_name import STATUS_EMOJI, build_name, parse_topic_from_name


def test_build_name_alive_with_index():
    assert build_name("c-lord命名検討", "alive", 5) == "🟢 c-lord命名検討 #5"


def test_build_name_pending_with_index():
    out = build_name("topic", "pending", 3)
    assert out.startswith(STATUS_EMOJI["pending"])
    assert out.endswith(" #3")


def test_build_name_dead_drops_index():
    out = build_name("絵本-イラスト発注", "dead", 7)
    assert "#" not in out
    assert out.startswith(STATUS_EMOJI["dead"])
    assert "絵本-イラスト発注" in out


def test_build_name_no_index():
    out = build_name("topic", "alive", None)
    assert out == "🟢 topic"


def test_build_name_truncates_long_topic():
    long = "あ" * 100
    out = build_name(long, "alive", 12)
    assert len(out) <= 30
    assert out.startswith("🟢 ")
    assert out.endswith(" #12")


def test_build_name_unknown_state_falls_back_to_alive_emoji():
    out = build_name("x", "weird", 1)
    assert out.startswith(STATUS_EMOJI["alive"])


def test_parse_strips_leading_emoji_and_trailing_index():
    assert parse_topic_from_name("🟢 c-lord命名検討 #5") == "c-lord命名検討"
    assert parse_topic_from_name("⚪ 絵本") == "絵本"
    assert parse_topic_from_name("🟠 t #12") == "t"


def test_parse_handles_name_without_emoji():
    assert parse_topic_from_name("just a name") == "just a name"


def test_parse_handles_name_without_index():
    assert parse_topic_from_name("🟢 just topic") == "just topic"


def test_parse_returns_empty_for_emoji_only():
    assert parse_topic_from_name("🟢") == ""


def test_build_then_parse_roundtrip():
    topic = "やること整理"
    name = build_name(topic, "alive", 3)
    assert parse_topic_from_name(name) == topic
