"""Version helpers for c-lord.

Implements the article-style runtime version string
``v1.4.0-b599631-20251203`` (semver tag + short commit + commit date) so a
running bot can report *exactly* which build it is — making "which version is
this bug from?" answerable without guesswork.
See https://qiita.com/yousan/items/cffa19f67f225097127d.

Layers:

* **Pure helpers** (``format_version_string``, ``parse_local_version``,
  ``bump_version``, ``detect_bump_level``, ``extract_changelog_section``) —
  no side effects, heavily unit-tested. Reused by ``scripts/release.sh``.
* **Resolver** (``resolve_version``) — thin, side-effecting: reads live git
  metadata when running from a checkout, otherwise falls back to the version
  baked in at build time by ``hatch-vcs`` (``importlib.metadata``).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Literal

BumpLevel = Literal["major", "minor", "patch"]

# setuptools_scm / hatch-vcs local version, e.g. "1.4.1.dev3+g599631.d20251203"
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def format_version_string(base: str, commit: str | None, date: str | None) -> str:
    """Build the article-format string ``v{base}[-b{commit}][-{date}]``.

    Missing (``None`` or empty) ``commit`` / ``date`` segments are omitted.
    A leading ``v`` on ``base`` is normalised away so we never emit ``vv``.
    """
    base = base.lstrip("v")
    out = f"v{base}"
    if commit:
        out += f"-b{commit}"
    if date:
        out += f"-{date}"
    return out


def parse_local_version(raw: str) -> tuple[str, str | None, str | None]:
    """Split a PEP 440 / setuptools_scm version into (base, commit, date).

    Examples::

        "1.4.0"                       -> ("1.4.0", None, None)
        "1.4.1.dev3+g599631.d20251203" -> ("1.4.1", "599631", "20251203")
        "1.4.1.dev3+g599631"           -> ("1.4.1", "599631", None)

    Used as the fallback path when no live git metadata is available (e.g. an
    installed wheel), recovering the commit/date that ``hatch-vcs`` embedded.
    """
    raw = raw.lstrip("v")
    public, _, local = raw.partition("+")
    base = public.split(".dev", 1)[0]

    commit: str | None = None
    date: str | None = None
    if local:
        for token in local.split("."):
            if token.startswith("g") and len(token) > 1:
                commit = token[1:]
            elif token.startswith("d") and token[1:].isdigit():
                date = token[1:]
    return base, commit, date


def bump_version(current: str, level: BumpLevel) -> str:
    """Return ``current`` bumped by ``level`` (major/minor/patch).

    A leading ``v`` is accepted and stripped. Returns a bare ``X.Y.Z`` string.
    """
    if level not in ("major", "minor", "patch"):
        raise ValueError(f"unknown bump level: {level!r}")
    bare = current.lstrip("v")
    if not _SEMVER_RE.match(bare):
        raise ValueError(f"not a X.Y.Z version: {current!r}")
    major, minor, patch = (int(p) for p in bare.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def detect_bump_level(message: str) -> BumpLevel:
    """Infer the bump level from a commit/PR-title ``message``.

    ``[major]`` wins, then ``[minor]`` / ``[release]`` (release is a minor
    boundary), otherwise ``patch``. Case-insensitive.
    """
    lowered = message.lower()
    if "[major]" in lowered:
        return "major"
    if "[minor]" in lowered or "[release]" in lowered:
        return "minor"
    return "patch"


def extract_changelog_section(changelog_text: str, version: str) -> str | None:
    """Return the body of the ``## [<version>] - ...`` section, or None.

    Assumes Keep a Changelog format. The returned text excludes the heading
    line itself and runs up to (but not including) the next ``## [`` heading.
    """
    target = version.lstrip("v")
    lines = changelog_text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(f"## [{target}]"):
            start = i + 1
            break
    if start is None:
        return None

    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## ["):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


# ---------------------------------------------------------------------------
# Resolver (side-effecting, thin)
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a read-only git command, returning stripped stdout or None.

    Uses ``subprocess.run`` with an explicit arg list (never ``shell=True``)
    per the project security rules. Any failure (no git, no repo, error) maps
    to ``None`` so callers can fall back cleanly.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _distribution_version() -> str | None:
    """Return the installed distribution version via importlib.metadata."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("c-lord")
    except PackageNotFoundError:
        return None
    except Exception:
        return None


def resolve_version() -> str:
    """Resolve the running build's article-format version string.

    Order of preference:

    1. **Live git** (running from a checkout): latest tag + short commit +
       commit date — the freshest, exact answer.
    2. **Installed metadata** (``hatch-vcs``-baked, e.g. a wheel): parse the
       local version to recover commit/date when present.
    3. ``"unknown"`` if nothing is available.
    """
    repo_root = Path(__file__).resolve().parent.parent

    if (repo_root / ".git").exists():
        tag = _git(["describe", "--tags", "--abbrev=0"], repo_root)
        commit = _git(["rev-parse", "--short=7", "HEAD"], repo_root)
        date = _git(
            ["log", "-1", "--date=format:%Y%m%d", "--format=%cd"],
            repo_root,
        )
        base = (tag or "").lstrip("v")
        if not base:
            # No tags yet — fall back to distribution version's base if any.
            dist = _distribution_version()
            base = parse_local_version(dist)[0] if dist else "0.0.0"
        return format_version_string(base, commit, date)

    dist = _distribution_version()
    if dist:
        base, commit, date = parse_local_version(dist)
        return format_version_string(base, commit, date)

    return "unknown"
