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

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import ImageFont

logger = logging.getLogger(__name__)

# CJK-capable proportional text fonts (used for full-width / wide glyphs).
#
# #664: every entry here is *verified* at load time to actually carry CJK
# glyphs — existing on disk is not enough. The list used to end with
# ``DejaVuSans.ttf``, which ships on virtually every Linux box and has no CJK
# coverage at all, so any host without Noto silently rendered Japanese as tofu.
# Keep the list wide (a missing font is skipped, never trusted) but never treat
# "the file is there" as "the glyphs are there".
JP_FONT_PATHS = (
    os.path.expanduser("~/.local/share/fonts/NotoSansJP.ttf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-JP-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/vlgothic/VL-Gothic-Regular.ttf",
    "/usr/share/fonts/truetype/ipafont/ipagp.ttf",
    "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
)

# Characters used to probe a candidate for CJK coverage. Two probes (kanji and
# kana) so a font that happens to give one of them a .notdef-shaped box is not
# enough to pass.
_CJK_PROBES = ("\u65e5", "\u3042")
# An unassigned Plane-15 codepoint: no font has a glyph for it, so whatever it
# renders *is* that font's .notdef. A probe whose metrics match .notdef exactly
# is a probe the font cannot draw.
_NOTDEF_PROBE = "\U000fffff"

# Warn once per process, not once per rendered frame (#664 AC3).
_warned_no_cjk = False

_FONT_HELP = (
    "No CJK-capable font found — Japanese text in /tmux-screenshot and evidence "
    "captures will render as tofu (boxes). Install one (e.g. Noto Sans JP) and "
    "restart; see README のフォント導入手順 (#498)."
)


def reset_cjk_warning() -> None:
    """Re-arm the once-per-process CJK warning (used by tests)."""
    global _warned_no_cjk
    _warned_no_cjk = False


def has_cjk_glyphs(font: ImageFont.FreeTypeFont) -> bool:
    """True when *font* can actually draw CJK, not just when the file exists.

    Pillow has no glyph-coverage API, so compare each probe's metrics against
    the font's own .notdef. A font missing the glyph draws .notdef for it, so
    the boxes are identical. This needs no dependency beyond Pillow (#664 AC2).
    """
    try:
        notdef = font.getbbox(_NOTDEF_PROBE)
        return any(font.getbbox(ch) != notdef for ch in _CJK_PROBES)
    except Exception:  # pragma: no cover - defensive: never block rendering
        return True


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
    """Load a CJK-capable proportional font at *size*, or ``None``.

    Candidates are tried in order and each is **verified** to carry CJK glyphs
    (#664). Returning a font that cannot draw Japanese would be worse than
    returning ``None``: the caller's own fallback never fires and the tofu ships
    silently. When nothing qualifies, warn once so the host owner can act.
    """
    global _warned_no_cjk
    for path in JP_FONT_PATHS:
        if not os.path.exists(path):
            continue
        font = _load((path,), size)
        if font is not None and has_cjk_glyphs(font):
            return font
    if not _warned_no_cjk:
        _warned_no_cjk = True
        logger.warning("%s", _FONT_HELP)
    return None


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
