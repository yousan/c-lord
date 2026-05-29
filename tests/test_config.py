"""Tests for ClaudeConfig defaults."""

from __future__ import annotations

from c_lord.claude.config import ClaudeConfig


def test_effort_defaults_to_max() -> None:
    """c-lord raises the effort default: a fresh ClaudeConfig uses 'max'.

    The Claude CLI otherwise runs at its own (lower) default; c-lord opts every
    session into the deepest reasoning level available via the --effort flag.
    """
    assert ClaudeConfig().effort == "max"


def test_effort_is_configurable() -> None:
    """The effort level is overridable per ClaudeConfig instance."""
    assert ClaudeConfig(effort="high").effort == "high"
