"""Build hook: bake the commit date into the package (#756).

hatch-vcs records a date in the version only for a *dirty* build, so a clean
``uv tool install git+…`` / ``uv add git+…`` produced a c-lord that reported
``v1.4.197`` — no date. An installed instance therefore could not know how old
it is, and the stale-build notice (``c_lord.version.stale_build_age``) could
never fire for exactly the instances that fall behind.

This writes ``c_lord/_build_info.py`` (gitignored, like ``_version.py``) with
the commit date of the tree being built — the same ``git log`` date the
checkout path of ``resolve_version`` reads, so both report the same string.

Nothing here may fail a build: a missing date only means the age is unknown,
and the runtime already stays quiet about what it does not know.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

BUILD_INFO = "c_lord/_build_info.py"


def _commit_date(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--date=format:%Y%m%d", "--format=%cd"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = result.stdout.strip()
    if result.returncode != 0 or len(out) != 8 or not out.isdigit():
        return None
    return out


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        target = root / BUILD_INFO
        try:
            # Only this project's own history — never the date of some
            # enclosing repository an sdist happens to be unpacked inside.
            built = _commit_date(root) if (root / ".git").exists() else None
            if built is not None:
                target.write_text(
                    "# Generated at build time by hatch_build.py (#756) — do not edit.\n"
                    f'COMMIT_DATE = "{built}"\n',
                    encoding="utf-8",
                )
        except OSError:
            pass
        # From an sdist there is no ``.git``, but the sdist carries the file
        # written when it was built — ship that one.
        if target.exists():
            build_data["artifacts"].append(f"/{BUILD_INFO}")
