"""Tests for ClaudeConfig defaults."""

from __future__ import annotations

from c_lord.claude.config import ClaudeConfig


def test_effort_defaults_to_none() -> None:
    """A fresh ClaudeConfig passes no --effort flag (CLI default applies).

    c-lord does not override the CLI's own reasoning-effort default; the
    effort level is opt-in via ``CLAUDE_EFFORT`` / per-instance config.
    """
    assert ClaudeConfig().effort is None


def test_effort_is_configurable() -> None:
    """The effort level is overridable per ClaudeConfig instance."""
    assert ClaudeConfig(effort="high").effort == "high"
