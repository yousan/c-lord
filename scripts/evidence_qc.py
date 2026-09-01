#!/usr/bin/env python3
"""Refuse to call an unusable capture "evidence" (c-lord Issue #559).

``scripts/discord_evidence_shot.sh`` used to print ``captured: <path>`` for
every frame it grabbed, including frames where the interesting part was hidden.
Three failure modes all produce a perfectly valid PNG with no evidentiary
value, and all three used to be reported as success:

1. a Discord promo modal ("ショップ新着" etc.) dims the app and sits in the middle
2. the client is still rendering (skeleton sidebar / empty body)
3. the client never opened the requested channel (wrong guild id, no permission,
   expired session) and shows the friends/login screen instead

This module inspects the PNG **after** the capture and reports those. It is a
pure image check on a file the capture already produced: it never touches the
Discord client, and it must never grow a "close the modal for me" path — see
the prohibition in ``docs/discord-evidence-capture.md``. A human closes the
modal once, by hand; the profile is shared, so that one action fixes every
future capture for every thread.

Thresholds come from real captures taken on 2026-09-01 against the c-lord
Discord — three usable frames at different window sizes, and one frame per
failure mode (values are ``fraction of pixels in the region``):

    frame                     luma<16   sidebar     body   composer
    1600x1500 --wait 45        0.0019    0.0207   0.0662     0.0376
    900x2600  (tall)           0.0021    0.0112   0.1068     0.0551
    900x900   (small)          0.0041    0.0383   0.1139     0.0484
    --wait 4  (skeleton)       0.0019    0.0000   0.0641     0.0407
    wrong guild (friends)      0.0019    0.0070   0.0135     0.0009
    session expired (login)    0.0040    0.0228   0.0296     0.0000
    promo modal                0.7529    0.0152   0.0307     0.0224

The modal row is a reconstruction: the live promo modal was dismissed by hand
on 2026-08-26 and bringing it back would mean automating the account, which is
forbidden. It composites Discord's own dark-theme scrim over a real capture —
``--background-scrim: hsl(var(--opacity-black-72-hsl)/0.72)``, i.e. 72% black,
read out of the shipped stylesheet on 2026-09-01. That scrim is what makes
``luma<16`` such a wide separation: every Discord surface (the darkest rail at
luma 31 up to the composer at 55) lands under 16 once it is multiplied by 0.28,
while glyphs stay above it. Clean frames only reach 0.004.

Usage:

    python3 scripts/evidence_qc.py shot.png     # exit 0 = usable, 2 = rejected
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

EXIT_CLEAN = 0
EXIT_PROBLEM = 2
EXIT_CANNOT_INSPECT = 3

# --- geometry (pixels of the captured frame, Discord's dark web client) ------
_RAIL_W = 72  # server rail — no text, always ignored
_SIDEBAR = (76, 360)  # channel / thread list column
_CHROME_TOP = 100  # window title + channel header
_CHROME_BOTTOM = 110  # composer + user panel
_BODY_X0 = 400  # message column starts right of the sidebar
_MEMBERS_W = 300  # member list on the right, skipped when the frame is wide

# --- thresholds --------------------------------------------------------------
# Each sits at least 3x away from the closest real measurement above.
_MODAL_DIM_RATIO = 0.35  # share of the frame pushed to near-black by a backdrop
_MODAL_OUTER_LIT = 0.15  # left/right edges must be dark for a backdrop to exist
_MODAL_CENTER_GAP = 0.25  # ...while the middle stays lit: that is the card
_SIDEBAR_EDGES = 0.003  # below this the channel list is placeholder bars
_BODY_EDGES = 0.005  # below this the message area holds no rendered content
_COMPOSER_EDGES = 0.010  # below this there is no message input box on screen
_MIN_SIZE = (700, 500)  # smaller frames are crops; only the modal check applies


# Bias: reject when in doubt. A false reject fails loudly with a reason and a
# `--no-qc` escape hatch; a false accept is the bug this module exists to kill.
# Known tight spot: a channel where the capture account cannot post shows a
# "you do not have permission to send messages" bar instead of the composer.


@dataclass(frozen=True)
class Finding:
    """One reason this PNG must not be pasted into a PR as evidence."""

    code: str
    summary: str
    detail: str


def _edges(gray):
    """Edge map of the whole frame.

    Filtering once, up front, matters: Pillow's 3x3 kernel leaves artefacts on
    the outermost row/column, so filtering a thin crop would measure its own
    border instead of the content (a flat 1160x86 band scores ~0.025 that way —
    enough to pass the "there is a composer here" check on an empty screen).
    """
    from PIL import ImageFilter

    return gray.filter(ImageFilter.FIND_EDGES)


def _edge_density(edges, box: tuple[int, int, int, int], cutoff: int) -> float:
    """Fraction of pixels inside ``box`` that sit on a strong edge.

    Rendered text is dense fine detail; skeleton bars and empty surfaces are
    smooth, so this separates "the client drew something" from "it did not".
    """
    crop = edges.crop(box)
    area = max(1, crop.width * crop.height)
    hist = crop.histogram()
    return sum(hist[cutoff:]) / area


def _dim_ratio(gray) -> float:
    """Fraction of the frame that a modal backdrop would have pushed to black."""
    hist = gray.histogram()
    return sum(hist[:16]) / max(1, gray.width * gray.height)


def _column_lit_profile(gray, cells: int = 100) -> list[float]:
    """Per-column share of pixels the backdrop did *not* dim, left to right."""
    from PIL import Image

    lit = gray.point(lambda v: 255 if v >= 24 else 0)
    small = lit.resize((cells, cells), Image.Resampling.BOX)
    px = small.load()
    return [sum(px[x, y] for y in range(cells)) / (cells * 255) for x in range(cells)]


def _check_modal(gray) -> Finding | None:
    dim = _dim_ratio(gray)
    if dim < _MODAL_DIM_RATIO:
        return None
    profile = _column_lit_profile(gray)
    edge = profile[:10] + profile[-10:]
    outer = sum(edge) / len(edge)
    middle = profile[35:65]
    center = sum(middle) / len(middle)
    if outer >= _MODAL_OUTER_LIT or center - outer < _MODAL_CENTER_GAP:
        return None
    return Finding(
        code="modal",
        summary=(
            "画面がモーダルに覆われている可能性があります"
            "（背景が暗転し、中央にカードが出ています）。"
            "人間に一度モーダルを閉じてもらってください — "
            "プロファイルは共有なので、その 1 回で全スレッド・全今後の実行に効きます。"
        ),
        detail=f"dim_ratio={dim:.3f} outer_lit={outer:.3f} center_lit={center:.3f}",
    )


def _check_still_loading(edges) -> Finding | None:
    w, h = edges.size
    box = (_SIDEBAR[0], _CHROME_TOP, min(_SIDEBAR[1], w), h - _CHROME_BOTTOM)
    density = _edge_density(edges, box, cutoff=64)
    if density >= _SIDEBAR_EDGES:
        return None
    return Finding(
        code="loading",
        summary=(
            "描画が終わる前に撮れています"
            "（チャンネル一覧がスケルトンのままで、文字が出ていません）。"
            "--wait を伸ばして撮り直してください。"
        ),
        detail=f"sidebar_edge_density={density:.4f} < {_SIDEBAR_EDGES}",
    )


def _check_blank_body(edges) -> Finding | None:
    w, h = edges.size
    x1 = max(_BODY_X0 + 120, w - _MEMBERS_W)
    density = _edge_density(edges, (_BODY_X0, _CHROME_TOP, x1, h - _CHROME_BOTTOM), cutoff=64)
    if density >= _BODY_EDGES:
        return None
    return Finding(
        code="blank",
        summary="本文領域に何も描かれていません（真っ白/真っ暗のまま撮れています）。",
        detail=f"body_edge_density={density:.4f} < {_BODY_EDGES}",
    )


def _check_not_a_channel(edges) -> Finding | None:
    w, h = edges.size
    box = (_BODY_X0, h - _CHROME_BOTTOM, w - 40, h - 24)
    density = _edge_density(edges, box, cutoff=32)
    if density >= _COMPOSER_EDGES:
        return None
    return Finding(
        code="not-a-channel",
        summary=(
            "要求したチャンネルを開けていません"
            "（メッセージ入力欄が画面下にありません）。"
            "guild ID の取り違え / 撮影アカウントの権限不足 / "
            "セッション失効（ログイン画面）のいずれかです。"
        ),
        detail=f"composer_edge_density={density:.4f} < {_COMPOSER_EDGES}",
    )


def inspect(path: str | Path) -> list[Finding]:
    """Return every reason ``path`` is unusable as evidence (empty list = good)."""
    from PIL import Image

    with Image.open(path) as img:
        gray = img.convert("L")

    findings: list[Finding] = []
    modal = _check_modal(gray)
    if modal is not None:
        findings.append(modal)
    if gray.width < _MIN_SIZE[0] or gray.height < _MIN_SIZE[1]:
        # A crop, not a raw capture: the layout checks below have no anchors.
        return findings
    edges = _edges(gray)
    for check in (_check_still_loading, _check_blank_body, _check_not_a_channel):
        found = check(edges)
        if found is not None:
            findings.append(found)
    return findings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        return EXIT_CLEAN if args else EXIT_PROBLEM
    path = Path(args[0])
    if not path.is_file():
        print(f"evidence_qc: no such file: {path}", file=sys.stderr)
        return EXIT_PROBLEM
    try:
        findings = inspect(path)
    except ImportError:
        print(
            "evidence_qc: Pillow が無いので画像検査をスキップしました "
            "(pip install pillow / uv sync --dev)",
            file=sys.stderr,
        )
        return EXIT_CANNOT_INSPECT
    if not findings:
        return EXIT_CLEAN
    print(f"evidence_qc: {path} は証跡として使えません:", file=sys.stderr)
    for f in findings:
        print(f"  [{f.code}] {f.summary}", file=sys.stderr)
        print(f"           ({f.detail})", file=sys.stderr)
    return EXIT_PROBLEM


if __name__ == "__main__":
    raise SystemExit(main())
