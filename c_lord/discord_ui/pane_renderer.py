"""Render a tmux pane's current screen (ANSI text) to a PNG image (#285).

``tmux capture-pane -e -p`` returns the visible pane with ANSI escape
sequences preserved. :func:`parse_ansi` turns that into styled runs and
:func:`render_pane_png` paints them onto a terminal-style dark canvas, so a
Discord ``/tmux-screenshot`` keeps the colors, layout, and 🟢/🔴 status lamps
that a plain text capture would lose.

Pillow is an optional dependency (``pip install c-lord[table]``);
:func:`render_pane_png` returns ``None`` when Pillow or a usable font is
missing, mirroring :mod:`c_lord.discord_ui.table_renderer`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING

from .fonts import load_emoji_font, load_mono_font, load_text_font
from .table_renderer import _display_width, _segment_runs

if TYPE_CHECKING:
    from PIL import Image as PILImage

# ── Palette ─────────────────────────────────────────────────────────────────
# 16-color ANSI palette (VS Code dark theme values). Index 0-7 are the standard
# colors, 8-15 their bright variants.
ANSI_16: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0),  # 0 black
    (205, 49, 49),  # 1 red
    (13, 188, 121),  # 2 green
    (229, 229, 16),  # 3 yellow
    (36, 114, 200),  # 4 blue
    (188, 63, 188),  # 5 magenta
    (17, 168, 205),  # 6 cyan
    (229, 229, 229),  # 7 white
    (102, 102, 102),  # 8 bright black (gray)
    (241, 76, 76),  # 9 bright red
    (35, 209, 139),  # 10 bright green
    (245, 245, 67),  # 11 bright yellow
    (59, 142, 234),  # 12 bright blue
    (214, 112, 214),  # 13 bright magenta
    (41, 184, 219),  # 14 bright cyan
    (255, 255, 255),  # 15 bright white
)

# xterm 256-color cube steps.
_CUBE_STEPS = (0, 95, 135, 175, 215, 255)

DEFAULT_FG = (201, 209, 217)  # GitHub-dark default foreground
DEFAULT_BG = (13, 17, 23)  # GitHub-dark canvas

# ── Layout knobs (pixels) ────────────────────────────────────────────────────
FONT_SIZE = 22
EMOJI_STRIKE = 109  # Noto Color Emoji bitmap strike — render large, scale down
LINE_GAP = 4
MARGIN = 12
# Safety caps so a pathological pane can't request a giant allocation.
MAX_COLS = 400
MAX_ROWS = 200


def color_256(n: int) -> tuple[int, int, int]:
    """Map an xterm 256-color index to an RGB tuple."""
    n &= 0xFF
    if n < 16:
        return ANSI_16[n]
    if n < 232:
        c = n - 16
        return (_CUBE_STEPS[c // 36], _CUBE_STEPS[(c % 36) // 6], _CUBE_STEPS[c % 6])
    level = 8 + (n - 232) * 10
    return (level, level, level)


@dataclass(frozen=True)
class Style:
    """A run's visual attributes. ``None`` color means terminal default."""

    fg: tuple[int, int, int] | None = None
    bg: tuple[int, int, int] | None = None
    bold: bool = False
    dim: bool = False
    inverse: bool = False


# OSC sequences (e.g. OSC 8 hyperlinks) — drop them entirely; the visible link
# text lives between the two OSC markers as normal text.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# CSI: ESC [ <params> <intermediates> <final>. Only ``m`` (SGR) carries color.
_CSI_RE = re.compile(r"\x1b\[([0-9;:?]*)[ -/]*([@-~])")


def _parse_ext_color(codes: list[int], i: int) -> tuple[tuple[int, int, int] | None, int]:
    """Parse a 38/48 extended-color sequence starting at ``codes[i]``.

    Returns ``(color, consumed)`` where *consumed* counts the codes used
    (including the leading 38/48).
    """
    if i + 2 < len(codes) and codes[i + 1] == 5:
        return color_256(codes[i + 2]), 3
    if i + 4 < len(codes) and codes[i + 1] == 2:
        return (codes[i + 2] & 0xFF, codes[i + 3] & 0xFF, codes[i + 4] & 0xFF), 5
    return None, 1


def _apply_sgr(style: Style, params: str) -> Style:
    """Return a new Style after applying one SGR (``\\x1b[...m``) parameter list."""
    parts = params.split(";") if params else ["0"]
    codes = [int(p) if p.isdigit() else 0 for p in parts]
    fg, bg = style.fg, style.bg
    bold, dim, inverse = style.bold, style.dim, style.inverse
    i = 0
    while i < len(codes):
        c = codes[i]
        if c == 0:
            fg = bg = None
            bold = dim = inverse = False
        elif c == 1:
            bold = True
        elif c == 2:
            dim = True
        elif c == 22:
            bold = dim = False
        elif c == 7:
            inverse = True
        elif c == 27:
            inverse = False
        elif 30 <= c <= 37:
            fg = ANSI_16[c - 30]
        elif c == 39:
            fg = None
        elif 40 <= c <= 47:
            bg = ANSI_16[c - 40]
        elif c == 49:
            bg = None
        elif 90 <= c <= 97:
            fg = ANSI_16[c - 90 + 8]
        elif 100 <= c <= 107:
            bg = ANSI_16[c - 100 + 8]
        elif c in (38, 48):
            color, consumed = _parse_ext_color(codes, i)
            if c == 38:
                fg = color
            else:
                bg = color
            i += consumed
            continue
        # Other codes (italic, underline, blink, ...) are parsed but not drawn.
        i += 1
    return Style(fg=fg, bg=bg, bold=bold, dim=dim, inverse=inverse)


def parse_ansi(text: str) -> list[list[tuple[str, Style]]]:
    """Parse ANSI text into lines, each a list of ``(run_text, Style)`` runs."""
    text = _OSC_RE.sub("", text)
    lines: list[list[tuple[str, Style]]] = []
    current: list[tuple[str, Style]] = []
    buf: list[str] = []
    style = Style()

    def flush() -> None:
        if buf:
            current.append(("".join(buf), style))
            buf.clear()

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            flush()
            lines.append(current)
            current = []
            i += 1
            continue
        if ch == "\x1b" and i + 1 < n and text[i + 1] == "[":
            m = _CSI_RE.match(text, i)
            if m:
                flush()
                if m.group(2) == "m":
                    style = _apply_sgr(style, m.group(1))
                i = m.end()
                continue
            i += 1  # malformed CSI — drop the ESC
            continue
        if ch == "\x1b":
            i += 2  # other escape (ESC + one byte) — drop both
            continue
        buf.append(ch)
        i += 1

    flush()
    if current:
        lines.append(current)
    return lines


# ── Rendering ────────────────────────────────────────────────────────────────


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """Linear blend of *a* toward *b* by fraction *t*."""
    return tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3))  # type: ignore[return-value]


def _emoji_tile(
    cluster: str,
    emoji_font: object,
    is_color: bool,
    target_h: int,
    cache: dict[str, object | None],
) -> object | None:
    """Render one emoji cluster to an RGBA tile scaled to *target_h* pixels."""
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
            font=emoji_font,  # type: ignore[arg-type]
            embedded_color=is_color,
            fill=None if is_color else DEFAULT_FG,
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


def _layout(lines: list[list[tuple[str, Style]]]) -> list[list[tuple[str, int, Style, bool]]]:
    """Flatten parsed lines into rows of ``(text, cell_width, style, is_emoji)``."""
    grid: list[list[tuple[str, int, Style, bool]]] = []
    for line in lines[:MAX_ROWS]:
        row: list[tuple[str, int, Style, bool]] = []
        cols = 0
        for run_text, style in line:
            for seg, is_emoji in _segment_runs(run_text):
                if is_emoji:
                    if cols + 2 > MAX_COLS:
                        break
                    row.append((seg, 2, style, True))
                    cols += 2
                else:
                    for char in seg:
                        w = _display_width(char)
                        if w <= 0:
                            continue
                        if cols + w > MAX_COLS:
                            break
                        row.append((char, w, style, False))
                        cols += w
        grid.append(row)
    return grid


def build_status_bar_ansi(
    session: str,
    tabs: list[tuple[int, str, bool]],
    current_window: str | None,
    width: int,
) -> str:
    """Build a tmux-style status bar as an ANSI string (one full-width line).

    Mirrors tmux's bottom bar: ``[session]`` followed by ``index:name`` window
    tabs on a green background. The session-active window is suffixed with
    ``*`` (tmux convention); the *current_window* (the one being screenshotted)
    is inverse-highlighted so it's obvious which pane the image shows. The line
    is padded with spaces to *width* display columns so it renders as a solid
    full-width bar. The result is fed back through :func:`parse_ansi`.
    """
    parts: list[tuple[str, bool]] = [(f"[{session}]", False)]
    for idx, name, active in tabs:
        label = f" {idx}:{name}" + ("*" if active else "")
        parts.append((label, name == current_window))

    visible = sum(_display_width(text) for text, _ in parts)
    pad = max(0, width - visible)

    out = ["\x1b[42m\x1b[30m"]  # tmux default status: green bg, black fg
    for text, is_current in parts:
        if is_current:
            out.append(f"\x1b[1m\x1b[7m{text}\x1b[27m\x1b[22m")  # bold + inverse
        else:
            out.append(text)
    out.append(" " * pad)
    out.append("\x1b[0m")
    return "".join(out)


def render_pane_png(
    ansi_text: str,
    status_bar: tuple[str, list[tuple[int, str, bool]], str | None] | None = None,
) -> bytes | None:
    """Render *ansi_text* (from ``capture-pane -e -p``) to PNG bytes.

    When *status_bar* is ``(session, tabs, current_window)``, a tmux-style
    status bar row is appended at the bottom (see :func:`build_status_bar_ansi`).

    Returns ``None`` when Pillow or a usable monospace font is unavailable.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    mono = load_mono_font(FONT_SIZE)
    if mono is None:
        return None
    text_font = load_text_font(FONT_SIZE) or mono
    emoji_font, emoji_is_color = load_emoji_font(EMOJI_STRIKE, FONT_SIZE)

    grid = _layout(parse_ansi(ansi_text))

    if status_bar is not None:
        session, tabs, current_window = status_bar
        n_cols_pane = max((sum(w for _, w, _, _ in row) for row in grid), default=1)
        bar_ansi = build_status_bar_ansi(session, tabs, current_window, max(n_cols_pane, 1))
        grid = grid + _layout(parse_ansi(bar_ansi))

    cell_w = max(1, round(mono.getlength("M")))
    ascent, descent = mono.getmetrics()
    cell_h = ascent + descent + LINE_GAP

    n_cols = max((sum(w for _, w, _, _ in row) for row in grid), default=1)
    n_rows = max(len(grid), 1)
    width = MARGIN * 2 + max(1, n_cols) * cell_w
    height = MARGIN * 2 + n_rows * cell_h

    img: PILImage.Image = Image.new("RGB", (width, height), DEFAULT_BG)
    draw = ImageDraw.Draw(img)
    emoji_cache: dict[str, object | None] = {}

    y = MARGIN
    for row in grid:
        x = MARGIN
        for text, cell_width, style, is_emoji in row:
            span = cell_width * cell_w
            eff_fg = style.fg if style.fg is not None else DEFAULT_FG
            eff_bg = style.bg
            if style.inverse:
                eff_fg, eff_bg = (eff_bg if eff_bg is not None else DEFAULT_BG), eff_fg
            if eff_bg is not None:
                draw.rectangle((x, y, x + span - 1, y + cell_h - 1), fill=eff_bg)
            if style.dim:
                eff_fg = _blend(eff_fg, DEFAULT_BG, 0.45)

            if is_emoji:
                tile = (
                    _emoji_tile(text, emoji_font, emoji_is_color, max(1, ascent), emoji_cache)
                    if emoji_font is not None
                    else None
                )
                if tile is not None:
                    img.paste(tile, (x, y + (cell_h - tile.height) // 2), tile)  # type: ignore[attr-defined]
                else:
                    draw.text((x, y), text, font=text_font, fill=eff_fg)
            else:
                draw.text((x, y), text, font=mono if cell_width == 1 else text_font, fill=eff_fg)
            x += span
        y += cell_h

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
