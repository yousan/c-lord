"""Tests for the dev-only before/after screenshot composer (Issue #310).

This exercises ``scripts/compose_screenshots.py`` — a standalone dev tool that
is intentionally NOT part of the importable ``c_lord`` package. The test loads
it by file path so it never depends on the runtime package layout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from PIL import Image

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "compose_screenshots.py"


def _load_compose():
    """Load ``compose`` from the standalone script by file path."""
    spec = importlib.util.spec_from_file_location("compose_screenshots", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.compose


def _make_png(path: Path, size: tuple[int, int], color: str) -> None:
    Image.new("RGB", size, color).save(path)


def test_compose_creates_valid_png_side_by_side(tmp_path: Path) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    out = tmp_path / "diff.png"
    w1, h1 = 120, 80
    w2, h2 = 100, 60
    _make_png(before, (w1, h1), "red")
    _make_png(after, (w2, h2), "blue")

    compose = _load_compose()
    result = compose(str(before), str(after), str(out))

    # Output file created.
    assert out.exists()
    assert Path(result) == out

    # Valid PNG.
    with Image.open(out) as img:
        img.verify()
    with Image.open(out) as img:
        assert img.format == "PNG"
        width, height = img.size

    # Side-by-side: total width is at least the sum of the two panels.
    assert width >= w1 + w2
    # Height covers the tallest panel plus the label band.
    assert height >= max(h1, h2)


def test_compose_handles_differing_sizes(tmp_path: Path) -> None:
    before = tmp_path / "b.png"
    after = tmp_path / "a.png"
    out = tmp_path / "out.png"
    _make_png(before, (200, 50), "green")
    _make_png(after, (40, 300), "yellow")

    compose = _load_compose()
    compose(str(before), str(after), str(out))

    with Image.open(out) as img:
        width, height = img.size
    assert width >= 200 + 40
    assert height >= 300
