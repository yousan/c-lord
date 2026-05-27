"""Tests for Markdown table detection and image rendering."""

from __future__ import annotations

import pytest

from c_lord.discord_ui.table_renderer import (
    _display_width,
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
# render_table_image
# ===========================================================================

pytest_plugins: list[str] = []

try:
    import matplotlib  # noqa: F401

    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


@pytest.mark.skipif(not MATPLOTLIB_AVAILABLE, reason="matplotlib not installed")
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

    def test_aligned_table_renders(self) -> None:
        result = render_table_image(ALIGNED_TABLE)
        assert result is not None

    def test_invalid_table_returns_none(self) -> None:
        result = render_table_image("not a table at all")
        assert result is None


class TestRenderTableImageNoMatplotlib:
    def test_returns_none_without_matplotlib(self, monkeypatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "matplotlib", None)
        monkeypatch.setitem(sys.modules, "matplotlib.pyplot", None)
        # Re-import to trigger ImportError path
        import importlib

        import c_lord.discord_ui.table_renderer as mod

        importlib.reload(mod)
        result = mod.render_table_image(SIMPLE_TABLE)
        assert result is None
        # Restore
        importlib.reload(mod)
