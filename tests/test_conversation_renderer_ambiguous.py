"""#1087: ambiguous-width characters in a code block must not render as tofu.

``_wide_runs`` sends only East Asian Width W/F characters to the CJK face; A
(ambiguous) ones such as ``※ ① ○ △`` stayed with the monospace face, which on
DejaVu Sans Mono has no glyph for them — a box, in a picture whose whole point
is to be read at a glance (#316, the rest of #588). A character the monospace
face cannot draw now goes to the CJK face, on the same two-cell slot as W/F.
"""

from __future__ import annotations

import pytest

from c_lord.discord_ui import conversation_renderer as cr
from c_lord.discord_ui.fonts import load_mono_font, load_text_font

pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

CHARS = "①※○△"


def _notdef(face: object) -> bytes:
    """The bitmap of a code point no font draws — the .notdef box (or nothing)."""
    return bytes(face.getmask("\U0010fffd"))  # type: ignore[attr-defined]


def _drawn(face: object, ch: str) -> bool:
    mask = face.getmask(ch)  # type: ignore[attr-defined]
    return mask.getbbox() is not None and bytes(mask) != _notdef(face)


def _grid(mono: object, cjk: object) -> cr._MonoGrid:
    """The code-block grid the way the renderer builds it."""
    cell_w = float(mono.getlength("M"))  # type: ignore[attr-defined]
    build = getattr(cr, "_code_block_grid", None)
    if build is not None:
        return build(mono, cjk, cell_w, 0.0)
    return cr._MonoGrid(cjk=cjk, cell_w=cell_w)


@pytest.fixture
def faces() -> tuple[object, object]:
    mono = load_mono_font(cr.MONO_SIZE)
    cjk = load_text_font(cr.MONO_SIZE * 2)
    if mono is None or cjk is None:
        pytest.skip("needs a monospace face and a CJK face")
    if all(_drawn(mono, ch) for ch in CHARS):
        pytest.skip("this host's monospace face already has these glyphs")
    if not all(_drawn(cjk, ch) for ch in CHARS):
        pytest.skip("this host's CJK face lacks these glyphs too")
    return mono, cjk


def test_ambiguous_chars_in_a_code_block_are_drawn_with_a_face_that_has_them(
    faces: tuple[object, object],
) -> None:
    """AC1: each of ① ※ ○ △ is drawn with a face whose glyph is not .notdef."""
    mono, cjk = faces
    img = Image.new("RGB", (800, 60), (49, 51, 56))
    draw = ImageDraw.Draw(img)
    used: dict[str, object] = {}
    real_text = draw.text

    def spy(xy, s, font=None, **kw):  # type: ignore[no-untyped-def]
        for ch in s:
            used[ch] = font
        return real_text(xy, s, font=font, **kw)

    draw.text = spy  # type: ignore[method-assign]
    fonts = cr._Fonts(
        body=cjk,
        name=cjk,
        time=cjk,
        mono=mono,
        badge=cjk,
        emoji=None,
        emoji_color=False,
        mono_cjk=cjk,
        mono_cell_w=float(mono.getlength("M")),  # type: ignore[attr-defined]
    )
    cr._draw_rich(
        img,
        draw,
        4,
        4,
        " ".join(CHARS) + " ab",
        mono,
        fonts,
        (220, 220, 220),
        16,
        {},
        grid=_grid(mono, cjk),
    )

    for ch in CHARS:
        assert _drawn(used[ch], ch), f"{ch!r} drawn as tofu with {used[ch]!r}"
    assert used["a"] is mono, "ASCII stays on the monospace face"


def test_measure_and_draw_agree_for_ambiguous_chars(faces: tuple[object, object]) -> None:
    """Wrapping measures what drawing advances, fallback glyphs included."""
    mono, cjk = faces
    grid = _grid(mono, cjk)
    draw = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    fonts = cr._Fonts(
        body=cjk,
        name=cjk,
        time=cjk,
        mono=mono,
        badge=cjk,
        emoji=None,
        emoji_color=False,
        mono_cjk=cjk,
        mono_cell_w=grid.cell_w,
    )
    text = "a①※b"
    end = cr._draw_rich(
        None, draw, 0.0, 0.0, text, mono, fonts, (0, 0, 0), 16, {}, dry=True, grid=grid
    )
    assert end == pytest.approx(cr._measure_rich(draw, text, mono, 16, grid=grid))
