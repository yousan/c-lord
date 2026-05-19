"""Markdown table detection and image rendering for Discord.

GFM pipe tables are detected in message content and optionally rendered as
PNG images via matplotlib (optional dependency: ``pip install c-lord[table]``).

Controlled by the ``CLORD_RENDER_TABLE_IMAGES`` environment variable.
"""

from __future__ import annotations

import os
import re
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


def detect_tables(content: str) -> list[str]:
    """Return all GFM pipe table blocks found in *content*."""
    return _TABLE_PATTERN.findall(content)


def has_tables(content: str) -> bool:
    """Return True if *content* contains at least one GFM pipe table."""
    return bool(_TABLE_PATTERN.search(content))


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

    # Try to use a Japanese-capable font if available
    jp_fonts = ["Noto Sans CJK JP", "IPAexGothic", "Hiragino Sans", "Yu Gothic"]
    font_prop = None
    for fname in jp_fonts:
        candidates = [
            f for f in font_manager.fontManager.ttflist if fname.lower() in f.name.lower()
        ]
        if candidates:
            font_prop = candidates[0].name
            break

    n_cols = len(headers)
    n_rows = len(rows)

    # Normalize row widths
    rows_padded = [r + [""] * max(0, n_cols - len(r)) for r in rows]
    rows_padded = [r[:n_cols] for r in rows_padded]

    fig_w = max(4, n_cols * 1.8)
    fig_h = max(1.2, (n_rows + 1) * 0.45)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis("off")

    tbl = ax.table(
        cellText=rows_padded,
        colLabels=headers,
        loc="center",
        cellLoc="left",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(11)
    tbl.auto_set_column_width(range(n_cols))

    if font_prop:
        for cell in tbl.get_celld().values():
            cell.get_text().set_fontfamily(font_prop)

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
    plt.savefig(buf, format="png", bbox_inches="tight", dpi=150, pad_inches=0.1)
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
