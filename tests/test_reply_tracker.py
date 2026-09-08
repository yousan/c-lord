"""Tests for c_lord.skills.reply_tracker.

The tracker remembers the message each thread's last answer landed in, so
post-turn helpers (e.g. the context-usage line) can append to that bubble
instead of opening a new one.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from c_lord.skills.reply_tracker import (
    get_last_reply_message,
    record_reply_message,
    reset_tracker,
)


def setup_function() -> None:
    reset_tracker()


def test_no_reply_returns_none() -> None:
    assert get_last_reply_message(thread_id=12345) is None


def test_records_and_returns_the_message() -> None:
    msg = MagicMock()
    record_reply_message(12345, msg)
    assert get_last_reply_message(12345) is msg


def test_last_write_wins() -> None:
    first, last = MagicMock(), MagicMock()
    record_reply_message(12345, first)
    record_reply_message(12345, last)
    assert get_last_reply_message(12345) is last


def test_other_thread_isolated() -> None:
    msg = MagicMock()
    record_reply_message(111, msg)
    assert get_last_reply_message(222) is None
    assert get_last_reply_message(111) is msg


def test_reset_clears_state() -> None:
    record_reply_message(12345, MagicMock())
    reset_tracker()
    assert get_last_reply_message(12345) is None
