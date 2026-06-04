"""Render a Discord thread/conversation to a PNG image (#243, route 2).

The c-lord DoD wants *Discord-side* evidence — the result a user actually sees:
author rows, message text, the ``Bash(...)`` / ``✅ Done`` tool-use embeds, the
🧠/🛠️/✅ status-lamp reactions, attachments. Automating the real Discord client
to screenshot it is a self-bot (forbidden by Discord's ToS) and impossible with
a bot token anyway, so c-lord instead **self-renders** the conversation from
data it already has in-process via ``channel.history`` — the same approach the
tmux ``/tmux-screenshot`` (#285) takes for the pane side.

Two interchangeable engines produce the PNG:

* :func:`render_conversation_png` — pure-Python Pillow draw (no new dependency
  beyond the existing ``c-lord[table]`` extra). Default, zero-config.
* :func:`render_conversation_html_png` — an HTML/CSS template rendered by a
  headless system browser (Chrome/Chromium) for higher fidelity. Optional;
  degrades to ``None`` when no browser is found.

Both return ``bytes | None`` and never raise on a missing optional dependency,
mirroring :mod:`c_lord.discord_ui.pane_renderer`.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import shutil
import tempfile
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
    """A Discord embed — these carry the ``Bash(...)`` / ``✅ Done`` evidence."""

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


def _color_value(obj: object) -> int | None:
    """Extract an int color from a ``discord.Colour``-like object (0 → None)."""
    if obj is None:
        return None
    value = getattr(obj, "value", None)
    if isinstance(obj, int):
        value = obj
    if not value:  # 0 / None → "no color"
        return None
    return int(value)


def message_to_conv(msg: object) -> ConvMessage:
    """Normalize a ``discord.Message`` (or a duck-typed stand-in) to a ConvMessage.

    Reads only public attributes, so it works against both real messages and
    test doubles. Missing optional fields default safely.
    """
    author_obj = getattr(msg, "author", None)
    author = str(getattr(author_obj, "display_name", None) or author_obj or "unknown")
    is_bot = bool(getattr(author_obj, "bot", False))
    color = _color_value(getattr(author_obj, "color", None))

    created = getattr(msg, "created_at", None)
    timestamp = created.strftime("%Y-%m-%d %H:%M") if created is not None else None

    embeds: list[ConvEmbed] = []
    for e in getattr(msg, "embeds", None) or []:
        fields = tuple(
            ConvField(
                name=str(getattr(f, "name", "") or ""),
                value=str(getattr(f, "value", "") or ""),
                inline=bool(getattr(f, "inline", False)),
            )
            for f in (getattr(e, "fields", None) or [])
        )
        embeds.append(
            ConvEmbed(
                title=getattr(e, "title", None),
                description=getattr(e, "description", None),
                color=_color_value(getattr(e, "color", None)),
                fields=fields,
            )
        )

    reactions = tuple(
        ConvReaction(emoji=str(getattr(r, "emoji", "")), count=int(getattr(r, "count", 1) or 1))
        for r in (getattr(msg, "reactions", None) or [])
    )
    attachments = tuple(
        ConvAttachment(
            filename=str(getattr(a, "filename", "file")),
            content_type=getattr(a, "content_type", None),
        )
        for a in (getattr(msg, "attachments", None) or [])
    )

    return ConvMessage(
        author=author,
        content=str(getattr(msg, "content", "") or ""),
        is_bot=is_bot,
        timestamp=timestamp,
        color=color,
        embeds=tuple(embeds),
        reactions=reactions,
        attachments=attachments,
    )


async def fetch_conversation(channel: object, *, limit: int = 20) -> list[ConvMessage]:
    """Fetch the last *limit* messages of *channel* as ConvMessages (oldest first).

    Uses discord.py's ``channel.history`` async iterator — a REST fetch that
    returns full message payloads (embeds + reactions included) regardless of
    gateway intents. Purely in-process; no browser, no extra auth.
    """
    out: list[ConvMessage] = []
    async for msg in channel.history(limit=limit, oldest_first=True):  # type: ignore[attr-defined]
        out.append(message_to_conv(msg))
    return out


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


# ── Pillow renderer (variant a) ──────────────────────────────────────────────


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


# ── HTML / headless-Chrome renderer (variant b) ──────────────────────────────

_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")


def browser_available() -> bool:
    """True if a headless-capable system browser binary is on PATH."""
    return _find_browser() is not None


def _find_browser() -> str | None:
    override = os.getenv("CLORD_HEADLESS_BROWSER")
    if override and os.path.exists(override):
        return override
    return next((p for name in _BROWSERS if (p := shutil.which(name))), None)


def _embed_html(e: ConvEmbed) -> str:
    bar = "#%06x" % (e.color & 0xFFFFFF) if e.color else "#4f545c"
    parts = [f'<div class="embed" style="border-left:4px solid {bar}">']
    if e.title:
        parts.append(f'<div class="etitle">{html.escape(e.title)}</div>')
    if e.description:
        parts.append(f'<div class="edesc">{html.escape(e.description)}</div>')
    for f in e.fields:
        parts.append(
            f'<div class="efield"><div class="ename">{html.escape(f.name)}</div>'
            f'<div class="evalue">{html.escape(f.value)}</div></div>'
        )
    parts.append("</div>")
    return "".join(parts)


def _content_html(content: str) -> str:
    out: list[str] = []
    for kind, text in _split_code_blocks(content):
        if not text.strip():
            continue
        if kind == "code":
            out.append(f"<pre><code>{html.escape(text)}</code></pre>")
        else:
            out.append(f'<div class="body">{html.escape(text).replace(chr(10), "<br>")}</div>')
    return "".join(out)


def _message_html(m: ConvMessage) -> str:
    initial = html.escape((m.author.strip()[:1] or "?").upper())
    hue = sum(ord(c) for c in m.author) * 47 % 360
    name_color = "#%06x" % (m.color & 0xFFFFFF) if m.color else "#f2f3f5"
    badge = '<span class="badge">BOT</span>' if m.is_bot else ""
    ts = f'<span class="ts">{html.escape(m.timestamp)}</span>' if m.timestamp else ""
    chips = "".join(
        f'<span class="chip">📎 {html.escape(a.filename)}</span>' for a in m.attachments
    ) + "".join(f'<span class="chip">{html.escape(r.emoji)} {r.count}</span>' for r in m.reactions)
    chips_html = f'<div class="chips">{chips}</div>' if chips else ""
    embeds = "".join(_embed_html(e) for e in m.embeds)
    name_html = f'<span class="name" style="color:{name_color}">{html.escape(m.author)}</span>'
    head = f'<div class="head">{name_html}{badge}{ts}</div>'
    return (
        f'<div class="msg">'
        f'<div class="avatar" style="background:hsl({hue},45%,45%)">{initial}</div>'
        f'<div class="col">{head}'
        f"{_content_html(m.content)}{embeds}{chips_html}"
        f"</div></div>"
    )


def _build_html(messages: list[ConvMessage], title: str | None) -> str:
    body = "".join(_message_html(m) for m in messages)
    header = f'<div class="title">{html.escape(title)}</div>' if title else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ box-sizing: border-box; }}
body {{ margin:0; width:860px; background:#313338;
  font-family:'Noto Sans CJK JP','Noto Sans JP','Noto Color Emoji',sans-serif;
  color:#dbdee1; font-size:16px; line-height:1.35; }}
.title {{ padding:12px 16px; color:#949ba4; font-size:14px; border-bottom:1px solid #2b2d31; }}
.msg {{ display:flex; padding:6px 16px; gap:14px; }}
.avatar {{ width:40px; height:40px; border-radius:50%; flex:0 0 40px;
  display:flex; align-items:center; justify-content:center; color:#fff; font-weight:600; }}
.col {{ min-width:0; flex:1; }}
.head {{ display:flex; align-items:center; gap:8px; }}
.name {{ font-weight:600; }}
.badge {{ background:#5865f2; color:#fff; font-size:11px; font-weight:700;
  border-radius:4px; padding:1px 5px; }}
.ts {{ color:#949ba4; font-size:12px; }}
.body {{ margin-top:2px; white-space:pre-wrap; word-break:break-word; }}
pre {{ background:#1e1f22; border-radius:5px; padding:8px 12px; margin:4px 0;
  overflow:hidden; }}
code {{ font-family:'DejaVu Sans Mono',monospace; font-size:14px; white-space:pre-wrap;
  word-break:break-word; }}
.embed {{ background:#2b2d31; border-radius:6px; padding:8px 12px; margin:4px 0; max-width:520px; }}
.etitle {{ font-weight:600; color:#f2f3f5; }}
.edesc {{ color:#c5c9cf; margin-top:2px; }}
.efield {{ margin-top:4px; }}
.ename {{ font-weight:600; font-size:14px; }}
.evalue {{ color:#c5c9cf; font-size:14px; }}
.chips {{ margin-top:4px; }}
.chip {{ display:inline-block; background:#3c3f45; border-radius:8px;
  padding:2px 8px; margin:2px 4px 2px 0; font-size:14px; }}
</style></head><body>{header}{body}</body></html>"""


def _autocrop_bottom(png: bytes) -> bytes:
    """Trim uniform bottom padding left by a fixed-height headless screenshot."""
    try:
        from PIL import Image
    except ImportError:
        return png
    try:
        im = Image.open(BytesIO(png)).convert("RGB")
    except Exception:
        return png
    w, h = im.size
    bg = im.getpixel((w // 2, h - 1))
    px = im.load()
    bottom = h
    for yy in range(h - 1, -1, -1):
        if any(px[xx, yy] != bg for xx in range(0, w, max(1, w // 60))):
            bottom = min(h, yy + 16)
            break
    if bottom < h:
        im = im.crop((0, 0, w, bottom))
    out = BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def render_conversation_html_png(
    messages: list[ConvMessage], *, title: str | None = None
) -> bytes | None:
    """Render *messages* to PNG via an HTML template + headless system browser.

    Returns ``None`` when no browser binary is found or the browser fails, so
    callers can fall back to :func:`render_conversation_png`.
    """
    browser = _find_browser()
    if browser is None:
        return None

    # Headless screenshot clips to the window height, so bias the estimate HIGH
    # (code blocks, wrapped lines, and field-heavy embeds all add rows) and let
    # _autocrop_bottom trim the surplus. Underestimating would clip content.
    def _msg_h(m: ConvMessage) -> int:
        lines = m.content.count("\n") + 1 + len(m.content) // 60
        embed_rows = sum(2 + len(e.fields) + (len(e.description or "") // 60) for e in m.embeds)
        return 120 + 30 * lines + 90 * len(m.embeds) + 30 * embed_rows

    est_h = 160 + sum(_msg_h(m) for m in messages)
    est_h = max(400, min(est_h, 24000))
    html_doc = _build_html(messages, title)

    tmpdir = tempfile.mkdtemp(prefix="clord-shot-")
    html_path = os.path.join(tmpdir, "c.html")
    out_path = os.path.join(tmpdir, "c.png")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html_doc)

    args = [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--force-device-scale-factor=2",
        "--default-background-color=313338ff",
        f"--window-size={WIDTH},{est_h}",
        f"--screenshot={out_path}",
        f"file://{html_path}",
    ]
    try:
        png = asyncio.run(_run_browser(args, out_path))
    except RuntimeError:
        # Already inside a running loop — run in a fresh thread's loop.
        png = _run_browser_sync(args, out_path)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully like other renderers
        logger.warning("html screenshot failed: %s", exc)
        png = None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if png is None:
        return None
    return _autocrop_bottom(png)


async def _run_browser(args: list[str], out_path: str) -> bytes | None:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()  # reap so we don't leave a zombie
        logger.warning("html screenshot timed out")
        return None
    if not os.path.exists(out_path):
        logger.warning(
            "html screenshot produced no file: %s", stderr.decode("utf-8", "replace")[:300]
        )
        return None
    with open(out_path, "rb") as fh:
        return fh.read()


def _run_browser_sync(args: list[str], out_path: str) -> bytes | None:
    import subprocess

    try:
        subprocess.run(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("html screenshot subprocess failed: %s", exc)
        return None
    if not os.path.exists(out_path):
        return None
    with open(out_path, "rb") as fh:
        return fh.read()


# ── Dispatcher ───────────────────────────────────────────────────────────────


def render_conversation(
    messages: list[ConvMessage], *, engine: str = "pillow", title: str | None = None
) -> bytes | None:
    """Render *messages* with the requested *engine*.

    * ``"pillow"`` (default) — pure-Python self-render, zero-config.
    * ``"html"`` — HTML template + headless browser (higher fidelity).
    * ``"auto"`` — prefer HTML when a browser exists, fall back to Pillow.
    """
    if engine == "html":
        return render_conversation_html_png(messages, title=title)
    if engine == "auto":
        if browser_available():
            png = render_conversation_html_png(messages, title=title)
            if png is not None:
                return png
        return render_conversation_png(messages, title=title)
    return render_conversation_png(messages, title=title)
