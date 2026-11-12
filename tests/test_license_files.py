"""LICENSE names c-lord's copyright holder; NOTICE keeps the upstream notice (#1341).

c-lord started as a copy of claude-code-discord-bridge (MIT). Its license asks
that the original copyright notice and permission notice travel with every copy,
so they live in NOTICE and both files are declared as license files of the
package. These tests are what stops a later tidy-up from dropping either half.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_NOTICE = "Copyright (c) 2026 Masahiko Ebisuda"
UPSTREAM_URL = "https://github.com/ebibibi/claude-code-discord-bridge"
# sha256 of the permission notice as it stood in the code c-lord started from
# (commit 88a05f73). Pinned here, so the check does not lean on LICENSE staying intact.
UPSTREAM_PERMISSION_SHA256 = "21a2121221d2a35df2d8d333d00a61ed240446b6107092741ff996941acdc919"


def _permission_text(mit_license: str) -> str:
    """The MIT permission notice: everything from "Permission is hereby granted" on."""
    return mit_license[mit_license.index("Permission is hereby granted") :].strip()


def test_license_names_one_copyright_holder() -> None:
    lines = [ln for ln in (ROOT / "LICENSE").read_text().splitlines() if "Copyright (c)" in ln]

    assert lines == ["Copyright (c) 2026 yousan"]


def test_notice_keeps_the_upstream_copyright_and_the_whole_permission_notice() -> None:
    notice = (ROOT / "NOTICE").read_text()

    assert UPSTREAM_NOTICE in notice
    assert UPSTREAM_URL in notice
    # The upstream notice sits at the end of NOTICE, word for word.
    reproduced = _permission_text(notice[notice.index(UPSTREAM_NOTICE) :])
    assert hashlib.sha256(reproduced.encode()).hexdigest() == UPSTREAM_PERMISSION_SHA256


def test_license_carries_the_same_permission_notice() -> None:
    ours = _permission_text((ROOT / "LICENSE").read_text())

    assert hashlib.sha256(ours.encode()).hexdigest() == UPSTREAM_PERMISSION_SHA256


def test_both_files_are_declared_as_license_files_of_the_package() -> None:
    # Read as text: tomllib only exists from Python 3.11 and CI also runs 3.10.
    declared = [
        ln.strip()
        for ln in (ROOT / "pyproject.toml").read_text().splitlines()
        if ln.startswith("license-files")
    ]

    assert declared == ['license-files = ["LICENSE", "NOTICE"]']
