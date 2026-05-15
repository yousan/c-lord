"""Tests for c_lord.skills.reply_tracker.

The tracker records when Claude posts via the ``discord-reply`` skill so
``run_claude_with_config`` can detect turns where the skill was never called
and surface a fallback notification to Discord (issue #67).
"""

from __future__ import annotations

import time

from c_lord.skills.reply_tracker import (
    record_reply,
    reset_tracker,
    was_replied_since,
)


def setup_function() -> None:
    reset_tracker()


def test_no_reply_returns_false() -> None:
    assert was_replied_since(thread_id=12345, since=time.monotonic()) is False


def test_recorded_reply_after_since_returns_true() -> None:
    before = time.monotonic()
    record_reply(thread_id=12345)
    assert was_replied_since(thread_id=12345, since=before) is True


def test_reply_before_since_returns_false() -> None:
    record_reply(thread_id=12345)
    later = time.monotonic() + 1.0
    assert was_replied_since(thread_id=12345, since=later) is False


def test_other_thread_isolated() -> None:
    before = time.monotonic()
    record_reply(thread_id=111)
    assert was_replied_since(thread_id=222, since=before) is False
    assert was_replied_since(thread_id=111, since=before) is True


def test_reset_clears_state() -> None:
    record_reply(thread_id=12345)
    reset_tracker()
    assert was_replied_since(thread_id=12345, since=0.0) is False
