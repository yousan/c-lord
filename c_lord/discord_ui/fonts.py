"""Shared font resolution for the ``discord_ui`` image renderers.

Both the GFM table renderer (:mod:`c_lord.discord_ui.table_renderer`) and the
tmux pane screenshot renderer (:mod:`c_lord.discord_ui.pane_renderer`, #285)
need to locate a CJK-capable text font, a monospace font, and a color-emoji
font from a list of well-known system paths. Centralizing the lookup keeps the
two renderers in sync.

Pillow is an optional dependency (``pip install c-lord[table]``). Every loader
lazily imports it and returns ``None`` when Pillow — or a usable font — is
missing, so callers degrade gracefully instead of raising.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import ImageFont

# CJK-capable proportional text fonts (used for full-width / wide glyphs).
JP_FONT_PATHS = (
    os.path.expanduser("~/.local/share/fonts/NotoSansJP.ttf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# Monospace fonts for terminal-style grid rendering (pane screenshots).
MONO_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
)

# Color emoji fonts (CBDT/COLR) Pillow can render with embedded_color=True.
COLOR_EMOJI_PATHS = (
    os.path.expanduser("~/.local/share/fonts/NotoColorEmoji.ttf"),
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
)
# Monochrome fallback if no color font is present (no color, but no tofu).
MONO_EMOJI_PATHS = (
    os.path.expanduser("~/.local/share/fonts/NotoEmoji-Regular.ttf"),
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
)


def first_existing(paths: tuple[str, ...]) -> str | None:
    """Return the first path in *paths* that exists on disk, or ``None``."""
    return next((p for p in paths if os.path.exists(p)), None)


def _load(paths: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont | None:
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    path = first_existing(paths)
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return None


def load_text_font(size: int) -> ImageFont.FreeTypeFont | None:
    """Load a CJK-capable proportional font at *size*, or ``None``."""
    return _load(JP_FONT_PATHS, size)


def load_mono_font(size: int) -> ImageFont.FreeTypeFont | None:
    """Load a monospace font at *size*, or ``None``."""
    return _load(MONO_FONT_PATHS, size)


def load_emoji_font(color_size: int, mono_size: int) -> tuple[ImageFont.FreeTypeFont | None, bool]:
    """Load an emoji font, preferring a color strike.

    Returns ``(font, is_color)``. Color fonts are bitmap strikes loaded at
    *color_size*; the monochrome fallback is loaded at *mono_size*. ``font`` is
    ``None`` when neither is available (or Pillow is missing).
    """
    if first_existing(COLOR_EMOJI_PATHS) is not None:
        return _load(COLOR_EMOJI_PATHS, color_size), True
    return _load(MONO_EMOJI_PATHS, mono_size), False
