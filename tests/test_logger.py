"""Tests for log context helper."""

from __future__ import annotations

from c_lord.utils.logger import log_ctx


def test_log_ctx_thread_only() -> None:
    assert log_ctx(thread_id=123) == "[thread=123]"


def test_log_ctx_thread_and_session() -> None:
    assert log_ctx(thread_id=123, session_id="abc-def") == "[thread=123 session=abc-def]"


def test_log_ctx_all_fields() -> None:
    out = log_ctx(thread_id=1, session_id="s", task_id=7, channel_id=99)
    assert out == "[thread=1 session=s task=7 channel=99]"


def test_log_ctx_empty_returns_empty_string() -> None:
    assert log_ctx() == ""


def test_log_ctx_skips_none_values() -> None:
    assert log_ctx(thread_id=1, session_id=None, task_id=None) == "[thread=1]"


def test_log_ctx_truncates_long_session_id() -> None:
    # Full UUIDs are noisy in logs; the helper keeps them readable.
    out = log_ctx(session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert out == "[session=aaaaaaaa]"
