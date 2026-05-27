"""Markdown table detection and color image rendering for Discord.

GFM pipe tables are detected in message content and rendered as PNG images via
Pillow (optional dependency: ``pip install c-lord[table]``). Emoji are drawn in
full color from a color emoji font (Noto Color Emoji) while text uses a
CJK-capable font, so 🟢/🔴 status lamps keep their color — something
matplotlib's single-font-per-cell tables could not do.

Controlled by the ``CLORD_RENDER_TABLE_IMAGES`` environment variable.
"""

from __future__ import annotations

import os
import re
import unicodedata
from io import BytesIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import ImageFont

# GFM pipe table pattern. Kept deliberately permissive so real-world tables
# still render instead of leaking as raw pipe text:
#   - outer leading/trailing pipes are optional (GFM allows `a | b`)
#   - leading indentation is tolerated (tables nested under a list item)
#   - trailing whitespace after a row is tolerated
#   - CRLF (`\r\n`) line endings are tolerated
# A row is any line containing at least one pipe; the separator is the anchor.
# The `(?=[^\n]*\|)` lookahead asserts a pipe exists, then the line is matched
# once with a single greedy `[^\n]+`. This avoids two unbounded `[^\n]*` around
# a literal `|`, which backtracks cubically on adversarial pipe-heavy input.
_HEADER = r"[ \t]*(?=[^\n]*\|)[^\n]+\r?\n"  # line with >=1 pipe, newline required
_DATA_ROW = r"[ \t]*(?=[^\n]*\|)[^\n]+\r?\n?"  # data row; trailing newline optional
_SEP = r"[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*\r?\n"
_TABLE_PATTERN = re.compile(
    r"(" + _HEADER + _SEP + r"(?:" + _DATA_ROW + r")+)",
)

# Layout knobs (pixels unless noted).
FONT_SIZE = 26
EMOJI_STRIKE = 109  # Noto Color Emoji bitmap strike size — render here, then scale down
PAD_X = 20  # horizontal cell padding
PAD_Y = 14  # vertical cell padding
LINE_GAP = 8  # extra space between wrapped lines
EMOJI_GAP = 2  # space after an emoji glyph
MAX_COL_WIDTH = 44  # max display width (CJK = 2) per column before wrapping

BORDER_COLOR = (208, 213, 221)
HEADER_BG = (68, 114, 196)
HEADER_FG = (255, 255, 255)
ALT_ROW_BG = (235, 243, 251)
ROW_BG = (255, 255, 255)
TEXT_COLOR = (32, 38, 46)

_JP_FONT_PATHS = (
    os.path.expanduser("~/.local/share/fonts/NotoSansJP.ttf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
# Color emoji fonts (CBDT/COLR) Pillow can render with embedded_color=True.
_COLOR_EMOJI_PATHS = (
    os.path.expanduser("~/.local/share/fonts/NotoColorEmoji.ttf"),
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
)
# Monochrome fallback if no color font is present (no color, but no tofu).
_MONO_EMOJI_PATHS = (
    os.path.expanduser("~/.local/share/fonts/NotoEmoji-Regular.ttf"),
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
)


def detect_tables(content: str) -> list[str]:
    """Return all GFM pipe table blocks found in *content*."""
    return _TABLE_PATTERN.findall(content)


def has_tables(content: str) -> bool:
    """Return True if *content* contains at least one GFM pipe table."""
    return bool(_TABLE_PATTERN.search(content))


def _display_width(text: str) -> int:
    """Display width of *text*, counting East Asian Wide/Fullwidth glyphs as 2."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _wrap_cell(text: str, max_width: int) -> str:
    """Wrap *text* so no line exceeds *max_width* display units.

    Wraps on spaces where possible; hard-breaks tokens (URLs, unspaced CJK)
    that are themselves wider than *max_width*. Returns newline-joined lines.
    """
    if max_width <= 0 or _display_width(text) <= max_width:
        return text

    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            if _display_width(word) > max_width:
                if cur:
                    lines.append(cur)
                    cur = ""
                chunk = ""
                for ch in word:
                    if chunk and _display_width(chunk + ch) > max_width:
                        lines.append(chunk)
                        chunk = ch
                    else:
                        chunk += ch
                cur = chunk
                continue
            candidate = f"{cur} {word}" if cur else word
            if _display_width(candidate) > max_width:
                lines.append(cur)
                cur = word
            else:
                cur = candidate
        lines.append(cur)
    return "\n".join(lines)


def _segment_runs(text: str) -> list[tuple[str, bool]]:
    """Split *text* into ``(substring, is_emoji)`` runs.

    Uses the ``emoji`` library to locate emoji clusters (ZWJ sequences,
    variation selectors, skin tones). Falls back to a single text run when the
    library is unavailable.
    """
    if not text:
        return []
    try:
        import emoji as _emoji
    except ImportError:
        return [(text, False)]

    spans = _emoji.emoji_list(text)
    if not spans:
        return [(text, False)]

    runs: list[tuple[str, bool]] = []
    i = 0
    for span in spans:
        start, end = span["match_start"], span["match_end"]
        if start > i:
            runs.append((text[i:start], False))
        runs.append((text[start:end], True))
        i = end
    if i < len(text):
        runs.append((text[i:], False))
    return runs


def _parse_table(table_md: str) -> tuple[list[str], list[list[str]]] | None:
    """Parse a GFM table into (headers, rows).

    Returns None if the table cannot be parsed.
    """
    lines = [ln.strip() for ln in table_md.strip().splitlines() if ln.strip()]
    if len(lines) < 3:  # need header + separator + at least 1 row
        return None

    def _split_row(line: str) -> list[str]:
        parts = line.strip("|").split("|")
        return [p.strip() for p in parts]

    headers = _split_row(lines[0])
    # lines[1] is the separator — skip it
    rows = [_split_row(line) for line in lines[2:]]
    if not headers or not rows:
        return None
    return headers, rows


def _first_existing(paths: tuple[str, ...]) -> str | None:
    return next((p for p in paths if os.path.exists(p)), None)


def _emoji_tile(
    cluster: str,
    emoji_font: ImageFont.FreeTypeFont,
    is_color: bool,
    target_h: int,
    cache: dict[str, object | None],
):
    """Render one emoji cluster to an RGBA tile scaled to *target_h* pixels.

    Color fonts are bitmap strikes at ``EMOJI_STRIKE``; we render large then
    scale down. Returns None if the glyph is absent (nothing drawn).
    """
    from PIL import Image, ImageDraw

    if cluster in cache:
        return cache[cluster]

    canvas = EMOJI_STRIKE * 2
    big = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    try:
        draw.text(
            (EMOJI_STRIKE // 4, EMOJI_STRIKE // 4),
            cluster,
            font=emoji_font,
            embedded_color=is_color,
            fill=TEXT_COLOR if not is_color else None,
        )
    except Exception:
        cache[cluster] = None
        return None

    bbox = big.getbbox()
    if bbox is None:
        cache[cluster] = None
        return None

    glyph = big.crop(bbox)
    scale = target_h / glyph.height
    tile = glyph.resize((max(1, round(glyph.width * scale)), target_h), Image.LANCZOS)
    cache[cluster] = tile
    return tile


def render_table_image(table_md: str) -> bytes | None:
    """Render a GFM pipe table as a color PNG image.

    Returns PNG bytes, or None if rendering is unavailable (Pillow not
    installed / no usable font) or the table cannot be parsed.
    """
    parsed = _parse_table(table_md)
    if parsed is None:
        return None
    headers, rows = parsed

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    jp_path = _first_existing(_JP_FONT_PATHS)
    if jp_path is None:
        return None
    text_font = ImageFont.truetype(jp_path, FONT_SIZE)

    color_emoji_path = _first_existing(_COLOR_EMOJI_PATHS)
    emoji_is_color = color_emoji_path is not None
    emoji_path = color_emoji_path or _first_existing(_MONO_EMOJI_PATHS)
    emoji_font = (
        ImageFont.truetype(emoji_path, EMOJI_STRIKE if emoji_is_color else FONT_SIZE)
        if emoji_path
        else None
    )

    n_cols = len(headers)
    rows = [(r + [""] * max(0, n_cols - len(r)))[:n_cols] for r in rows]
    grid = [headers, *rows]

    # Wrap every cell so lines stay within MAX_COL_WIDTH (bounds image width).
    grid = [[_wrap_cell(cell, MAX_COL_WIDTH) for cell in row] for row in grid]

    ascent, descent = text_font.getmetrics()
    text_h = ascent + descent
    emoji_h = int(FONT_SIZE * 1.2)
    line_h = max(text_h, emoji_h) + LINE_GAP

    scratch = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    tile_cache: dict[str, object | None] = {}

    def line_width(line: str) -> float:
        width = 0.0
        for text, is_emoji in _segment_runs(line):
            if is_emoji and emoji_font is not None:
                tile = _emoji_tile(text, emoji_font, emoji_is_color, emoji_h, tile_cache)
                width += (tile.width if tile is not None else emoji_h) + EMOJI_GAP
            else:
                width += scratch.textlength(text, font=text_font)
        return width

    # Column widths and row heights from wrapped, measured content.
    col_widths = [0.0] * n_cols
    row_heights: list[int] = []
    for row in grid:
        n_lines = 1
        for c, cell in enumerate(row):
            cell_lines = cell.split("\n")
            n_lines = max(n_lines, len(cell_lines))
            col_widths[c] = max(col_widths[c], max(line_width(ln) for ln in cell_lines))
        row_heights.append(n_lines * line_h + 2 * PAD_Y)

    col_px = [int(w + 2 * PAD_X) for w in col_widths]
    total_w = sum(col_px) + 1
    total_h = sum(row_heights) + 1

    img = Image.new("RGB", (total_w, total_h), ROW_BG)
    draw = ImageDraw.Draw(img)

    def draw_line(line: str, x0: int, y0: int, fill: tuple[int, int, int]) -> None:
        x = float(x0)
        for text, is_emoji in _segment_runs(line):
            if is_emoji and emoji_font is not None:
                tile = _emoji_tile(text, emoji_font, emoji_is_color, emoji_h, tile_cache)
                if tile is not None:
                    ey = y0 + (text_h - emoji_h) // 2
                    img.paste(tile, (int(x), ey), tile)
                    x += tile.width + EMOJI_GAP
                else:
                    x += emoji_h + EMOJI_GAP
            else:
                draw.text((x, y0), text, font=text_font, fill=fill, anchor="la")
                x += scratch.textlength(text, font=text_font)

    y = 0
    for r, row in enumerate(grid):
        is_header = r == 0
        if is_header:
            bg, fg = HEADER_BG, HEADER_FG
        else:
            bg, fg = (ALT_ROW_BG if r % 2 == 0 else ROW_BG), TEXT_COLOR
        x = 0
        for c, cell in enumerate(row):
            draw.rectangle([x, y, x + col_px[c], y + row_heights[r]], fill=bg, outline=BORDER_COLOR)
            ty = y + PAD_Y
            for line in cell.split("\n"):
                draw_line(line, x + PAD_X, ty, fg)
                ty += line_h
            x += col_px[c]
        y += row_heights[r]

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def get_table_images(content: str) -> list[tuple[str, bytes]]:
    """Return (filename, png_bytes) pairs for all tables in *content*.

    Returns an empty list when ``CLORD_RENDER_TABLE_IMAGES`` is not enabled
    or rendering is unavailable.  Callers wrap each pair into a
    ``discord.File(BytesIO(png_bytes), filename=filename)``.
    """
    if os.getenv("CLORD_RENDER_TABLE_IMAGES", "").lower() not in ("1", "true", "yes"):
        return []
    result = []
    for i, table_md in enumerate(detect_tables(content), start=1):
        img_bytes = render_table_image(table_md)
        if img_bytes:
            result.append((f"table_{i}.png", img_bytes))
    return result
