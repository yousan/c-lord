"""Tests for context-window usage extraction and formatting.

Numerator (used tokens) comes from the Claude Code JSONL transcript; the
denominator (context window total) is learned from the ``/context`` slash
command output (or a per-model fallback map).  See
``c_lord/claude/context_usage.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from c_lord.claude.context_usage import (
    ContextUsage,
    default_window,
    format_context_line,
    parse_context_total,
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


def test_context_usage_used_property() -> None:
    u = ContextUsage(
        input_tokens=5,
        output_tokens=10,
        cache_read_tokens=100,
        cache_creation_tokens=20,
    )
    assert u.used == 125
