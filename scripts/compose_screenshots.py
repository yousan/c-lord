#!/usr/bin/env python3
"""Compose two screenshots side-by-side with "Before" / "After" labels.

A dev-time helper for writing Issue/PR/completion reports (c-lord Issue #310).
It takes two PNGs and emits a single image with them placed horizontally,
each panel labeled. It is intentionally standalone: it does NOT import from the
``c_lord`` runtime package and is not wired into the bot. Run it directly:

    python scripts/compose_screenshots.py before.png after.png -o diff.png

Out of scope (by design): arrow / freeform annotation — see Issue #310.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Layout constants (pixels).
_LABEL_BAND = 28  # vertical space reserved above each panel for its label
_GAP = 16  # horizontal gap between the two panels
_MARGIN = 12  # outer margin around the whole canvas
_BG = (255, 255, 255)  # canvas background
_FG = (0, 0, 0)  # label text color


def _load_font() -> ImageFont.ImageFont:
    """Return a usable font, falling back to Pillow's bundled default."""
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
    except OSError:
        return ImageFont.load_default()


def compose(before_path: str, after_path: str, out_path: str) -> str:
    """Place two images side-by-side with Before/After labels and save the result.

    Images of differing sizes are handled: each panel is top-aligned, the total
    width is the sum of the two panel widths (plus gap/margins), and the height
    is the tallest panel plus the label band (plus margins). Returns ``out_path``.
    """
    with Image.open(before_path) as before_img, Image.open(after_path) as after_img:
        before = before_img.convert("RGB")
        after = after_img.convert("RGB")

    panels = ((before, "Before"), (after, "After"))
    panel_h = max(before.height, after.height)

    canvas_w = _MARGIN * 2 + before.width + _GAP + after.width
    canvas_h = _MARGIN * 2 + _LABEL_BAND + panel_h

    canvas = Image.new("RGB", (canvas_w, canvas_h), _BG)
    draw = ImageDraw.Draw(canvas)
    font = _load_font()

    x = _MARGIN
    for panel, label in panels:
        draw.text((x, _MARGIN), label, fill=_FG, font=font)
        canvas.paste(panel, (x, _MARGIN + _LABEL_BAND))
        x += panel.width + _GAP

    canvas.save(out_path, format="PNG")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compose two PNGs side-by-side with Before/After labels (dev tool).",
    )
    parser.add_argument("before", help="Path to the 'before' PNG.")
    parser.add_argument("after", help="Path to the 'after' PNG.")
    parser.add_argument(
        "-o",
        "--output",
        default="compose.png",
        help="Output PNG path (default: compose.png).",
    )
    args = parser.parse_args()

    out = compose(args.before, args.after, args.output)
    print(f"Wrote {Path(out).resolve()}")


if __name__ == "__main__":
    main()
