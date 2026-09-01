"""Render a *hand-authored* Discord conversation to a PNG mockup (#316).

This is a c-lord **internal dev tool**, not a shipped consumer feature. It draws
a Discord-looking image from data you author by hand (``ConvMessage`` objects /
a spec dict), so you can mock up *the intended look* of a feature — author rows,
message text, the ``Bash(...)`` / ``✅ Done`` tool-use embeds, the 🧠/🛠️/✅
status-lamp reactions, attachments — and attach it to an Issue as a **design
comp** ("this is the goal"). Because it renders from arbitrary data it can show
states that don't exist yet, which a real screenshot cannot.

It is deliberately *not* a capture of the real Discord client (that would need a
self-bot, forbidden by Discord's ToS — see #243). For evidence of the real
client's actual rendering, a human screenshot remains authoritative.

:func:`render_conversation_png` paints with Pillow and returns ``bytes | None``
(``None`` when Pillow / a usable font is missing), mirroring
:mod:`c_lord.discord_ui.pane_renderer`. The CLI lives in
``scripts/discord_mockup.py``.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING

from .fonts import load_emoji_font, load_mono_font, load_text_font
from .table_renderer import _emoji_tile, _segment_runs

if TYPE_CHECKING:
    from collections.abc import Iterable

    from PIL import Image as PILImage
    from PIL import ImageDraw as PILImageDraw
    from PIL import ImageFont

logger = logging.getLogger(__name__)


# ── Data model (decoupled from live discord.py objects, for testability) ──────


@dataclass(frozen=True)
class ConvAttachment:
    """A file attached to a message."""

    filename: str
    content_type: str | None = None


@dataclass(frozen=True)
class ConvField:
    """One name/value field inside an embed."""

    name: str
    value: str
    inline: bool = False


@dataclass(frozen=True)
class ConvEmbed:
    """A Discord embed — renders the ``Bash(...)`` / ``✅ Done`` tool-use cards."""

    title: str | None = None
    description: str | None = None
    color: int | None = None
    fields: tuple[ConvField, ...] = ()


@dataclass(frozen=True)
class ConvReaction:
    """A reaction pill — these carry the 🧠/🛠️/✅ status lamps."""

    emoji: str
    count: int = 1


@dataclass(frozen=True)
class ConvMessage:
    """One normalized message ready to render, free of discord.py types."""

    author: str
    content: str = ""
    is_bot: bool = False
    timestamp: str | None = None
    color: int | None = None
    embeds: tuple[ConvEmbed, ...] = ()
    reactions: tuple[ConvReaction, ...] = ()
    attachments: tuple[ConvAttachment, ...] = field(default_factory=tuple)


def _opt_str(v: object) -> str | None:
    """Coerce an optional spec value to ``str`` (``None`` stays ``None``).

    Keeps a numeric authoring slip (e.g. ``"timestamp": 1200``) from reaching the
    renderer as an int, which Pillow would crash on. Mirrors the ``str(...)``
    coercion applied to the required scalar fields.
    """
    return str(v) if v is not None else None


def _to_int_color(c: object) -> int | None:
    """Accept an int or ``"#RRGGBB"`` / ``"RRGGBB"`` string → 0xRRGGBB int (0 → None)."""
    if isinstance(c, bool):
        return None
    if isinstance(c, int):
        return c or None
    if isinstance(c, str):
        try:
            return int(c.strip().lstrip("#"), 16) or None
        except ValueError:
            return None
    return None


def _reaction_from_spec(r: object) -> ConvReaction:
    if isinstance(r, dict):
        return ConvReaction(emoji=str(r.get("emoji", "")), count=int(r.get("count", 1)))
    if isinstance(r, (list, tuple)):
        return ConvReaction(emoji=str(r[0]), count=int(r[1]) if len(r) > 1 else 1)
    return ConvReaction(emoji=str(r), count=1)


def _attachment_from_spec(a: object) -> ConvAttachment:
    if isinstance(a, dict):
        return ConvAttachment(
            filename=str(a.get("filename", "file")), content_type=a.get("content_type")
        )
    return ConvAttachment(filename=str(a))


def _embed_from_spec(e: dict) -> ConvEmbed:
    fields = tuple(
        ConvField(
            name=str(f.get("name", "")),
            value=str(f.get("value", "")),
            inline=bool(f.get("inline", False)),
        )
        for f in (e.get("fields") or [])
    )
    return ConvEmbed(
        title=_opt_str(e.get("title")),
        description=_opt_str(e.get("description")),
        color=_to_int_color(e.get("color")),
        fields=fields,
    )


def conversation_from_spec(data: object) -> list[ConvMessage]:
    """Build ``ConvMessage``\\ s from a hand-authored spec (the design-comp input).

    *data* is a list of message dicts, or ``{"messages": [...]}``. Each message:
    ``{author, content?, is_bot?, timestamp?, color?, embeds?, reactions?,
    attachments?}``. ``color`` accepts an int or ``"#RRGGBB"``. ``embeds`` is a
    list of ``{title?, description?, color?, fields?: [{name, value, inline?}]}``.
    ``reactions`` accepts ``[{emoji, count?}]`` or ``["🧠", ...]``; ``attachments``
    accepts ``[{filename, content_type?}]`` or ``["diff.patch", ...]``.
    """
    messages = data.get("messages", []) if isinstance(data, dict) else data
    out: list[ConvMessage] = []
    for m in messages or []:
        out.append(
            ConvMessage(
                author=str(m.get("author", "unknown")),
                content=str(m.get("content", "")),
                is_bot=bool(m.get("is_bot", False)),
                timestamp=_opt_str(m.get("timestamp")),
                color=_to_int_color(m.get("color")),
                embeds=tuple(_embed_from_spec(e) for e in (m.get("embeds") or [])),
                reactions=tuple(_reaction_from_spec(r) for r in (m.get("reactions") or [])),
                attachments=tuple(_attachment_from_spec(a) for a in (m.get("attachments") or [])),
            )
        )
    return out


def load_spec_file(path: str) -> list[ConvMessage]:
    """Load a JSON design-comp spec file → ``ConvMessage``\\ s."""
    with open(path, encoding="utf-8") as fh:
        return conversation_from_spec(json.load(fh))


# ── Palette / layout knobs (Discord dark theme) ──────────────────────────────

WIDTH = 860
MARGIN = 16
AVATAR = 40
GUTTER = MARGIN + AVATAR + 14  # x where text columns start
MSG_GAP = 14

BG = (49, 51, 56)  # #313338 chat background
EMBED_BG = (43, 45, 49)  # #2b2d31 embed / code card
CODE_BG = (30, 31, 34)  # #1e1f22 code block
CHIP_BG = (60, 63, 69)  # reaction / attachment pill
TEXT = (219, 222, 225)  # #dbdee1 body text
EMBED_TEXT = (197, 201, 207)
MUTED = (148, 155, 164)  # #949ba4 timestamps / secondary
NAME_DEFAULT = (242, 243, 245)
BADGE_BG = (88, 101, 242)  # #5865f2 "BOT" / "APP" badge
WHITE = (255, 255, 255)
EMBED_BAR_DEFAULT = (79, 84, 92)

BODY_SIZE = 19
NAME_SIZE = 19
TIME_SIZE = 13
MONO_SIZE = 16
BADGE_SIZE = 12

_AVATAR_COLORS = (
    (88, 101, 242),
    (87, 242, 135),
    (254, 231, 92),
    (235, 69, 158),
    (250, 166, 26),
    (0, 168, 252),
)


def _rgb(color: int | None) -> tuple[int, int, int] | None:
    """Convert a 0xRRGGBB int to an (r, g, b) tuple, or None."""
    if color is None:
        return None
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


# ── Pillow renderer ───────────────────────────────────────────────────────────


@dataclass
class _Fonts:
    body: ImageFont.FreeTypeFont
    name: ImageFont.FreeTypeFont
    time: ImageFont.FreeTypeFont
    mono: ImageFont.FreeTypeFont
    badge: ImageFont.FreeTypeFont
    emoji: object | None
    emoji_color: bool
    # #623: the monospace faces we can find (DejaVu / Liberation Mono) carry no
    # CJK glyphs, so a Japanese code block used to render as tofu. Code blocks
    # draw wide glyphs with this proportional CJK face instead.
    mono_cjk: object | None = None
    mono_cell_w: float = 0.0
    mono_cjk_dy: float = 0.0


@dataclass
class _MonoGrid:
    """Monospace cell metrics for the code-block path (#623).

    ``cjk`` is the face wide glyphs are drawn with; ``cell_w`` is the width of
    one monospace cell. A wide glyph is placed on a **two-cell** advance so a
    table with Japanese in it still lines up, the way Discord renders it.
    """

    cjk: object | None
    cell_w: float
    #: baseline correction so the CJK face sits on the monospace baseline.
    cjk_dy: float = 0.0


def _wide_runs(text: str) -> list[tuple[str, bool]]:
    """Split *text* into consecutive runs of (segment, is_fullwidth).

    Width comes from ``unicodedata.east_asian_width`` — the same rule
    ``status_view`` pads its tables by, so the mockup matches the real message.
    """
    runs: list[tuple[str, bool]] = []
    for ch in text:
        wide = unicodedata.east_asian_width(ch) in ("W", "F")
        if runs and runs[-1][1] == wide:
            runs[-1] = (runs[-1][0] + ch, wide)
        else:
            runs.append((ch, wide))
    return runs


def _measure_rich(
    draw: PILImageDraw.ImageDraw,
    text: str,
    font: object,
    emoji_w: float,
    *,
    grid: _MonoGrid | None = None,
) -> float:
    """Pixel width of *text*, treating each emoji cluster as *emoji_w* wide.

    With *grid* (code blocks), width comes from the monospace cell count — one
    cell per narrow glyph, two per wide one — so it matches what ``_draw_rich``
    actually advances rather than the proportional CJK face's own metrics.
    """
    total = 0.0
    for seg, is_emoji in _segment_runs(text):
        if is_emoji:
            total += emoji_w
        elif grid is not None:
            for run, wide in _wide_runs(seg):
                total += len(run) * grid.cell_w * (2 if wide else 1)
        else:
            total += draw.textlength(seg, font=font)  # type: ignore[arg-type]
    return total


def _wrap_rich(
    draw: PILImageDraw.ImageDraw,
    text: str,
    font: object,
    max_px: float,
    emoji_w: float,
    *,
    grid: _MonoGrid | None = None,
) -> list[str]:
    """Word-wrap *text* to *max_px*, hard-breaking overlong tokens by cluster."""
    import re

    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        cur_w = 0.0
        for tok in re.findall(r"\S+\s*|\s+", para) or [para]:
            tok_w = _measure_rich(draw, tok, font, emoji_w, grid=grid)
            if tok_w > max_px:  # token alone exceeds a line — flush then char-break
                if cur:
                    lines.append(cur.rstrip())
                    cur, cur_w = "", 0.0
                for seg, is_emoji in _segment_runs(tok):
                    units: Iterable[str] = [seg] if is_emoji else list(seg)
                    for u in units:
                        if is_emoji:
                            uw = emoji_w
                        elif grid is not None:
                            uw = _measure_rich(draw, u, font, emoji_w, grid=grid)
                        else:
                            uw = draw.textlength(u, font=font)  # type: ignore[arg-type]
                        if cur and cur_w + uw > max_px:
                            lines.append(cur)
                            cur, cur_w = u, uw
                        else:
                            cur += u
                            cur_w += uw
            elif cur and cur_w + tok_w > max_px:
                lines.append(cur.rstrip())
                cur, cur_w = tok, tok_w
            else:
                cur += tok
                cur_w += tok_w
        lines.append(cur.rstrip())
    return lines


def _draw_rich(
    img: PILImage.Image | None,
    draw: PILImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    font: object,
    fonts: _Fonts,
    fill: tuple[int, int, int],
    emoji_h: int,
    cache: dict[str, object | None],
    *,
    bold: bool = False,
    dry: bool = False,
    grid: _MonoGrid | None = None,
) -> float:
    """Draw *text* (mixing glyphs + color-emoji tiles); return the end x.

    With *grid* (code blocks, #623) wide glyphs are drawn with the CJK face on a
    two-cell advance — one character at a time, so a proportional CJK face can't
    drift the column off the monospace grid. Narrow runs keep the mono face and
    are drawn in one call.
    """
    cx = x
    kwargs = {"stroke_width": 1, "stroke_fill": fill} if bold else {}
    for seg, is_emoji in _segment_runs(text):
        if is_emoji and fonts.emoji is not None:
            tile = _emoji_tile(seg, fonts.emoji, fonts.emoji_color, emoji_h, cache)
            if tile is not None:
                if not dry and img is not None:
                    img.paste(tile, (int(cx), int(y) + 2), tile)  # type: ignore[attr-defined]
                cx += tile.width + 1  # type: ignore[attr-defined]
                continue
        if grid is None:
            if not dry:
                draw.text((cx, y), seg, font=font, fill=fill, **kwargs)  # type: ignore[arg-type]
            cx += draw.textlength(seg, font=font)  # type: ignore[arg-type]
            continue
        for run, wide in _wide_runs(seg):
            if not wide:
                if not dry:
                    draw.text((cx, y), run, font=font, fill=fill, **kwargs)  # type: ignore[arg-type]
                cx += len(run) * grid.cell_w
                continue
            # No CJK face available: keep the mono face (tofu beats crashing).
            face = grid.cjk if grid.cjk is not None else font
            slot = 2 * grid.cell_w
            for ch in run:
                if not dry:
                    # Centre the glyph in its two cells: the CJK face is
                    # proportional, so its advance rarely equals the slot.
                    pad = 0.0
                    try:
                        pad = max(0.0, (slot - face.getlength(ch)) / 2)  # type: ignore[attr-defined]
                    except (AttributeError, TypeError):
                        pad = 0.0
                    draw.text(  # type: ignore[arg-type]
                        (cx + pad, y + grid.cjk_dy), ch, font=face, fill=fill, **kwargs
                    )
                cx += slot
    return cx


def _draw_avatar(
    img: PILImage.Image,
    draw: PILImageDraw.ImageDraw,
    x: int,
    y: int,
    d: int,
    name: str,
    font: object,
) -> None:
    color = _AVATAR_COLORS[sum(ord(c) for c in name) % len(_AVATAR_COLORS)]
    draw.ellipse((x, y, x + d, y + d), fill=color)
    init = (name.strip()[:1] or "?").upper()
    bbox = draw.textbbox((0, 0), init, font=font)  # type: ignore[arg-type]
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        (x + (d - tw) / 2 - bbox[0], y + (d - th) / 2 - bbox[1]),
        init,
        font=font,  # type: ignore[arg-type]
        fill=WHITE,
    )


def _line_h(font: object, gap: int = 6) -> int:
    ascent, descent = font.getmetrics()  # type: ignore[attr-defined]
    return ascent + descent + gap


def _split_code_blocks(content: str) -> list[tuple[str, str]]:
    """Split *content* into ``("text"|"code", body)`` blocks on ``` fences."""
    blocks: list[tuple[str, str]] = []
    in_code = False
    buf: list[str] = []
    for line in content.split("\n"):
        if line.lstrip().startswith("```"):
            blocks.append(("code" if in_code else "text", "\n".join(buf)))
            buf = []
            in_code = not in_code
            continue
        buf.append(line)
    blocks.append(("code" if in_code else "text", "\n".join(buf)))
    return [(k, v) for k, v in blocks if v.strip() or k == "text"]


#: Discord packs at most three ``inline`` fields onto one row.
INLINE_PER_ROW = 3

#: Horizontal gap between inline columns, in pixels.
_INLINE_GAP = 10


def _messages_contain_emoji(messages: list[ConvMessage]) -> bool:
    """True when any text in *messages* holds a character outside the BMP-text
    range that the body font can be expected to cover.

    Deliberately a cheap codepoint test rather than the ``emoji`` library: this
    runs precisely when that library is unavailable.
    """

    def _has(text: str | None) -> bool:
        if not text:
            return False
        return any(ord(ch) >= 0x2190 for ch in text)

    for m in messages:
        if _has(m.content) or _has(m.author):
            return True
        for a in m.attachments:
            if _has(a.filename):
                return True
        for r in m.reactions:
            if _has(r.emoji):
                return True
        for e in m.embeds:
            if _has(e.title) or _has(e.description):
                return True
            for f in e.fields:
                if _has(f.name) or _has(f.value):
                    return True
    return False


def emoji_support_available() -> bool:
    """Whether the optional ``emoji`` library is importable.

    Without it :func:`c_lord.discord_ui.table_renderer._segment_runs` classifies
    every cluster as *not* an emoji, so emoji get drawn with the body font — i.e.
    as tofu. Callers use this to refuse rather than emit a broken picture (#588).
    """
    try:
        import emoji  # noqa: F401
    except ImportError:
        return False
    return True


def group_fields_into_rows(
    fields: list[ConvField] | tuple[ConvField, ...],
) -> list[list[ConvField]]:
    """Group *fields* the way Discord lays them out.

    Consecutive ``inline`` fields share a row, at most :data:`INLINE_PER_ROW` of
    them. A non-inline field takes a whole row **and breaks the run**, so
    ``a b | wide | c`` renders as three rows rather than folding ``c`` back up
    next to ``a b``.
    """
    rows: list[list[ConvField]] = []
    run: list[ConvField] = []
    for f in fields:
        if not f.inline:
            if run:
                rows.append(run)
                run = []
            rows.append([f])
            continue
        run.append(f)
        if len(run) == INLINE_PER_ROW:
            rows.append(run)
            run = []
    if run:
        rows.append(run)
    return rows


def _layout_embed(
    e: ConvEmbed,
    y: int,
    draw: PILImageDraw.ImageDraw,
    img: PILImage.Image | None,
    fonts: _Fonts,
    cache: dict[str, object | None],
    *,
    dry: bool,
) -> int:
    inner_x = GUTTER + 14
    inner_w = WIDTH - MARGIN - inner_x
    body_h = _line_h(fonts.body)
    pad = 12
    emoji_h = int(BODY_SIZE * 1.15)

    # An embed is laid out as rows of columns. Title and description are
    # single-column rows; a group of inline fields is one row with up to
    # INLINE_PER_ROW columns. Every column in a row starts at the same y, and the
    # row is as tall as its tallest column — which is what stops inline fields
    # from stacking vertically (#588).
    # (font, line, color, bold, height)
    line_t = tuple[object, str, tuple[int, int, int], bool, int]
    # (dx from inner_x, lines)
    column_t = tuple[int, list[line_t]]
    rows: list[list[column_t]] = []

    def _lines(
        text: str, font: object, color: tuple[int, int, int], bold: bool, h: int, width: int
    ) -> list[line_t]:
        return [(font, ln, color, bold, h) for ln in _wrap_rich(draw, text, font, width, emoji_h)]

    if e.title:
        rows.append(
            [
                (
                    0,
                    _lines(
                        e.title,
                        fonts.name,
                        _rgb(e.color) or NAME_DEFAULT,
                        True,
                        _line_h(fonts.name),
                        inner_w,
                    ),
                )
            ]
        )
    if e.description:
        rows.append([(0, _lines(e.description, fonts.body, EMBED_TEXT, False, body_h, inner_w))])

    for group in group_fields_into_rows(e.fields):
        n = len(group)
        col_w = (inner_w - _INLINE_GAP * (n - 1)) // n
        columns: list[column_t] = []
        for i, f in enumerate(group):
            lines = _lines(f.name, fonts.name, NAME_DEFAULT, True, _line_h(fonts.name, 2), col_w)
            lines += _lines(f.value, fonts.body, EMBED_TEXT, False, body_h, col_w)
            columns.append((i * (col_w + _INLINE_GAP), lines))
        rows.append(columns)

    def _row_h(row: list[column_t]) -> int:
        return max((sum(ln[4] for ln in lines) for _, lines in row), default=0)

    inner_h = sum(_row_h(r) for r in rows)
    box_h = inner_h + 2 * pad
    if not dry:
        draw.rounded_rectangle((GUTTER, y, WIDTH - MARGIN, y + box_h), radius=6, fill=EMBED_BG)
        draw.rounded_rectangle(
            (GUTTER, y, GUTTER + 4, y + box_h), radius=2, fill=_rgb(e.color) or EMBED_BAR_DEFAULT
        )
        yy = y + pad
        for row in rows:
            for dx, lines in row:
                ly = yy
                for font, ln, color, bold, h in lines:
                    _draw_rich(
                        img,
                        draw,
                        inner_x + dx,
                        ly,
                        ln,
                        font,
                        fonts,
                        color,
                        emoji_h,
                        cache,
                        bold=bold,
                        dry=dry,
                    )
                    ly += h
            yy += _row_h(row)
    return y + box_h + 6


def _draw_chips(
    chips: list[str],
    y: int,
    draw: PILImageDraw.ImageDraw,
    img: PILImage.Image | None,
    fonts: _Fonts,
    cache: dict[str, object | None],
    *,
    dry: bool,
) -> int:
    if not chips:
        return y
    emoji_h = int(BODY_SIZE * 1.1)
    chip_h = _line_h(fonts.body, 8)
    pad_x = 8
    gap = 6
    x = GUTTER
    row_y = y
    max_x = WIDTH - MARGIN
    for label in chips:
        w = _measure_rich(draw, label, fonts.body, emoji_h) + 2 * pad_x
        if x + w > max_x and x > GUTTER:
            x = GUTTER
            row_y += chip_h + gap
        if not dry:
            draw.rounded_rectangle((x, row_y, x + w, row_y + chip_h), radius=8, fill=CHIP_BG)
            _draw_rich(
                img,
                draw,
                x + pad_x,
                row_y + 4,
                label,
                fonts.body,
                fonts,
                TEXT,
                emoji_h,
                cache,
                dry=dry,
            )
        x += w + gap
    return row_y + chip_h + 4


def _layout_message(
    m: ConvMessage,
    y: int,
    draw: PILImageDraw.ImageDraw,
    img: PILImage.Image | None,
    fonts: _Fonts,
    cache: dict[str, object | None],
    *,
    dry: bool,
) -> int:
    content_w = WIDTH - MARGIN - GUTTER
    body_h = _line_h(fonts.body)
    mono_h = _line_h(fonts.mono, 4)
    # #623: one monospace cell, so code blocks can place wide glyphs on a
    # two-cell advance and keep Japanese tables aligned.
    mono_grid = _MonoGrid(cjk=fonts.mono_cjk, cell_w=fonts.mono_cell_w, cjk_dy=fonts.mono_cjk_dy)
    emoji_h = int(BODY_SIZE * 1.15)
    top = y

    # Header row: name + (BOT badge) + timestamp.
    name_color = _rgb(m.color) or NAME_DEFAULT
    if not dry and img is not None:
        _draw_avatar(img, draw, MARGIN, y, AVATAR, m.author, fonts.name)
    nx = GUTTER
    if not dry:
        nx = _draw_rich(
            img,
            draw,
            GUTTER,
            y,
            m.author,
            fonts.name,
            fonts,
            name_color,
            emoji_h,
            cache,
            bold=True,
            dry=dry,
        )
        if m.is_bot:
            bw = draw.textlength("BOT", font=fonts.badge) + 10
            draw.rounded_rectangle(
                (nx + 8, y + 3, nx + 8 + bw, y + 3 + BADGE_SIZE + 6), radius=4, fill=BADGE_BG
            )
            draw.text((nx + 13, y + 4), "BOT", font=fonts.badge, fill=WHITE)  # type: ignore[arg-type]
            nx += 8 + bw
        if m.timestamp:
            draw.text((nx + 10, y + 4), m.timestamp, font=fonts.time, fill=MUTED)  # type: ignore[arg-type]
    y += _line_h(fonts.name, 4)

    # Body content (with fenced code blocks).
    for kind, text in _split_code_blocks(m.content):
        if kind == "code" and text.strip():
            wrapped: list[str] = []
            for ln in text.split("\n"):
                wrapped.extend(
                    _wrap_rich(draw, ln or " ", fonts.mono, content_w - 24, emoji_h, grid=mono_grid)
                )
            box_h = len(wrapped) * mono_h + 16
            if not dry:
                draw.rounded_rectangle(
                    (GUTTER, y, WIDTH - MARGIN, y + box_h), radius=5, fill=CODE_BG
                )
                ly = y + 8
                for ln in wrapped:
                    _draw_rich(
                        img,
                        draw,
                        GUTTER + 12,
                        ly,
                        ln,
                        fonts.mono,
                        fonts,
                        TEXT,
                        emoji_h,
                        cache,
                        dry=dry,
                        grid=mono_grid,
                    )
                    ly += mono_h
            y += box_h + 6
        elif kind == "text" and text.strip():
            for ln in _wrap_rich(draw, text, fonts.body, content_w, emoji_h):
                if not dry and ln:
                    _draw_rich(
                        img, draw, GUTTER, y, ln, fonts.body, fonts, TEXT, emoji_h, cache, dry=dry
                    )
                y += body_h

    # Embeds (tool-use / done cards).
    for e in m.embeds:
        y = _layout_embed(e, y, draw, img, fonts, cache, dry=dry)

    # Attachments + reactions as pills.
    if m.attachments:
        y = _draw_chips(
            [f"📎 {a.filename}" for a in m.attachments], y + 2, draw, img, fonts, cache, dry=dry
        )
    if m.reactions:
        y = _draw_chips(
            [f"{r.emoji} {r.count}" for r in m.reactions], y + 2, draw, img, fonts, cache, dry=dry
        )

    return max(y, top + AVATAR + 4)


def render_conversation_png(
    messages: list[ConvMessage], *, title: str | None = None
) -> bytes | None:
    """Render *messages* to a Discord-dark PNG via Pillow.

    Returns ``None`` when Pillow or a usable font is missing (the optional
    ``c-lord[table]`` extra), mirroring :func:`render_pane_png`.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    # #588: without the ``emoji`` library every emoji is drawn with the body font
    # — as tofu — and the old code reported success anyway. A mockup is evidence
    # someone adjudicates a design from, so a silently broken picture is worse
    # than none. Only refuse when the spec actually contains emoji: an ASCII-only
    # spec renders correctly without the extra.
    if not emoji_support_available() and _messages_contain_emoji(messages):
        logger.warning(
            "conversation mockup: spec contains emoji but the 'emoji' library is "
            "missing — refusing to render tofu. Install it with: uv sync --extra table"
        )
        return None

    text_font = load_text_font(BODY_SIZE)
    mono = load_mono_font(MONO_SIZE)
    if text_font is None or mono is None:
        return None
    emoji_font, emoji_color = load_emoji_font(109, BODY_SIZE)
    # #623: size the CJK fallback to the two-cell slot a wide glyph occupies, and
    # note the baseline shift, so Japanese sits on the monospace grid instead of
    # floating small and high inside it.
    cell_w = float(mono.getlength("M"))
    mono_cjk = load_text_font(max(1, round(2 * cell_w))) or load_text_font(MONO_SIZE)
    cjk_dy = 0.0
    if mono_cjk is not None:
        cjk_dy = float(mono.getmetrics()[0] - mono_cjk.getmetrics()[0])
    fonts = _Fonts(
        body=text_font,
        name=load_text_font(NAME_SIZE) or text_font,
        time=load_text_font(TIME_SIZE) or text_font,
        mono=mono,
        badge=load_text_font(BADGE_SIZE) or text_font,
        emoji=emoji_font,
        emoji_color=emoji_color,
        # #623: wide glyphs in code blocks fall back to this face; None is fine
        # (we then keep the mono face rather than failing to render).
        mono_cjk=mono_cjk,
        mono_cell_w=cell_w,
        mono_cjk_dy=cjk_dy,
    )

    cache: dict[str, object | None] = {}
    scratch = ImageDraw.Draw(Image.new("RGB", (4, 4)))

    header_h = _line_h(fonts.name, 10) if title else 0
    y = MARGIN + header_h
    for m in messages:
        y = _layout_message(m, y, scratch, None, fonts, cache, dry=True)
        y += MSG_GAP
    total_h = max(y + MARGIN - MSG_GAP, MARGIN * 2 + AVATAR)

    img: PILImage.Image = Image.new("RGB", (WIDTH, total_h), BG)
    draw = ImageDraw.Draw(img)
    if title:
        draw.text((MARGIN, MARGIN), title, font=fonts.name, fill=MUTED)  # type: ignore[arg-type]
        draw.line(
            (MARGIN, MARGIN + header_h - 6, WIDTH - MARGIN, MARGIN + header_h - 6),
            fill=EMBED_BG,
            width=1,
        )

    y = MARGIN + header_h
    cache.clear()
    for m in messages:
        y = _layout_message(m, y, draw, img, fonts, cache, dry=False)
        y += MSG_GAP

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
