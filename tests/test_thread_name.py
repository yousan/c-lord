"""Tests for c_lord.thread_name (build/parse)."""

from __future__ import annotations

from c_lord.thread_name import (
    CLOSED_MARK,
    LEGACY_CLOSED_MARKS,
    MAX_NAME_LEN,
    STATUS_EMOJI,
    build_name,
    parse_topic_from_name,
    thread_lamp_enabled,
    thread_retitle_enabled,
)


def test_build_name_alive_with_index():
    # New format: 🟢 W5 │ topic
    assert build_name("c-lord命名検討", "alive", 5) == "🟢 W5 │ c-lord命名検討"


def test_build_name_running_with_index():
    # "running" maps to 🟢 same as "alive"
    assert build_name("c-lord命名検討", "running", 5) == "🟢 W5 │ c-lord命名検討"


def test_build_name_waiting_with_index():
    out = build_name("topic", "waiting", 3)
    assert out.startswith(STATUS_EMOJI["waiting"])  # 🟡
    assert "W3 │" in out
    assert "topic" in out


def test_build_name_error_with_index():
    out = build_name("topic", "error", 2)
    assert out.startswith(STATUS_EMOJI["error"])  # 🔴
    assert "W2 │" in out
    assert "topic" in out


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


def test_build_name_running_no_index():
    out = build_name("topic", "running", None)
    assert out == "🟢 topic"


def test_build_name_truncates_long_topic():
    long = "あ" * 100
    out = build_name(long, "alive", 12)
    assert len(out) <= MAX_NAME_LEN
    assert out.startswith("🟢 W12 │ ")


def test_build_name_unknown_state_falls_back_to_alive_emoji():
    out = build_name("x", "weird", 1)
    assert out.startswith(STATUS_EMOJI["alive"])


def test_parse_strips_leading_emoji_and_work_prefix():
    # New format — includes new emojis
    assert parse_topic_from_name("🟢 W5 │ c-lord命名検討") == "c-lord命名検討"
    assert parse_topic_from_name("⚪ 絵本") == "絵本"
    assert parse_topic_from_name("🟠 W12 │ t") == "t"
    assert parse_topic_from_name("🟡 W3 │ 入力待ちtopic") == "入力待ちtopic"
    assert parse_topic_from_name("🔴 W1 │ エラーtopic") == "エラーtopic"


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


def test_build_then_parse_roundtrip_waiting():
    topic = "入力待ちwork"
    name = build_name(topic, "waiting", 2)
    assert parse_topic_from_name(name) == topic


def test_build_name_dead_no_work_prefix():
    # Dead state: no W prefix even if window_index is provided
    out = build_name("topic", "dead", 3)
    assert "W3" not in out
    assert "│" not in out


# --- #329: lamp can be disabled (default) — name keeps the topic, drops the
#     leading status emoji so the thread is never renamed on a state change. ---


def test_build_name_lamp_false_omits_emoji_with_index():
    # lamp disabled → no leading status emoji, topic + work prefix kept
    assert build_name("topic", "running", 3, lamp=False) == "W3 │ topic"


def test_build_name_lamp_false_omits_emoji_no_index():
    assert build_name("topic", "running", None, lamp=False) == "topic"


def test_build_name_lamp_false_no_status_emoji_for_any_state():
    for state in ("running", "waiting", "error", "dead", "alive", "pending"):
        out = build_name("topic", state, 2, lamp=False)
        for emoji in STATUS_EMOJI.values():
            assert emoji not in out, f"{state!r} leaked {emoji!r}"


def test_build_name_lamp_false_truncates_long_topic():
    out = build_name("あ" * 100, "running", 12, lamp=False)
    assert len(out) <= MAX_NAME_LEN
    assert out.startswith("W12 │ ")


def test_build_name_lamp_true_is_default_and_backward_compat():
    # Default is lamp=True — unchanged behaviour.
    assert build_name("topic", "running", 3) == build_name("topic", "running", 3, lamp=True)
    assert build_name("topic", "running", 3).startswith(STATUS_EMOJI["running"])


def test_parse_roundtrip_lamp_false():
    name = build_name("やること整理", "running", 3, lamp=False)
    assert parse_topic_from_name(name) == "やること整理"


# --- #329: thread_lamp_enabled() — off by default, opt in via CLORD_THREAD_LAMP ---


def test_thread_lamp_enabled_default_off(monkeypatch):
    monkeypatch.delenv("CLORD_THREAD_LAMP", raising=False)
    assert thread_lamp_enabled() is False


def test_thread_lamp_enabled_opt_in(monkeypatch):
    monkeypatch.setenv("CLORD_THREAD_LAMP", "1")
    assert thread_lamp_enabled() is True


def test_thread_lamp_enabled_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("CLORD_THREAD_LAMP", "1")
    assert thread_lamp_enabled(False) is False
    monkeypatch.delenv("CLORD_THREAD_LAMP", raising=False)
    assert thread_lamp_enabled(True) is True


def test_thread_lamp_enabled_truthy_values(monkeypatch):
    for v in ("1", "true", "yes", "on", "True", "YES"):
        monkeypatch.setenv("CLORD_THREAD_LAMP", v)
        assert thread_lamp_enabled() is True, f"{v!r} should enable"


def test_thread_lamp_enabled_falsy_values(monkeypatch):
    for v in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("CLORD_THREAD_LAMP", v)
        assert thread_lamp_enabled() is False, f"{v!r} should disable"


# --- #414: Issue/PR number token in the thread name ---


def test_build_name_with_issue_ref_lamp_on():
    out = build_name("認証", "running", 2, issue_ref="404")
    assert out == "🟢 W2 │ #404 認証"


def test_build_name_with_issue_ref_lamp_off():
    # Default deployment (lamp off): no status emoji, number kept.
    assert build_name("認証", "running", 2, lamp=False, issue_ref="404") == "W2 │ #404 認証"


def test_build_name_issue_ref_none_is_backward_compat():
    assert build_name("topic", "running", 3, issue_ref=None) == build_name("topic", "running", 3)


def test_build_name_issue_ref_normalizes_leading_hash():
    # Caller may pass "#404" or "404" — both render a single #.
    assert build_name("x", "running", 1, issue_ref="#404") == build_name(
        "x", "running", 1, issue_ref="404"
    )


def test_build_name_issue_ref_no_window():
    out = build_name("topic", "running", None, issue_ref="55")
    assert out == "🟢 #55 topic"


def test_build_name_issue_ref_truncates_topic_to_fit():
    out = build_name("あ" * 100, "running", 12, issue_ref="404")
    assert len(out) <= MAX_NAME_LEN
    assert out.startswith("🟢 W12 │ #404 ")


def test_parse_strips_leading_issue_ref():
    # New format: number sits between the work prefix and the topic.
    assert parse_topic_from_name("🟢 W2 │ #404 認証") == "認証"
    assert parse_topic_from_name("W2 │ #404 認証") == "認証"
    assert parse_topic_from_name("🟢 #55 topic") == "topic"


def test_build_then_parse_roundtrip_with_issue_ref():
    name = build_name("やること整理", "running", 3, issue_ref="404")
    assert parse_topic_from_name(name) == "やること整理"


# --- #414: maybe_retitle is opt-in (default off) via CLORD_THREAD_RETITLE ---


def test_thread_retitle_enabled_default_off(monkeypatch):
    monkeypatch.delenv("CLORD_THREAD_RETITLE", raising=False)
    assert thread_retitle_enabled() is False


def test_thread_retitle_enabled_opt_in(monkeypatch):
    monkeypatch.setenv("CLORD_THREAD_RETITLE", "1")
    assert thread_retitle_enabled() is True


def test_thread_retitle_enabled_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("CLORD_THREAD_RETITLE", "1")
    assert thread_retitle_enabled(False) is False
    monkeypatch.delenv("CLORD_THREAD_RETITLE", raising=False)
    assert thread_retitle_enabled(True) is True


# ── #512/#574: 停止 (stopped) marker ─────────────────────────────────────────


def test_build_name_closed_prefixes_marker():
    """#512: a stopped workspace is marked with :data:`CLOSED_MARK` up front.

    Asserted through the constant, not its literal text: #574 renamed it from
    「終了」 to 「停止」 and these tests should track the marker, not re-encode the
    wording. The wording itself is pinned once, in ``test_workspace_stop.py``.
    """
    assert (
        build_name("認証リファクタ", "dead", None, lamp=False, issue_ref="404", closed=True)
        == f"{CLOSED_MARK} #404 認証リファクタ"
    )


def test_build_name_closed_drops_work_prefix_and_lamp():
    """#512: the W<N> window no longer exists and the lamp is meaningless once closed."""
    out = build_name("topic", "alive", 3, lamp=True, closed=True)
    assert out == f"{CLOSED_MARK} topic"
    assert "W3" not in out
    assert not any(e in out for e in STATUS_EMOJI.values())


def test_build_name_closed_keeps_issue_ref_and_truncates_topic():
    """#512: under the name cap the marker + number survive; the topic is trimmed."""
    out = build_name("あ" * 40, "dead", None, lamp=False, issue_ref="404", closed=True)
    assert out.startswith(f"{CLOSED_MARK} #404 ")
    assert len(out) <= MAX_NAME_LEN


def test_build_name_not_closed_is_unchanged():
    """#512: closed=False (the default) must not alter any existing name."""
    assert build_name("c-lord命名検討", "alive", 5) == "🟢 W5 │ c-lord命名検討"


def test_parse_topic_strips_closed_marker():
    """#512: the marker must not be absorbed into the stored topic on manual rename."""
    for mark in (CLOSED_MARK, *LEGACY_CLOSED_MARKS):
        assert parse_topic_from_name(f"{mark} #404 認証リファクタ") == "認証リファクタ"
        assert parse_topic_from_name(f"{mark} W3 │ 認証リファクタ") == "認証リファクタ"
        assert parse_topic_from_name(f"{mark} 認証リファクタ") == "認証リファクタ"
