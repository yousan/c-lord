"""Default Chrome profile of scripts/discord_evidence_shot.sh — Issue #837.

The profile moved under ``~/.c-lord`` with the rest of c-lord's own output;
a host that already logged the test account into the old
``~/.clord/discord-evidence-profile`` keeps using it (a fresh profile would mean
another human login).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "discord_evidence_shot.sh"


def _profile(home: Path, **extra: str) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(home), **extra}
    out = subprocess.run(
        ["bash", str(SCRIPT), "--show-profile"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_new_host_uses_home_dot_c_lord(tmp_path: Path) -> None:
    assert _profile(tmp_path) == f"{tmp_path}/.c-lord/evidence-profile"


def test_existing_legacy_profile_keeps_being_used(tmp_path: Path) -> None:
    (tmp_path / ".clord" / "discord-evidence-profile").mkdir(parents=True)
    assert _profile(tmp_path) == f"{tmp_path}/.clord/discord-evidence-profile"


def test_new_profile_wins_once_it_exists(tmp_path: Path) -> None:
    (tmp_path / ".clord" / "discord-evidence-profile").mkdir(parents=True)
    (tmp_path / ".c-lord" / "evidence-profile").mkdir(parents=True)
    assert _profile(tmp_path) == f"{tmp_path}/.c-lord/evidence-profile"


def test_env_override_still_wins(tmp_path: Path) -> None:
    (tmp_path / ".clord" / "discord-evidence-profile").mkdir(parents=True)
    assert _profile(tmp_path, CLORD_EVIDENCE_PROFILE="/x/p") == "/x/p"


def test_profile_flag_still_wins(tmp_path: Path) -> None:
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    out = subprocess.run(
        ["bash", str(SCRIPT), "--profile", "/y/p", "--show-profile"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "/y/p"
