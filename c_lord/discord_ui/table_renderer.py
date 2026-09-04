"""Markdown table detection and color image rendering for Discord.

GFM pipe tables are detected in message content and rendered as PNG images via
Pillow (optional dependency: ``pip install c-lord[table]``). Emoji are drawn in
full color from a color emoji font (Noto Color Emoji) while text uses a
CJK-capable font, so 🟢/🔴 status lamps keep their color — something
matplotlib's single-font-per-cell tables could not do.

Controlled by the ``CLORD_RENDER_TABLE_IMAGES`` environment variable.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from io import BytesIO
from typing import TYPE_CHECKING

# Font path resolution is shared with the pane (screenshot) renderer — see
# c_lord/discord_ui/fonts.py. Re-exported under the historical private names so
# existing imports/tests keep working.
from .fonts import COLOR_EMOJI_PATHS as _COLOR_EMOJI_PATHS
from .fonts import JP_FONT_PATHS as _JP_FONT_PATHS
from .fonts import MONO_EMOJI_PATHS as _MONO_EMOJI_PATHS
from .fonts import first_existing as _first_existing

if TYPE_CHECKING:
    from PIL import ImageFont

logger = logging.getLogger(__name__)

# Discord accepts at most 10 attachments on a single message; an 11th makes the
# whole send fail, taking the message text with it.  This is the ceiling for
# table images on one message (#683) — not a per-turn budget, since every
# intermediate message is its own send with its own allowance.
MAX_TABLE_IMAGES = 10

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
MAX_COL_WIDTH = 84  # max display width (CJK = 2) per column before wrapping
STRIKE_WIDTH = 2  # thickness of the strikethrough rule
STRIKE_EM_ABOVE_BASELINE = 0.40  # rule height, in em above the baseline

BORDER_COLOR = (208, 213, 221)
HEADER_BG = (68, 114, 196)
HEADER_FG = (255, 255, 255)
ALT_ROW_BG = (235, 243, 251)
ROW_BG = (255, 255, 255)
TEXT_COLOR = (32, 38, 46)


def detect_tables(content: str) -> list[str]:
    """Return all GFM pipe table blocks found in *content*."""
    return _TABLE_PATTERN.findall(content)


def has_tables(content: str) -> bool:
    """Return True if *content* contains at least one GFM pipe table."""
    return bool(_TABLE_PATTERN.search(content))


# Inline markdown constructs we collapse to plain text before drawing a cell.
# A table is rendered as a PNG, so none of these can be made interactive — a
# link in an image is not clickable — and leaving the raw syntax in just leaks
# noise into the cell (#415).
_CODE_SPAN = re.compile(r"`+([^`]*?)`+")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_BOLD = re.compile(r"\*\*([^*]+?)\*\*")
_ITALIC = re.compile(r"\*([^*]+?)\*")
_STRIKE = re.compile(r"~~([^~]+?)~~")
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")

# Strikethrough is the one inline construct an image *can* express, so unlike
# bold/links (which collapse to plain text, #415) it survives as a marked span
# and is drawn as a real rule (#607). The markers are private control chars:
# they never reach the canvas, never count toward width, and are stripped from
# untrusted input so a cell cannot switch strike state on for the whole table.
_STRIKE_OPEN = "\x02"
_STRIKE_CLOSE = "\x03"
_STRIKE_MARKS = frozenset((_STRIKE_OPEN, _STRIKE_CLOSE))


def _strip_inline_markdown(text: str) -> str:
    """Reduce inline GFM markdown in *text* to the plain text a reader wants.

    The cell is drawn into an image, so inline syntax cannot be made
    interactive; rendering it verbatim only leaks ``**``, ``[label](url)`` and
    backticks into the picture. Each construct collapses to its display text:

    - ``[label](url)`` / ``![alt](url)`` -> ``label`` (the URL is unclickable in
      an image, so only the label is kept — this also removes long, noisy URLs)
    - ``**bold**`` -> ``bold``; ``*italic*`` -> ``italic``
    - ``~~s~~`` -> ``s`` wrapped in private strike markers, so
      :func:`render_table_image` can draw the rule the message body shows
      (#607). Use :func:`_split_strike_runs` to read them back.
    - `` `code` `` -> ``code``

    Underscore emphasis (``_x_`` / ``__x__``) is intentionally **not** stripped:
    identifiers like ``_is_allowed`` / ``__init__`` / ``foo_bar`` use
    underscores, and GFM likewise does not treat intra-word ``_`` as emphasis.
    Code-span contents are protected so symbols inside them survive untouched.
    """
    if not text:
        return text

    # 0. Strip any pre-existing marker chars: they are ours, and a cell that
    #    contained them could otherwise turn strike on for the rest of the row.
    text = _strip_strike_markers(text)

    # 1. Stash code-span contents so emphasis stripping can't touch symbols
    #    inside them (e.g. `a*b*c` must not lose its asterisks).
    saved: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        saved.append(match.group(1))
        return f"\x00{len(saved) - 1}\x00"

    text = _CODE_SPAN.sub(_stash, text)

    # 2. Links / images -> label, then emphasis / strikethrough.
    text = _LINK.sub(r"\1", text)
    text = _STRIKE.sub(rf"{_STRIKE_OPEN}\1{_STRIKE_CLOSE}", text)
    text = _BOLD.sub(r"\1", text)  # before italic so ***x*** -> *x* -> x
    text = _ITALIC.sub(r"\1", text)

    # 3. Restore the protected code-span contents (without the backticks).
    return _PLACEHOLDER.sub(lambda m: saved[int(m.group(1))], text)


def _strip_strike_markers(text: str) -> str:
    """Return *text* without the private strikethrough markers."""
    return text.replace(_STRIKE_OPEN, "").replace(_STRIKE_CLOSE, "")


def _split_strike_runs(text: str) -> list[tuple[str, bool]]:
    """Split *text* into ``(substring, struck)`` runs on the strike markers.

    Adjacent runs of the same state are merged, and the markers themselves are
    dropped — callers get only visible text. Unbalanced markers are tolerated:
    an unclosed span simply runs to the end of *text*.
    """
    if not text:
        return []
    runs: list[tuple[str, bool]] = []
    buf: list[str] = []
    struck = False

    def flush() -> None:
        if buf:
            if runs and runs[-1][1] == struck:
                runs[-1] = (runs[-1][0] + "".join(buf), struck)
            else:
                runs.append(("".join(buf), struck))
            buf.clear()

    for ch in text:
        if ch == _STRIKE_OPEN:
            flush()
            struck = True
        elif ch == _STRIKE_CLOSE:
            flush()
            struck = False
        else:
            buf.append(ch)
    flush()
    return runs


def _balance_strike_markers(text: str) -> str:
    """Re-open/close strike markers per line so each line stands on its own.

    Wrapping can cut a struck span in half; without this the continuation lines
    would lose the rule (or, worse, close a span they never opened).
    """
    out: list[str] = []
    open_ = False
    for line in text.split("\n"):
        prefix = _STRIKE_OPEN if open_ else ""
        for ch in line:
            if ch == _STRIKE_OPEN:
                open_ = True
            elif ch == _STRIKE_CLOSE:
                open_ = False
        out.append(prefix + line + (_STRIKE_CLOSE if open_ else ""))
    return "\n".join(out)


def _display_width(text: str) -> int:
    """Display width of *text*, counting East Asian Wide/Fullwidth glyphs as 2.

    Strike markers are metadata, not glyphs — counting them would silently
    shrink the wrap budget of every struck cell.
    """
    return sum(
        0 if c in _STRIKE_MARKS else (2 if unicodedata.east_asian_width(c) in ("W", "F") else 1)
        for c in text
    )


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


def _prepare_cell(text: str, max_width: int) -> str:
    """Turn a raw cell into drawable lines: collapse markdown, wrap, re-balance.

    The three steps belong together — wrapping runs on the *collapsed* text (so
    syntax does not eat the column budget) and the strike markers must be
    re-balanced *after* wrapping (so a span cut in half keeps its rule on every
    line).
    """
    return _balance_strike_markers(_wrap_cell(_strip_inline_markdown(text), max_width))


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

    # Collapse inline markdown (links/bold/code) to plain text — it can't be
    # made interactive in an image and otherwise leaks as raw syntax (#415) —
    # keep strikethrough as a marked span (it *is* drawable, #607), then wrap
    # every cell so lines stay within MAX_COL_WIDTH (bounds image width).
    grid = [[_prepare_cell(cell, MAX_COL_WIDTH) for cell in row] for row in grid]

    ascent, descent = text_font.getmetrics()
    text_h = ascent + descent
    emoji_h = int(FONT_SIZE * 1.2)
    line_h = max(text_h, emoji_h) + LINE_GAP

    scratch = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    tile_cache: dict[str, object | None] = {}

    def line_width(line: str) -> float:
        width = 0.0
        for chunk, _struck in _split_strike_runs(line):
            for text, is_emoji in _segment_runs(chunk):
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

    # Rule height: ~0.4em above the baseline. That is the vertical middle of a
    # CJK ideograph and still crosses Latin lowercase, so mixed 日本語 + ASCII
    # cells (the common case here) get one line at a sensible height.
    strike_dy = ascent - round(FONT_SIZE * STRIKE_EM_ABOVE_BASELINE)

    def draw_line(line: str, x0: int, y0: int, fill: tuple[int, int, int]) -> None:
        x = float(x0)
        for chunk, struck in _split_strike_runs(line):
            run_x0 = x
            for text, is_emoji in _segment_runs(chunk):
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
            if struck and x > run_x0:
                sy = y0 + strike_dy
                draw.line([(run_x0, sy), (x, sy)], fill=fill, width=STRIKE_WIDTH)

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


def get_table_images(content: str, *, limit: int = MAX_TABLE_IMAGES) -> list[tuple[str, bytes]]:
    """Return (filename, png_bytes) pairs for the tables in *content*.

    Returns an empty list when ``CLORD_RENDER_TABLE_IMAGES`` is not enabled
    or rendering is unavailable.  Callers wrap each pair into a
    ``discord.File(BytesIO(png_bytes), filename=filename)``.

    At most *limit* images are returned (#683).  Discord rejects a message
    carrying more than :data:`MAX_TABLE_IMAGES` attachments, and that rejection
    would lose the **whole message**, text included — so a body with more tables
    than that keeps its extra tables as raw markdown instead.  Callers that
    attach a file of their own (``progress.txt``) pass a smaller limit to leave
    room for it.
    """
    if os.getenv("CLORD_RENDER_TABLE_IMAGES", "").lower() not in ("1", "true", "yes"):
        return []
    if limit <= 0:
        return []
    result = []
    tables = detect_tables(content)
    for i, table_md in enumerate(tables, start=1):
        if len(result) >= limit:
            logger.info(
                "table_renderer: %d table(s) in a %d-char body exceed the "
                "%d-attachment budget — the rest stay as raw markdown (#683)",
                len(tables),
                len(content),
                limit,
            )
            break
        img_bytes = render_table_image(table_md)
        if img_bytes:
            result.append((f"table_{i}.png", img_bytes))
    return result
