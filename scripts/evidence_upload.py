#!/usr/bin/env python3
"""Upload evidence screenshots to a dedicated GitHub Release and print their URLs.

Evidence images (RED/GREEN screenshots, repro shots) must **not** be committed to
the source tree — binaries bloat git history irreversibly. This uploads them as
assets on a dedicated *prerelease* (tag ``evidence``) so they live on GitHub, are
permanent, and stay out of the git tree. Paste the printed URLs / markdown into
the PR or Issue body.

Why a Release asset (and not the drag-and-drop ``user-attachments`` CDN): GitHub
exposes **no public API** to upload to the ``user-attachments`` CDN — the web UI
does it through an internal endpoint that needs a browser session cookie, so a
headless bot cannot use it. Release assets are the official, token-based,
permanent, off-tree path (rendered inline for anyone with repo access, private
repos included). See ``docs/discord-evidence-capture.md``.

Requires the GitHub CLI (``gh``) authenticated with ``repo`` scope.

Usage:
    scripts/evidence_upload.py red.png green.png --issue 390
    scripts/evidence_upload.py shot.png --issue 390 --label repro --repo yousan/c-lord
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_TAG = "evidence"
DEFAULT_REPO = "yousan/c-lord"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize(token: str) -> str:
    """Reduce an arbitrary label/stem to a filesystem- and URL-safe token."""
    cleaned = _UNSAFE.sub("-", token).strip("-._")
    return cleaned or "evidence"


def asset_name(issue: int, label: str, stem: str, ts: str, idx: int | None = None) -> str:
    """Build a namespaced, collision-resistant asset filename.

    >>> asset_name(390, "red", "screenshot", "20260611T124500Z")
    'i390-red-screenshot-20260611T124500Z.png'
    >>> asset_name(390, "", "green", "20260611T124500Z")
    'i390-green-20260611T124500Z.png'
    """
    raw = [f"i{issue}"]
    if label:
        raw.append(sanitize(label))
    raw.append(sanitize(stem))
    raw.append(ts)
    if idx is not None:
        raw.append(f"{idx:02d}")
    parts: list[str] = []
    for part in raw:
        if part and part not in parts:
            parts.append(part)
    return "-".join(parts) + ".png"


def download_url(repo: str, tag: str, name: str) -> str:
    """Deterministic release-asset URL (how GitHub forms ``browser_download_url``)."""
    return f"https://github.com/{repo}/releases/download/{tag}/{name}"


def markdown_for(label: str, url: str) -> str:
    """Ready-to-paste markdown image embed."""
    return f"![{label}]({url})"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def ensure_release(repo: str, tag: str) -> None:
    """Create the evidence prerelease if it does not already exist."""
    view = _run(["gh", "release", "view", tag, "-R", repo, "--json", "tagName"])
    if view.returncode == 0:
        return
    create = _run(
        [
            "gh",
            "release",
            "create",
            tag,
            "-R",
            repo,
            "--prerelease",
            "--title",
            "Evidence assets (not a software release)",
            "--notes",
            (
                "Screenshot / evidence assets uploaded by scripts/evidence_upload.py. "
                "This prerelease exists purely to host PR/Issue evidence images off "
                "the git tree. Not a software release — safe to ignore for upgrades."
            ),
        ]
    )
    if create.returncode != 0:
        _die(f"failed to create release '{tag}': {create.stderr.strip()}")


def upload(repo: str, tag: str, src: Path, name: str) -> str:
    """Upload ``src`` under asset ``name`` (overwriting a same-named asset)."""
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / name
        shutil.copyfile(src, staged)
        res = _run(["gh", "release", "upload", tag, str(staged), "-R", repo, "--clobber"])
    if res.returncode != 0:
        _die(f"failed to upload {src}: {res.stderr.strip()}")
    return download_url(repo, tag, name)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload evidence PNGs to the dedicated GitHub 'evidence' release."
    )
    parser.add_argument("images", nargs="+", type=Path, help="image file(s) to upload")
    parser.add_argument(
        "--issue", "-i", type=int, required=True, help="Issue/PR number (namespaces the asset)"
    )
    parser.add_argument(
        "--label", default="", help="optional label prefix shared by all images (e.g. red, green)"
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo (default: %(default)s)")
    parser.add_argument("--tag", default=DEFAULT_TAG, help="release tag (default: %(default)s)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    images: list[Path] = args.images
    for img in images:
        if not img.is_file():
            _die(f"not a file: {img}")
    ensure_release(args.repo, args.tag)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    multiple = len(images) > 1
    for i, img in enumerate(images):
        label = args.label or img.stem
        name = asset_name(args.issue, args.label, img.stem, ts, idx=i if multiple else None)
        url = upload(args.repo, args.tag, img, name)
        print(url)
        print(markdown_for(label, url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
