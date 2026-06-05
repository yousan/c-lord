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
        title=e.get("title"),
        description=e.get("description"),
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
                timestamp=m.get("timestamp"),
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


def _measure_rich(draw: PILImageDraw.ImageDraw, text: str, font: object, emoji_w: float) -> float:
    """Pixel width of *text*, treating each emoji cluster as *emoji_w* wide."""
    total = 0.0
    for seg, is_emoji in _segment_runs(text):
        total += emoji_w if is_emoji else draw.textlength(seg, font=font)  # type: ignore[arg-type]
    return total


def _wrap_rich(
    draw: PILImageDraw.ImageDraw, text: str, font: object, max_px: float, emoji_w: float
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
            tok_w = _measure_rich(draw, tok, font, emoji_w)
            if tok_w > max_px:  # token alone exceeds a line — flush then char-break
                if cur:
                    lines.append(cur.rstrip())
                    cur, cur_w = "", 0.0
                for seg, is_emoji in _segment_runs(tok):
                    units: Iterable[str] = [seg] if is_emoji else list(seg)
                    for u in units:
                        uw = emoji_w if is_emoji else draw.textlength(u, font=font)  # type: ignore[arg-type]
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
) -> float:
    """Draw *text* (mixing glyphs + color-emoji tiles); return the end x."""
    cx = x
    for seg, is_emoji in _segment_runs(text):
        if is_emoji and fonts.emoji is not None:
            tile = _emoji_tile(seg, fonts.emoji, fonts.emoji_color, emoji_h, cache)
            if tile is not None:
                if not dry and img is not None:
                    img.paste(tile, (int(cx), int(y) + 2), tile)  # type: ignore[attr-defined]
                cx += tile.width + 1  # type: ignore[attr-defined]
                continue
        if not dry:
            kwargs = {"stroke_width": 1, "stroke_fill": fill} if bold else {}
            draw.text((cx, y), seg, font=font, fill=fill, **kwargs)  # type: ignore[arg-type]
        cx += draw.textlength(seg, font=font)  # type: ignore[arg-type]
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

    items: list[tuple[object, str, tuple[int, int, int], bool, int]] = []
    if e.title:
        for ln in _wrap_rich(draw, e.title, fonts.name, inner_w, emoji_h):
            items.append((fonts.name, ln, _rgb(e.color) or NAME_DEFAULT, True, _line_h(fonts.name)))
    if e.description:
        for ln in _wrap_rich(draw, e.description, fonts.body, inner_w, emoji_h):
            items.append((fonts.body, ln, EMBED_TEXT, False, body_h))
    for f in e.fields:
        for ln in _wrap_rich(draw, f.name, fonts.name, inner_w, emoji_h):
            items.append((fonts.name, ln, NAME_DEFAULT, True, _line_h(fonts.name, 2)))
        for ln in _wrap_rich(draw, f.value, fonts.body, inner_w, emoji_h):
            items.append((fonts.body, ln, EMBED_TEXT, False, body_h))

    inner_h = sum(it[4] for it in items)
    box_h = inner_h + 2 * pad
    if not dry:
        draw.rounded_rectangle((GUTTER, y, WIDTH - MARGIN, y + box_h), radius=6, fill=EMBED_BG)
        draw.rounded_rectangle(
            (GUTTER, y, GUTTER + 4, y + box_h), radius=2, fill=_rgb(e.color) or EMBED_BAR_DEFAULT
        )
        yy = y + pad
        for font, ln, color, bold, h in items:
            _draw_rich(
                img, draw, inner_x, yy, ln, font, fonts, color, emoji_h, cache, bold=bold, dry=dry
            )
            yy += h
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
                wrapped.extend(_wrap_rich(draw, ln or " ", fonts.mono, content_w - 24, emoji_h))
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

    text_font = load_text_font(BODY_SIZE)
    mono = load_mono_font(MONO_SIZE)
    if text_font is None or mono is None:
        return None
    emoji_font, emoji_color = load_emoji_font(109, BODY_SIZE)
    fonts = _Fonts(
        body=text_font,
        name=load_text_font(NAME_SIZE) or text_font,
        time=load_text_font(TIME_SIZE) or text_font,
        mono=mono,
        badge=load_text_font(BADGE_SIZE) or text_font,
        emoji=emoji_font,
        emoji_color=emoji_color,
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
