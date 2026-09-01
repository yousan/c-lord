"""Tests for c_lord.issue_ref — Issue/PR number detection (#414).

Pure extractors that derive the ``#<number>`` token for a thread name from
either the session's git branch or a message body. They must be conservative:
returning a number only when it is clearly an issue/PR reference, never an
incidental digit (the version ``2`` in ``feature/v2-api``, etc.).
"""

from __future__ import annotations

import pytest

from c_lord.issue_ref import extract_from_branch, extract_from_text


@pytest.mark.parametrize(
    "branch,expected",
    [
        ("404-add-thing", "404"),
        ("fix/404-add-thing", "404"),
        ("fix/404", "404"),
        ("feature/123-thing", "123"),
        ("issue-123", "123"),
        ("issue/123", "123"),
        ("gh-123", "123"),
        ("pr-7", "7"),
        ("bug/55-crash", "55"),
        ("hotfix/9001-x", "9001"),
        ("#42-quick", "42"),
    ],
)
def test_extract_from_branch_positive(branch, expected):
    assert extract_from_branch(branch) == expected


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "master",
        "develop",
        "dev",
        "trunk",
        "feature/v2-api",  # version, not an issue
        "feat/oauth2-login",  # glued digit
        "step-2",  # mid-segment digit, no keyword → not an issue ref
        "phase-3",
        "no-numbers-here",
        "",
        None,
    ],
)
def test_extract_from_branch_negative(branch):
    assert extract_from_branch(branch) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("#404 を直して", "404"),
        ("これは #123 の話", "123"),
        ("(#77) 参照", "77"),
        ("see https://github.com/yousan/c-lord/issues/404 for context", "404"),
        ("PR https://github.com/yousan/c-lord/pull/512 をマージ", "512"),
    ],
)
def test_extract_from_text_positive(text, expected):
    assert extract_from_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "ただのメッセージ",
        "color #fff is white",  # hex, not a number ref
        "",
        None,
        "no hash here 404 plain",  # bare number without # is not a ref
    ],
)
def test_extract_from_text_negative(text):
    assert extract_from_text(text) is None


def test_extract_from_text_url_takes_precedence_or_first():
    # Whichever appears, a single number is returned; URL form is reliable.
    assert extract_from_text("close https://github.com/o/r/pull/9") == "9"
