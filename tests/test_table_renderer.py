"""Tests for Markdown table detection and image rendering."""

from __future__ import annotations

import importlib.util

import pytest

from c_lord.discord_ui.table_renderer import (
    _JP_FONT_PATHS,
    _display_width,
    _first_existing,
    _segment_runs,
    _wrap_cell,
    detect_tables,
    has_tables,
    render_table_image,
)

# ---------------------------------------------------------------------------
# Sample fixtures
# ---------------------------------------------------------------------------

SIMPLE_TABLE = """\
| Name | Score |
|------|-------|
| Alice | 100  |
| Bob   | 85   |
"""

ALIGNED_TABLE = """\
| Left | Center | Right |
|:-----|:------:|------:|
| a    | b      | c     |
"""

JAPANESE_TABLE = """\
| 名前 | スコア |
|------|--------|
| アリス | 100  |
| ボブ   | 85   |
"""

NO_TABLE = "This is just plain text with no table."

MIXED_CONTENT = f"""\
Here is a summary:

{SIMPLE_TABLE}
And some trailing text.
"""

MULTIPLE_TABLES = f"""\
First table:

{SIMPLE_TABLE}
Second table:

{ALIGNED_TABLE}
"""

# Real-world variants that the original strict regex failed to detect, so the
# table silently fell back to raw pipe text in Discord (#table-rendering).

# GFM allows omitting the outer leading/trailing pipes.
NO_OUTER_PIPES_TABLE = """\
Name | Score
--- | ---
Alice | 100
Bob | 85
"""

# Trailing whitespace after the closing pipe (common from cell-alignment).
TRAILING_WS_TABLE = "| Name | Score | \n|------|-------| \n| Alice | 100 | \n"

# CRLF line endings (Windows / some clients).
CRLF_TABLE = "| Name | Score |\r\n|------|-------|\r\n| Alice | 100 |\r\n"

# Leading indentation (e.g. table emitted under a list item).
INDENTED_TABLE = "  | Name | Score |\n  |------|-------|\n  | Alice | 100 |\n"


# ===========================================================================
# detect_tables
# ===========================================================================


class TestDetectTables:
    def test_detects_simple_table(self) -> None:
        tables = detect_tables(SIMPLE_TABLE)
        assert len(tables) == 1

    def test_detects_aligned_table(self) -> None:
        tables = detect_tables(ALIGNED_TABLE)
        assert len(tables) == 1

    def test_detects_japanese_table(self) -> None:
        tables = detect_tables(JAPANESE_TABLE)
        assert len(tables) == 1

    def test_no_table_returns_empty(self) -> None:
        assert detect_tables(NO_TABLE) == []

    def test_detects_table_in_mixed_content(self) -> None:
        tables = detect_tables(MIXED_CONTENT)
        assert len(tables) == 1

    def test_detects_multiple_tables(self) -> None:
        tables = detect_tables(MULTIPLE_TABLES)
        assert len(tables) == 2

    def test_detects_table_without_outer_pipes(self) -> None:
        assert len(detect_tables(NO_OUTER_PIPES_TABLE)) == 1

    def test_detects_table_with_trailing_whitespace(self) -> None:
        assert len(detect_tables(TRAILING_WS_TABLE)) == 1

    def test_detects_table_with_crlf(self) -> None:
        assert len(detect_tables(CRLF_TABLE)) == 1

    def test_detects_indented_table(self) -> None:
        assert len(detect_tables(INDENTED_TABLE)) == 1


class TestHasTables:
    def test_true_when_table_present(self) -> None:
        assert has_tables(SIMPLE_TABLE) is True

    def test_false_when_no_table(self) -> None:
        assert has_tables(NO_TABLE) is False


# ===========================================================================
# _display_width
# ===========================================================================


class TestDisplayWidth:
    def test_ascii_counts_one_each(self) -> None:
        assert _display_width("abc") == 3

    def test_empty_is_zero(self) -> None:
        assert _display_width("") == 0

    def test_cjk_counts_two_each(self) -> None:
        assert _display_width("あい") == 4

    def test_mixed_ascii_and_cjk(self) -> None:
        # "AB漢" -> 1 + 1 + 2
        assert _display_width("AB漢") == 4


# ===========================================================================
# _wrap_cell
# ===========================================================================


class TestWrapCell:
    def test_short_text_unchanged(self) -> None:
        assert _wrap_cell("hello", 40) == "hello"

    def test_zero_width_disables_wrap(self) -> None:
        long = "x" * 100
        assert _wrap_cell(long, 0) == long

    def test_wraps_on_spaces(self) -> None:
        result = _wrap_cell("alpha beta gamma", 10)
        for line in result.split("\n"):
            assert _display_width(line) <= 10
        # round-trips back to the original words
        assert result.replace("\n", " ") == "alpha beta gamma"

    def test_hard_breaks_long_token(self) -> None:
        # A long unbroken token (e.g. a URL) must still be split.
        url = "https://github.com/yousan/c-lord/pull/188"
        result = _wrap_cell(url, 15)
        lines = result.split("\n")
        assert len(lines) > 1
        for line in lines:
            assert _display_width(line) <= 15
        assert "".join(lines) == url

    def test_wraps_cjk_by_display_width(self) -> None:
        # No spaces in CJK text -> must break by character width.
        text = "これはとても長い日本語のテキストです"
        result = _wrap_cell(text, 8)
        lines = result.split("\n")
        assert len(lines) > 1
        for line in lines:
            assert _display_width(line) <= 8
        assert "".join(lines) == text


# ===========================================================================
# _segment_runs
# ===========================================================================


class TestSegmentRuns:
    def test_plain_text_single_run(self) -> None:
        assert _segment_runs("hello world") == [("hello world", False)]

    def test_empty_text(self) -> None:
        assert _segment_runs("") == []

    def test_splits_emoji_from_text(self) -> None:
        runs = _segment_runs("🟢 OK")
        assert runs == [("🟢", True), (" OK", False)]

    def test_multiple_emoji(self) -> None:
        runs = _segment_runs("🟢🔴 NG")
        assert runs == [("🟢", True), ("🔴", True), (" NG", False)]

    def test_emoji_in_middle(self) -> None:
        runs = _segment_runs("RED 🟢 GREEN")
        assert runs == [("RED ", False), ("🟢", True), (" GREEN", False)]

    def test_reassembles_to_original(self) -> None:
        text = "状態 🟢 OK / 🔴 NG ✅"
        assert "".join(s for s, _ in _segment_runs(text)) == text


# ===========================================================================
# render_table_image
# ===========================================================================

RENDER_AVAILABLE = (
    importlib.util.find_spec("PIL") is not None and _first_existing(_JP_FONT_PATHS) is not None
)


@pytest.mark.skipif(not RENDER_AVAILABLE, reason="Pillow or a usable font not installed")
class TestRenderTableImage:
    def test_returns_bytes(self) -> None:
        result = render_table_image(SIMPLE_TABLE)
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_returns_png(self) -> None:
        result = render_table_image(SIMPLE_TABLE)
        assert result is not None
        assert result[:4] == b"\x89PNG"

    def test_japanese_table_renders(self) -> None:
        result = render_table_image(JAPANESE_TABLE)
        assert result is not None
        assert len(result) > 0

    def test_emoji_table_renders(self) -> None:
        table = "| 項目 | 状態 |\n|------|------|\n| ビルド | 🟢 OK |\n| テスト | 🔴 NG |\n"
        result = render_table_image(table)
        assert result is not None
        assert result[:4] == b"\x89PNG"

    def test_aligned_table_renders(self) -> None:
        result = render_table_image(ALIGNED_TABLE)
        assert result is not None

    def test_invalid_table_returns_none(self) -> None:
        result = render_table_image("not a table at all")
        assert result is None


class TestRenderTableImageNoPillow:
    def test_returns_none_without_pillow(self, monkeypatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "PIL", None)
        monkeypatch.setitem(sys.modules, "PIL.Image", None)
        import importlib

        import c_lord.discord_ui.table_renderer as mod

        importlib.reload(mod)
        try:
            result = mod.render_table_image(SIMPLE_TABLE)
            assert result is None
        finally:
            importlib.reload(mod)
