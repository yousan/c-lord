"""Tests for c_lord.thread_name (build/parse)."""

from __future__ import annotations

from c_lord.thread_name import STATUS_EMOJI, build_name, parse_topic_from_name


def test_build_name_alive_with_index():
    # New format: 🟢 W5 │ topic
    assert build_name("c-lord命名検討", "alive", 5) == "🟢 W5 │ c-lord命名検討"


def test_build_name_pending_with_index():
    out = build_name("topic", "pending", 3)
    assert out.startswith(STATUS_EMOJI["pending"])
    assert "W3 │" in out
    assert "topic" in out


def test_build_name_dead_drops_index():
    out = build_name("絵本-イラスト発注", "dead", 7)
    assert "W" not in out or "│" not in out  # no work prefix
    assert "#" not in out
    assert out.startswith(STATUS_EMOJI["dead"])
    assert "絵本-イラスト発注" in out


def test_build_name_no_index():
    # No window index → no W prefix
    out = build_name("topic", "alive", None)
    assert out == "🟢 topic"


def test_build_name_truncates_long_topic():
    long = "あ" * 100
    out = build_name(long, "alive", 12)
    assert len(out) <= 30
    assert out.startswith("🟢 W12 │ ")


def test_build_name_unknown_state_falls_back_to_alive_emoji():
    out = build_name("x", "weird", 1)
    assert out.startswith(STATUS_EMOJI["alive"])


def test_parse_strips_leading_emoji_and_work_prefix():
    # New format
    assert parse_topic_from_name("🟢 W5 │ c-lord命名検討") == "c-lord命名検討"
    assert parse_topic_from_name("⚪ 絵本") == "絵本"
    assert parse_topic_from_name("🟠 W12 │ t") == "t"


def test_parse_handles_old_trailing_index_format():
    # Backward compat: old format "🟢 topic #5" should still parse cleanly
    assert parse_topic_from_name("🟢 c-lord命名検討 #5") == "c-lord命名検討"
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


def test_build_name_dead_no_work_prefix():
    # Dead state: no W prefix even if window_index is provided
    out = build_name("topic", "dead", 3)
    assert "W3" not in out
    assert "│" not in out
