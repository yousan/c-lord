"""Markdown table detection and image rendering for Discord.

GFM pipe tables are detected in message content and optionally rendered as
PNG images via matplotlib (optional dependency: ``pip install c-lord[table]``).

Controlled by the ``CLORD_RENDER_TABLE_IMAGES`` environment variable.
"""

from __future__ import annotations

import os
import re
import unicodedata
from io import BytesIO

# GFM pipe table pattern:
#   header row  : | ... |
#   separator   : | ---  / :--- / ---: / :---: |
#   1+ data rows: | ... |
_TABLE_PATTERN = re.compile(
    r"(?m)"
    r"(\|[^\n]+\|\n"  # header row
    r"\|[-:| ]+\|\n"  # separator row
    r"(?:\|[^\n]+\|\n?)+)",  # one or more data rows
)

# Rendering layout knobs.
MAX_COL_WIDTH = 48  # max display width (CJK = 2) per column before wrapping
FONT_SIZE = 12
CELL_PAD = 0.08  # horizontal text inset as a fraction of cell width
COL_WIDTH_INCH = 0.10  # figure inches per display-width unit
LINE_HEIGHT_INCH = 0.46  # figure inches per wrapped text line (vertical padding)

# Font file paths checked before name-based lookup (faster, deterministic).
_JP_FONT_PATHS = (
    os.path.expanduser("~/.local/share/fonts/NotoSansJP.ttf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
)
_JP_FONT_NAMES = ("Noto Sans JP", "Noto Sans CJK JP", "IPAexGothic", "Hiragino Sans", "Yu Gothic")
_EMOJI_FONT_PATHS = (
    os.path.expanduser("~/.local/share/fonts/NotoEmoji-Regular.ttf"),
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
)
_EMOJI_FONT_NAMES = ("Noto Emoji", "Symbola")


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


def _resolve_font(font_manager) -> object | None:
    """Build a FontProperties with a JP→Emoji→DejaVu fallback chain.

    matplotlib (>=3.6) falls back per glyph through the family list, so a
    Japanese-capable font handles CJK while an emoji font covers glyphs the JP
    font lacks (previously rendered as tofu boxes). Returns None only when no
    custom font is found, leaving matplotlib's default.
    """
    families: list[str] = []

    def _register(paths: tuple[str, ...], names: tuple[str, ...]) -> None:
        for path in paths:
            if os.path.exists(path):
                font_manager.fontManager.addfont(path)
                families.append(font_manager.FontProperties(fname=path).get_name())
                return
        available = {f.name for f in font_manager.fontManager.ttflist}
        for name in names:
            match = next((n for n in available if name.lower() in n.lower()), None)
            if match:
                families.append(match)
                return

    _register(_JP_FONT_PATHS, _JP_FONT_NAMES)
    _register(_EMOJI_FONT_PATHS, _EMOJI_FONT_NAMES)
    if not families:
        return None
    families.append("DejaVu Sans")
    return font_manager.FontProperties(family=families)


def render_table_image(table_md: str) -> bytes | None:
    """Render a GFM pipe table as a PNG image.

    Returns PNG bytes, or None if rendering is unavailable (matplotlib not
    installed) or the table cannot be parsed.
    """
    parsed = _parse_table(table_md)
    if parsed is None:
        return None
    headers, rows = parsed

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        return None

    font_prop = _resolve_font(font_manager)

    n_cols = len(headers)
    rows_padded = [(r + [""] * max(0, n_cols - len(r)))[:n_cols] for r in rows]

    # Wrap every cell so no line exceeds MAX_COL_WIDTH; bounds the figure width.
    headers_w = [_wrap_cell(h, MAX_COL_WIDTH) for h in headers]
    rows_w = [[_wrap_cell(c, MAX_COL_WIDTH) for c in r] for r in rows_padded]
    n_rows = len(rows_w)
    all_rows = [headers_w, *rows_w]

    # Per-column display width (longest wrapped line) and per-row line count.
    col_w = [
        max(1, max(_display_width(line) for r in all_rows for line in r[c].split("\n")))
        for c in range(n_cols)
    ]
    total_w = sum(col_w)
    line_counts = [max(cell.count("\n") + 1 for cell in r) for r in all_rows]
    total_lines = sum(line_counts)

    fig_w = max(4.0, total_w * COL_WIDTH_INCH + n_cols * 0.25)
    fig_h = max(1.0, total_lines * LINE_HEIGHT_INCH)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis("off")

    tbl = ax.table(
        cellText=rows_w,
        colLabels=headers_w,
        loc="center",
        cellLoc="left",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(FONT_SIZE)

    # Explicit column widths (bounded) and row heights (grow with wrapped lines).
    for col in range(n_cols):
        width = col_w[col] / total_w
        for row in range(n_rows + 1):
            cell = tbl[row, col]
            cell.set_width(width)
            cell.PAD = CELL_PAD
    for row in range(n_rows + 1):
        height = line_counts[row] / total_lines
        for col in range(n_cols):
            tbl[row, col].set_height(height)

    if font_prop:
        for cell in tbl.get_celld().values():
            cell.get_text().set_font_properties(font_prop)

    # Style header row
    for col in range(n_cols):
        cell = tbl[0, col]
        cell.set_facecolor("#4472C4")
        cell.get_text().set_color("white")
        cell.get_text().set_fontweight("bold")

    # Alternate row shading
    for row in range(1, n_rows + 1):
        color = "#EBF3FB" if row % 2 == 0 else "white"
        for col in range(n_cols):
            tbl[row, col].set_facecolor(color)

    buf = BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", dpi=150, pad_inches=0.15)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def get_table_images(content: str) -> list[tuple[str, bytes]]:
    """Return (filename, png_bytes) pairs for all tables in *content*.

    Returns an empty list when ``CLORD_RENDER_TABLE_IMAGES`` is not enabled
    or matplotlib is unavailable.  Callers wrap each pair into a
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
