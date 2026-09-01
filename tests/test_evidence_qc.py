"""Tests for the captured-evidence quality check (Issue #559).

``scripts/evidence_qc.py`` inspects a PNG that ``discord_evidence_shot.sh``
just captured and refuses to call it evidence when the frame is unusable:
covered by a promo modal, still rendering (skeleton sidebar), or not the
requested channel at all.

Like ``test_compose_screenshots.py`` this loads the standalone script by file
path — it is deliberately not part of the importable ``c_lord`` package.

The synthetic frames below reproduce the pixel statistics measured on real
captures taken with ``discord_evidence_shot.sh`` on 2026-09-01 (the numbers
are recorded in the module docstring of ``scripts/evidence_qc.py``).
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "evidence_qc.py"

# Discord dark-theme surfaces, as sampled from a real capture.
_RAIL = (30, 31, 34)
_SIDEBAR_BG = (43, 45, 49)
_CHAT_BG = (49, 51, 56)
_COMPOSER_BG = (56, 58, 64)
_TEXT = (219, 222, 225)


def _load_qc():
    spec = importlib.util.spec_from_file_location("evidence_qc", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves annotations via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _text_rows(draw: ImageDraw.ImageDraw, x0: int, x1: int, y0: int, y1: int, seed: int) -> None:
    """Paint high-frequency glyph-like speckle — what rendered text looks like."""
    rnd = random.Random(seed)
    for y in range(y0, y1, 6):
        x = x0
        while x < x1 - 4:
            run = rnd.randint(2, 5)
            if rnd.random() < 0.55:
                draw.rectangle((x, y, x + run, y + 3), fill=_TEXT)
            x += run + rnd.randint(2, 4)


def _good_frame(size: tuple[int, int] = (1600, 1500)) -> Image.Image:
    """A loaded Discord channel view: sidebar text, message text, composer box."""
    w, h = size
    img = Image.new("RGB", size, _CHAT_BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 71, h), fill=_RAIL)
    d.rectangle((72, 0, 369, h), fill=_SIDEBAR_BG)
    _text_rows(d, 96, 340, 120, h - 120, seed=1)
    _text_rows(d, 420, w - 320, 120, h - 120, seed=2)
    d.rounded_rectangle((400, h - 84, w - 40, h - 28), radius=8, fill=_COMPOSER_BG)
    _text_rows(d, 430, w - 300, h - 70, h - 44, seed=3)
    return img


def _modal_frame(size: tuple[int, int] = (1600, 1500)) -> Image.Image:
    """The good frame under Discord's modal backdrop, with a centred card."""
    w, h = size
    base = _good_frame(size)
    # Discord's backdrop is ~85% black composited over the app.
    dimmed = base.point(lambda v: int(v * 0.15))
    d = ImageDraw.Draw(dimmed)
    cw, ch = 600, 825
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    d.rounded_rectangle((x0, y0, x0 + cw, y0 + ch), radius=12, fill=_COMPOSER_BG)
    _text_rows(d, x0 + 40, x0 + cw - 40, y0 + 40, y0 + ch - 40, seed=4)
    return dimmed


def _skeleton_frame(size: tuple[int, int] = (1600, 1500)) -> Image.Image:
    """Still rendering: the channel sidebar is grey placeholder bars, no text."""
    w, h = size
    img = _good_frame(size)
    d = ImageDraw.Draw(img)
    d.rectangle((72, 0, 369, h), fill=_SIDEBAR_BG)
    for y in range(120, h - 120, 44):
        d.rounded_rectangle((96, y, 96 + 180, y + 14), radius=7, fill=(52, 54, 59))
    return img


def _not_a_channel_frame(size: tuple[int, int] = (1600, 1500)) -> Image.Image:
    """Landed somewhere that is not the requested channel: no message composer."""
    w, h = size
    img = _good_frame(size)
    d = ImageDraw.Draw(img)
    d.rectangle((370, h - 110, w, h), fill=_CHAT_BG)
    return img


def _save(img: Image.Image, path: Path) -> Path:
    img.save(path)
    return path


# --- inspect() -------------------------------------------------------------


def test_clean_capture_has_no_findings(tmp_path: Path) -> None:
    qc = _load_qc()
    path = _save(_good_frame(), tmp_path / "good.png")
    assert qc.inspect(path) == []


def test_modal_capture_is_reported(tmp_path: Path) -> None:
    qc = _load_qc()
    path = _save(_modal_frame(), tmp_path / "modal.png")
    codes = [f.code for f in qc.inspect(path)]
    assert "modal" in codes


def test_skeleton_capture_is_reported(tmp_path: Path) -> None:
    qc = _load_qc()
    path = _save(_skeleton_frame(), tmp_path / "skeleton.png")
    codes = [f.code for f in qc.inspect(path)]
    assert "loading" in codes


def test_wrong_screen_capture_is_reported(tmp_path: Path) -> None:
    qc = _load_qc()
    path = _save(_not_a_channel_frame(), tmp_path / "friends.png")
    codes = [f.code for f in qc.inspect(path)]
    assert "not-a-channel" in codes


def test_blank_capture_is_reported(tmp_path: Path) -> None:
    qc = _load_qc()
    path = _save(Image.new("RGB", (1600, 1500), _CHAT_BG), tmp_path / "blank.png")
    codes = [f.code for f in qc.inspect(path)]
    assert "blank" in codes


def test_findings_carry_a_human_readable_reason(tmp_path: Path) -> None:
    qc = _load_qc()
    path = _save(_modal_frame(), tmp_path / "modal.png")
    finding = next(f for f in qc.inspect(path) if f.code == "modal")
    # The operator has to learn that a human closes it once, for everyone.
    assert "モーダル" in finding.summary
    assert finding.detail  # measured numbers, so a threshold can be argued with


def test_checks_survive_a_tall_window(tmp_path: Path) -> None:
    """The documented modal mitigation is a very tall window — don't false-positive."""
    qc = _load_qc()
    path = _save(_good_frame((900, 2600)), tmp_path / "tall.png")
    assert qc.inspect(path) == []


# --- main() ----------------------------------------------------------------


def test_main_exits_zero_on_a_clean_capture(tmp_path: Path) -> None:
    qc = _load_qc()
    path = _save(_good_frame(), tmp_path / "good.png")
    assert qc.main([str(path)]) == 0


def test_main_exits_nonzero_on_a_modal_capture(tmp_path: Path) -> None:
    qc = _load_qc()
    path = _save(_modal_frame(), tmp_path / "modal.png")
    assert qc.main([str(path)]) == qc.EXIT_PROBLEM
    assert qc.EXIT_PROBLEM != 0


def test_main_reports_a_missing_file_without_claiming_success(tmp_path: Path) -> None:
    qc = _load_qc()
    assert qc.main([str(tmp_path / "nope.png")]) != 0
