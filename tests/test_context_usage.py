"""Tests for context-window usage extraction and formatting.

Numerator (used tokens) comes from the Claude Code JSONL transcript; the
denominator (context window total) is learned from the ``/context`` slash
command output (or a per-model fallback map).  See
``c_lord/claude/context_usage.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from c_lord.claude.context_usage import (
    MODEL_CONTEXT_WINDOW,
    ContextUsage,
    default_window,
    fallback_window,
    format_context_line,
    parse_context_total,
    parse_cost_from_pane,
    read_latest_usage,
)

# A trimmed but faithful capture of a real ``/context`` pane (1M context).
CONTEXT_PANE_1M = """\
❯ /context
  ⎿  Context Usage
     ⛀ ⛀ ⛀ ⛀ ⛶   Opus 4.7 (1M context)
     ⛶ ⛶ ⛶ ⛶ ⛶   claude-opus-4-7[1m]
     ⛶ ⛶ ⛶ ⛶ ⛶   11.7k/1m tokens (1%)
     ⛶ ⛶ ⛶ ⛶ ⛶
     ⛶ ⛶ ⛶ ⛶ ⛶   Estimated usage by category
     ⛁ System prompt: 2.3k tokens (0.2%)
"""

CONTEXT_PANE_200K = """\
  ⎿  Context Usage
     claude-opus-4-7
     60k/200k tokens (30%)
"""


class TestReadLatestUsage:
    def _write(self, path: Path, lines: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(d) for d in lines) + "\n")

    def _assistant(self, **usage: int) -> dict:
        return {"type": "assistant", "message": {"role": "assistant", "usage": usage}}

    def test_reads_latest_assistant_usage(self, tmp_path: Path) -> None:
        f = tmp_path / "s.jsonl"
        self._write(
            f,
            [
                {"type": "user", "message": {"role": "user", "content": "hi"}},
                self._assistant(
                    input_tokens=2,
                    cache_creation_input_tokens=368,
                    cache_read_input_tokens=59750,
                    output_tokens=132,
                ),
                self._assistant(
                    input_tokens=3,
                    cache_creation_input_tokens=212,
                    cache_read_input_tokens=60250,
                    output_tokens=85,
                ),
            ],
        )
        usage = read_latest_usage(f)
        assert usage is not None
        # used = input + cache_read + cache_creation of the LAST assistant turn.
        assert usage.used == 3 + 60250 + 212
        assert usage.output_tokens == 85

    def test_captures_model(self, tmp_path: Path) -> None:
        f = tmp_path / "s.jsonl"
        self._write(
            f,
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-opus-4-7",
                        "usage": {"input_tokens": 1},
                    },
                }
            ],
        )
        usage = read_latest_usage(f)
        assert usage is not None
        assert usage.model == "claude-opus-4-7"

    def test_returns_none_when_no_assistant_usage(self, tmp_path: Path) -> None:
        f = tmp_path / "s.jsonl"
        self._write(f, [{"type": "user", "message": {"role": "user", "content": "hi"}}])
        assert read_latest_usage(f) is None

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert read_latest_usage(tmp_path / "nope.jsonl") is None

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "s.jsonl"
        f.write_text(
            "not json\n"
            + json.dumps(self._assistant(input_tokens=1, cache_read_input_tokens=999))
            + "\n"
        )
        usage = read_latest_usage(f)
        assert usage is not None
        assert usage.used == 1000

    def test_skips_trailing_synthetic_zero_usage_entry(self, tmp_path: Path) -> None:
        """#425: Claude Code writes a synthetic assistant entry (model
        ``<synthetic>``, all-zero usage) for failed tool-call parses / no-op
        turns.  If it is the last entry, reporting it would show 0 tokens used
        (a wrong "0% context" line) and key the context-window probe on a phantom
        model.  The last entry with real usage must be returned instead.
        """
        f = tmp_path / "s.jsonl"
        self._write(
            f,
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-opus-4-8",
                        "usage": {
                            "input_tokens": 5,
                            "cache_creation_input_tokens": 1_000,
                            "cache_read_input_tokens": 300_000,
                            "output_tokens": 120,
                        },
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "<synthetic>",
                        "usage": {
                            "input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 0,
                        },
                    },
                },
            ],
        )
        usage = read_latest_usage(f)
        assert usage is not None
        # The real opus turn is reported, not the trailing synthetic one.
        assert usage.model == "claude-opus-4-8"
        assert usage.used == 5 + 1_000 + 300_000

    def test_returns_none_when_only_synthetic_entries(self, tmp_path: Path) -> None:
        """All entries synthetic/zero-usage → nothing meaningful to report."""
        f = tmp_path / "s.jsonl"
        self._write(
            f,
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "<synthetic>",
                        "usage": {"input_tokens": 0},
                    },
                }
            ],
        )
        assert read_latest_usage(f) is None


class TestParseContextTotal:
    def test_parses_1m_suffix(self) -> None:
        assert parse_context_total(CONTEXT_PANE_1M) == 1_000_000

    def test_parses_200k_suffix(self) -> None:
        assert parse_context_total(CONTEXT_PANE_200K) == 200_000

    def test_parses_inline_line(self) -> None:
        assert parse_context_total("Context Usage\n  150.2k/1m tokens (15%)") == 1_000_000

    def test_decimal_k_total(self) -> None:
        assert parse_context_total("Context Usage\n  1k/12.5k tokens (8%)") == 12_500

    def test_returns_none_without_match(self) -> None:
        assert parse_context_total("no token info in this pane") is None

    def test_anchored_on_context_usage_header(self) -> None:
        """An unrelated ``X/Y tokens`` line elsewhere in the pane must NOT win.

        Regression: the bot pane scrollback can contain ``56.6k/200k tokens``
        from prior conversation/PR text, while the freshly-emitted ``/context``
        block (which is the only authoritative one) sits below a ``Context
        Usage`` header.  Picking the first regex match in the pane caches the
        wrong window forever.  parse_context_total must anchor on the latest
        ``Context Usage`` header and only consider lines that follow it.
        """
        pane = (
            "前の会話: PR 本文に 56.6k/200k tokens (28%) と書いた\n"
            "...history continues with that text rendered...\n"
            "❯ /context\n"
            "  ⎿  Context Usage\n"
            "     claude-opus-4-7[1m]\n"
            "     340.5k/1m tokens (34%)\n"
        )
        assert parse_context_total(pane) == 1_000_000

    def test_no_context_usage_header_returns_none(self) -> None:
        """Without the anchor the parse must return None (safer than guessing)."""
        assert parse_context_total("scrollback: 56.6k/200k tokens (28%)") is None


class TestDefaultWindow:
    def test_known_model(self) -> None:
        assert default_window("claude-opus-4-7") == 200_000

    def test_unknown_model_falls_back(self) -> None:
        assert default_window("some-future-model") == 200_000

    def test_none_falls_back(self) -> None:
        assert default_window(None) == 200_000

    def test_current_default_model_is_registered(self) -> None:
        """#292: the current default model must be in the map, not silently
        riding the unknown-model fallback (which hides tier drift)."""
        assert "claude-opus-4-8" in MODEL_CONTEXT_WINDOW
        assert default_window("claude-opus-4-8") == 200_000


class TestFallbackWindow:
    """#292: when /context can't be scraped, the fallback denominator must never
    be smaller than the numerator — a real window can't be smaller than what
    already occupies it. Otherwise a 1M-tier session whose probe failed renders
    a false "100% full / auto-compact" warning."""

    def test_promotes_to_next_tier_when_used_exceeds_default(self) -> None:
        # Real Factorio case: opus-4-8 on the 1M tier, used ≈ 314k, probe failed.
        assert fallback_window("claude-opus-4-8", used=313_779) == 1_000_000

    def test_no_promotion_when_used_fits_default(self) -> None:
        assert fallback_window("claude-opus-4-8", used=50_000) == 200_000

    def test_zero_used_returns_model_default(self) -> None:
        assert fallback_window("claude-opus-4-8") == 200_000

    def test_unknown_or_none_model_still_promoted_by_used(self) -> None:
        assert fallback_window(None, used=300_000) == 1_000_000
        assert fallback_window("some-future-model", used=300_000) == 1_000_000

    def test_beyond_largest_tier_total_never_below_used(self) -> None:
        # Pathological: used exceeds even the largest known tier. Invariant holds.
        assert fallback_window("claude-opus-4-8", used=2_000_000) >= 2_000_000

    def test_invariant_total_geq_used(self) -> None:
        for used in (0, 199_999, 200_000, 200_001, 999_999, 1_000_001):
            assert fallback_window("claude-opus-4-8", used=used) >= used

    def test_factorio_line_is_subtle_not_a_false_warning(self) -> None:
        """End-to-end of the bug: numerator+resolved-fallback must yield the
        subtle line, NOT the ⚠️ auto-compact warning."""
        used = 313_779
        total = fallback_window("claude-opus-4-8", used=used)
        line = format_context_line(used, total)
        assert line.startswith("-#")
        assert "⚠" not in line
        assert "compact" not in line.lower()
        assert "1.0M" in line
        assert "31%" in line


class TestFormatContextLine:
    def test_normal_is_subtle(self) -> None:
        line = format_context_line(used=60_000, total=1_000_000)
        assert line.startswith("-#")
        assert "6%" in line
        assert "context" in line.lower()
        assert "1.0M" in line

    def test_over_threshold_is_a_warning(self) -> None:
        line = format_context_line(used=170_000, total=200_000)
        assert "⚠" in line  # ⚠️
        assert "85%" in line
        assert "compact" in line.lower()

    def test_uses_k_for_thousands(self) -> None:
        line = format_context_line(used=60_000, total=200_000)
        assert "60k" in line
        assert "200k" in line

    def test_model_appended_shortened(self) -> None:
        line = format_context_line(used=60_000, total=200_000, model="claude-sonnet-4-6")
        assert "sonnet-4-6" in line
        assert "claude-sonnet-4-6" not in line

    def test_model_without_claude_prefix_unchanged(self) -> None:
        line = format_context_line(used=60_000, total=200_000, model="opus-4-8")
        assert "opus-4-8" in line

    def test_effort_appended(self) -> None:
        line = format_context_line(used=60_000, total=200_000, effort="medium")
        assert "medium" in line

    def test_cli_version_appended(self) -> None:
        line = format_context_line(used=60_000, total=200_000, cli_version="1.2.3")
        assert "CLI 1.2.3" in line

    def test_cost_appended(self) -> None:
        line = format_context_line(used=60_000, total=200_000, cost_usd=0.0042)
        assert "$0.0042" in line

    def test_all_extras_joined_with_dot(self) -> None:
        line = format_context_line(
            used=60_000,
            total=200_000,
            model="claude-sonnet-4-6",
            effort="medium",
            cli_version="1.2.3",
            cost_usd=0.0042,
        )
        assert "sonnet-4-6" in line
        assert "medium" in line
        assert "CLI 1.2.3" in line
        assert "$0.0042" in line
        assert " · " in line

    def test_warning_line_includes_extras(self) -> None:
        line = format_context_line(
            used=170_000, total=200_000, model="claude-opus-4-8", cost_usd=0.12
        )
        assert "⚠" in line
        assert "opus-4-8" in line
        assert "$0.1200" in line

    def test_none_extras_omitted(self) -> None:
        line = format_context_line(used=60_000, total=200_000, model=None, effort=None)
        assert " · " not in line


class TestParseCostFromPane:
    def test_parses_standard_ccstatusline(self) -> None:
        pane = "Model: claude-sonnet-4-6  Style: auto\nCost: $0.0042  Session: $0.1234\n❯ "
        assert parse_cost_from_pane(pane) == pytest.approx(0.0042)

    def test_returns_none_when_not_found(self) -> None:
        assert parse_cost_from_pane("no cost info here") is None

    def test_parses_larger_cost(self) -> None:
        pane = "Cost: $1.2345  Session: $5.6789"
        assert parse_cost_from_pane(pane) == pytest.approx(1.2345)

    def test_ignores_session_cost(self) -> None:
        pane = "Cost: $0.0010  Session: $9.9999"
        cost = parse_cost_from_pane(pane)
        assert cost == pytest.approx(0.0010)


def test_context_usage_used_property() -> None:
    u = ContextUsage(
        input_tokens=5,
        output_tokens=10,
        cache_read_tokens=100,
        cache_creation_tokens=20,
    )
    assert u.used == 125
