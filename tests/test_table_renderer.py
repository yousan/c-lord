"""Tests for Markdown table detection and image rendering."""

from __future__ import annotations

import pytest

from c_lord.discord_ui.table_renderer import detect_tables, has_tables, render_table_image

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
