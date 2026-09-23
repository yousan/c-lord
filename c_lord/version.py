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
* **Runtime pin** (``runtime_version``) — what the *running process* is, as
  opposed to what is on disk right now. Everything user-facing (the boot log
  line, the 📊 footer, ``/version``) reports this one (#722).
* **Build age** (``build_date``, ``stale_build_age``, ``label_with_age``) — how
  old that build is, judged from the version string's ``-YYYYMMDD`` tail and
  today's date only. No network: an OSS framework must not phone home by
  default, and must keep working where it cannot (#756).
"""

from __future__ import annotations

import importlib
import logging
import re
import subprocess
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

BumpLevel = Literal["major", "minor", "patch"]

# setuptools_scm / hatch-vcs local version, e.g. "1.4.1.dev3+g599631.d20251203"
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: A build at least this many days old says so — a WARNING at boot and
#: ``(Nd)`` after the version in the 📊 footer (#756). One place, on purpose.
STALE_BUILD_DAYS = 7

# The article format's date tail: "v1.4.197-b3f06814-20260915" -> "20260915".
_DATE_TAIL_RE = re.compile(r"-(\d{8})$")


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


def build_date(version: str) -> date | None:
    """Return the commit date in an article-format version string, or None.

    ``None`` whenever there is no usable ``-YYYYMMDD`` tail — ``"unknown"``, a
    bare ``v1.4.197``, or a tail that is not a real date.
    """
    match = _DATE_TAIL_RE.search(version)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def stale_build_age(version: str, today: date | None = None) -> int | None:
    """Return the build's age in days once it is worth saying, else None.

    #756: the version string already says when the build is from, but nobody
    turns ``-20260908`` into "two weeks ago" — so instances ran builds 18
    releases behind while their users hit bugs fixed on main. This does that
    arithmetic, locally: the date tail plus today, nothing else.

    ``None`` for a build younger than :data:`STALE_BUILD_DAYS`, and for one
    whose date is unknown (知らないことは黙る — never "None days"). A date in
    the future (a skewed clock) is not stale either.
    """
    built = build_date(version)
    if built is None:
        return None
    age = ((today or date.today()) - built).days
    return age if age >= STALE_BUILD_DAYS else None


def label_with_age(version: str, today: date | None = None) -> str:
    """Return ``version``, followed by `` (Nd)`` when the build is stale."""
    age = stale_build_age(version, today)
    return f"{version} ({age}d)" if age is not None else version


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


def _baked_commit_date() -> str | None:
    """Return the commit date ``hatch_build.py`` baked into the wheel, if any.

    Imported by name at runtime: the module only exists in a built package
    (it is generated at build time and gitignored, like ``_version.py``).
    """
    try:
        info = importlib.import_module("c_lord._build_info")
    except Exception:
        return None
    value = getattr(info, "COMMIT_DATE", None)
    if isinstance(value, str) and _DATE_TAIL_RE.search(f"-{value}"):
        return value
    return None


def _installed_version() -> str | None:
    """Resolve the version of an installed package (no ``.git``), or None.

    hatch-vcs puts a date in the local version only for a *dirty* build, so a
    clean install reports ``1.4.197`` / ``1.4.198.dev1+g3f06814`` with no date.
    The commit date baked in at build time fills that gap (#756) — otherwise
    the build age could never be known for exactly the instances that fall
    behind (installed packages, as opposed to checkouts that get pulled).
    """
    dist = _distribution_version()
    if not dist:
        return None
    base, commit, built = parse_local_version(dist)
    return format_version_string(base, commit, built or _baked_commit_date())


def resolve_version() -> str:
    """Resolve the running build's article-format version string.

    Order of preference:

    1. **Live git** (running from a checkout): latest tag + short commit +
       commit date — the freshest, exact answer.
    2. **Installed metadata** (``hatch-vcs``-baked, e.g. a wheel): parse the
       local version to recover commit/date when present, taking the date from
       the build hook's ``_build_info`` when the local version has none.
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

    return _installed_version() or "unknown"


@lru_cache(maxsize=1)
def runtime_version() -> str:
    """Return the version of the build **this process is running**, pinned.

    :func:`resolve_version` describes the checkout *on disk*. That is the right
    answer for ``c-lord version`` on a shell, but not for a bot that has been
    up for days: the process loaded its code at import time, so a ``git pull``
    (or a ``uv tool upgrade``) into the same tree makes disk and process
    disagree. Re-reading later would make a running old build claim to be the
    new one — a lie in exactly the direction #722 exists to stop. So the value
    is resolved once, on first use, and kept for the process's lifetime; a
    restart is what changes it, which is also what changes the running code.

    (Keeping it also means no ``git describe`` subprocess per turn — the footer
    reads this for free, the same rule the CLI version follows in
    ``docs/specs/context-footer.md``.)

    Never raises: a version banner must not be able to take the bot down. An
    unresolvable build reports ``"unknown"``, and callers that render it to
    users drop the item rather than showing that.
    """
    try:
        return resolve_version()
    except Exception:  # pragma: no cover - defensive; resolve_version swallows its own
        logger.debug("could not resolve the running c-lord version", exc_info=True)
        return "unknown"
