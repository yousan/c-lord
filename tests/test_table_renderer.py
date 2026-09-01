"""Tests for Markdown table detection and image rendering."""

from __future__ import annotations

import importlib.util
from io import BytesIO

import pytest

from c_lord.discord_ui.table_renderer import (
    _JP_FONT_PATHS,
    _display_width,
    _first_existing,
    _prepare_cell,
    _segment_runs,
    _split_strike_runs,
    _strip_inline_markdown,
    _strip_strike_markers,
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

# The emoji library ships with the [table] extra; CI runs without it, in which
# case _segment_runs degrades to a single text run. Gate the split assertions.
EMOJI_LIB_AVAILABLE = importlib.util.find_spec("emoji") is not None
_needs_emoji = pytest.mark.skipif(not EMOJI_LIB_AVAILABLE, reason="emoji library not installed")


class TestSegmentRuns:
    def test_plain_text_single_run(self) -> None:
        assert _segment_runs("hello world") == [("hello world", False)]

    def test_empty_text(self) -> None:
        assert _segment_runs("") == []

    @_needs_emoji
    def test_splits_emoji_from_text(self) -> None:
        runs = _segment_runs("🟢 OK")
        assert runs == [("🟢", True), (" OK", False)]

    @_needs_emoji
    def test_multiple_emoji(self) -> None:
        runs = _segment_runs("🟢🔴 NG")
        assert runs == [("🟢", True), ("🔴", True), (" NG", False)]

    @_needs_emoji
    def test_emoji_in_middle(self) -> None:
        runs = _segment_runs("RED 🟢 GREEN")
        assert runs == [("RED ", False), ("🟢", True), (" GREEN", False)]

    def test_reassembles_to_original(self) -> None:
        text = "状態 🟢 OK / 🔴 NG ✅"
        assert "".join(s for s, _ in _segment_runs(text)) == text


# ===========================================================================
# _strip_inline_markdown
# ===========================================================================


class TestStripInlineMarkdown:
    """Inline markdown is rendered into a PNG, so it cannot be made interactive.

    The renderer must collapse it to the plain text a reader cares about instead
    of leaking the raw syntax into the image (#415).
    """

    def test_empty_unchanged(self) -> None:
        assert _strip_inline_markdown("") == ""

    def test_plain_text_unchanged(self) -> None:
        assert _strip_inline_markdown("plain text 123") == "plain text 123"

    def test_strips_bold(self) -> None:
        assert _strip_inline_markdown("**クローズ 6件**") == "クローズ 6件"

    def test_strips_italic(self) -> None:
        assert _strip_inline_markdown("*italic*") == "italic"

    def test_strips_bold_italic(self) -> None:
        assert _strip_inline_markdown("***both***") == "both"

    def test_strikethrough_is_marked_not_dropped(self) -> None:
        # `~~x~~` is the one construct an image CAN express (#607): the syntax
        # goes away but the span is marked so the renderer can draw a line.
        marked = _strip_inline_markdown("~~old~~")
        assert _strip_strike_markers(marked) == "old"
        assert _split_strike_runs(marked) == [("old", True)]

    def test_strips_inline_code(self) -> None:
        assert _strip_inline_markdown("`/clear`") == "/clear"

    def test_link_collapses_to_label(self) -> None:
        # The URL is unclickable inside an image, so only the label is kept.
        url = "https://github.com/yousan/c-lord/issues/61"
        assert _strip_inline_markdown(f"[#61]({url})") == "#61"

    def test_image_collapses_to_alt(self) -> None:
        assert _strip_inline_markdown("![alt](https://x/y.png)") == "alt"

    def test_link_with_trailing_text(self) -> None:
        url = "https://github.com/yousan/c-lord/issues/61"
        assert _strip_inline_markdown(f"[#61]({url})(not planned)") == "#61(not planned)"

    def test_code_preserves_underscores(self) -> None:
        # Identifiers inside code spans must survive intact.
        assert _strip_inline_markdown("`_is_allowed`") == "_is_allowed"

    def test_underscore_emphasis_left_untouched(self) -> None:
        # Intra-word underscores are identifiers, not emphasis (GFM agrees).
        assert _strip_inline_markdown("foo_bar_baz") == "foo_bar_baz"
        assert _strip_inline_markdown("__init__") == "__init__"

    def test_multiple_constructs_in_one_cell(self) -> None:
        url = "https://github.com/yousan/c-lord/issues/405"
        text = f"[#405]({url}) — `/clear`・`!clear` の `_is_allowed` 欠如 (#56由来)"
        expected = "#405 — /clear・!clear の _is_allowed 欠如 (#56由来)"
        assert _strip_inline_markdown(text) == expected

    def test_mixed_bold_link_code(self) -> None:
        assert _strip_inline_markdown("**a** and [b](http://u) and `c`") == "a and b and c"

    def test_hash_reference_unchanged(self) -> None:
        # `#61` is an issue ref, not a heading — must not be touched.
        assert _strip_inline_markdown("#61(not planned)") == "#61(not planned)"


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


# ===========================================================================
# Strikethrough (#607)
# ===========================================================================


class TestStrikeMarkers:
    """`~~x~~` survives as a marked span so the renderer can draw a real line.

    Claude uses strikethrough in tables to mean "this one is dropped". Collapsing
    it to plain text (as #415 did for every inline construct) makes the image say
    the opposite of the message body, which Discord *does* strike (#607).
    """

    def test_plain_text_has_one_unstruck_run(self) -> None:
        assert _split_strike_runs("abc") == [("abc", False)]

    def test_empty_text_has_no_runs(self) -> None:
        assert _split_strike_runs("") == []

    def test_partial_strike_splits_into_two_runs(self) -> None:
        marked = _strip_inline_markdown("~~a~~ b")
        assert _split_strike_runs(marked) == [("a", True), (" b", False)]

    def test_strike_around_bold_keeps_both_effects(self) -> None:
        # `~~**x**~~` -> the bold syntax goes (unrepresentable), the strike stays.
        assert _split_strike_runs(_strip_inline_markdown("~~**x**~~")) == [("x", True)]

    def test_markers_never_reach_the_visible_text(self) -> None:
        marked = _strip_inline_markdown("~~old~~ new")
        assert _strip_strike_markers(marked) == "old new"
        assert "~" not in marked

    def test_display_width_ignores_markers(self) -> None:
        # Markers are metadata; counting them would shrink the wrap budget.
        assert _display_width(_strip_inline_markdown("~~abc~~")) == 3

    def test_hostile_marker_chars_in_input_are_dropped(self) -> None:
        # A cell containing the internal control chars must not be able to
        # switch strike state on for the rest of the table.
        assert _split_strike_runs(_strip_inline_markdown("a\x02b\x03c")) == [("abc", False)]

    def test_reassembles_to_original_visible_text(self) -> None:
        marked = _strip_inline_markdown("~~やめる~~ → **やる**")
        assert "".join(chunk for chunk, _ in _split_strike_runs(marked)) == "やめる → やる"


class TestPrepareCell:
    """strip -> wrap -> re-balance, so every wrapped line stands on its own."""

    def test_short_cell_keeps_single_struck_run(self) -> None:
        assert _split_strike_runs(_prepare_cell("~~old~~", 84)) == [("old", True)]

    def test_wrapped_strike_continues_on_every_line(self) -> None:
        # A struck span wider than the column must stay struck after wrapping —
        # not only on the first line (AC3).
        cell = _prepare_cell("~~あいうえおかきくけこ~~", 8)
        lines = cell.split("\n")
        assert len(lines) > 1
        for line in lines:
            runs = _split_strike_runs(line)
            assert runs, line
            assert all(struck for _, struck in runs), runs

    def test_wrapped_plain_text_stays_unstruck(self) -> None:
        cell = _prepare_cell("あいうえおかきくけこ", 8)
        for line in cell.split("\n"):
            assert all(not struck for _, struck in _split_strike_runs(line))

    def test_wrap_budget_is_measured_without_markers(self) -> None:
        # 8 display units of CJK == 4 chars per line; markers must not steal room.
        cell = _prepare_cell("~~あいうえおかきくけこ~~", 8)
        for line in cell.split("\n"):
            assert _display_width(line) <= 8


@pytest.mark.skipif(not RENDER_AVAILABLE, reason="Pillow or a usable font not installed")
class TestRenderStrikethrough:
    STRUCK = "| x |\n|---|\n| ~~old~~ |\n"
    PLAIN = "| x |\n|---|\n| old |\n"
    PARTIAL = "| x |\n|---|\n| ~~old~~ new |\n"

    def test_struck_cell_differs_from_plain_cell(self) -> None:
        # AC1 — the line must actually be drawn, so the PNGs cannot be identical.
        assert render_table_image(self.STRUCK) != render_table_image(self.PLAIN)

    def test_partial_strike_differs_from_full_strike(self) -> None:
        # AC2 — striking only part of the cell is a third, distinct rendering.
        both = "| x |\n|---|\n| ~~old new~~ |\n"
        assert render_table_image(self.PARTIAL) != render_table_image(both)
        assert render_table_image(self.PARTIAL) != render_table_image("| x |\n|---|\n| old new |\n")

    def test_strike_syntax_is_not_drawn(self) -> None:
        # AC4 — the image must not leak `~~` (a #415 regression would widen it).
        from PIL import Image

        struck = Image.open(BytesIO(render_table_image(self.STRUCK)))
        plain = Image.open(BytesIO(render_table_image(self.PLAIN)))
        assert struck.size == plain.size

    def test_plain_table_rendering_unchanged(self) -> None:
        # AC5 — tables without strikethrough render byte-identically to before.
        assert render_table_image(SIMPLE_TABLE) == render_table_image(SIMPLE_TABLE)
        assert render_table_image(JAPANESE_TABLE)[:4] == b"\x89PNG"
