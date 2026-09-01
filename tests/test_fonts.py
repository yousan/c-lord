"""Tests for CJK-capable font resolution (#664).

``load_text_font`` promises a *CJK-capable* font. Before #664 it returned the
first candidate that merely **existed** on disk, and the candidate list ended
with ``DejaVuSans.ttf`` — a font with no CJK glyphs. On any host without Noto
(i.e. most Linux boxes, since DejaVu ships by default) that made
``/tmux-screenshot`` render Japanese as tofu with no warning at all.
"""

from __future__ import annotations

import logging

import pytest

from c_lord.discord_ui import fonts

pytest.importorskip("PIL")

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _dejavu_available() -> bool:
    return fonts.first_existing((DEJAVU,)) is not None


needs_dejavu = pytest.mark.skipif(
    not _dejavu_available(), reason="DejaVuSans.ttf not installed on this host"
)


@needs_dejavu
def test_load_text_font_rejects_font_without_cjk_glyphs(monkeypatch):
    """A non-CJK font must never be returned as the CJK-capable text font."""
    monkeypatch.setattr(fonts, "JP_FONT_PATHS", (DEJAVU,))
    assert fonts.load_text_font(14) is None


@needs_dejavu
def test_missing_cjk_font_is_logged_once(monkeypatch, caplog):
    """Falling back to no CJK font must be visible in the log, not silent."""
    monkeypatch.setattr(fonts, "JP_FONT_PATHS", (DEJAVU,))
    fonts.reset_cjk_warning()
    with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.fonts"):
        fonts.load_text_font(14)
        fonts.load_text_font(14)
        fonts.load_text_font(14)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"
    assert "README" in warnings[0].getMessage()


def test_real_cjk_font_is_accepted_when_present():
    """AC4: on a host that *does* have a CJK font, behaviour is unchanged."""
    path = fonts.first_existing(fonts.JP_FONT_PATHS)
    if path is None:
        pytest.skip("no CJK-capable font installed on this host")
    font = fonts.load_text_font(14)
    assert font is not None
    assert fonts.has_cjk_glyphs(font)


@needs_dejavu
def test_has_cjk_glyphs_detects_missing_coverage():
    """The probe itself: DejaVu has no CJK, a real CJK font does."""
    from PIL import ImageFont

    assert not fonts.has_cjk_glyphs(ImageFont.truetype(DEJAVU, 14))
    path = fonts.first_existing(fonts.JP_FONT_PATHS)
    if path is not None:
        assert fonts.has_cjk_glyphs(ImageFont.truetype(path, 14))
