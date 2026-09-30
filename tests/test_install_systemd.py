"""Tests for scripts/install-systemd.sh — 本番の unit を repo と一致させる (ops#2).

本番の unit が repo の外 (untracked の起動スクリプト + 手書きの unit) にあると、
repo を読んでも本番の起動のされ方が分からず、手順書と実機が食い違う。
install-systemd.sh は repo の ``deploy/c-lord.service`` を**そのまま**置き、
ホスト固有の値 (clone の場所・uv・PATH) だけを drop-in に書く。
systemctl / loginctl は PATH の偽物で置き換え、ホストの systemd には触れない。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "install-systemd.sh"


def _run(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for name, body in (
        ("systemctl", f'#!/usr/bin/env bash\necho "systemctl $*" >>{log}\n'),
        ("loginctl", "#!/usr/bin/env bash\necho Linger=yes\n"),
    ):
        (bindir / name).write_text(body, encoding="utf-8")
        (bindir / name).chmod(0o755)
    xdg = tmp_path / "xdg"
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "XDG_CONFIG_HOME": str(xdg)}
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )
    return result, xdg / "systemd" / "user", log


def test_installed_unit_is_the_repo_unit_verbatim(tmp_path: Path) -> None:
    result, unit_dir, _ = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    installed = (unit_dir / "c-lord.service").read_bytes()
    assert installed == (REPO / "deploy" / "c-lord.service").read_bytes()


def test_host_specific_values_go_to_a_drop_in(tmp_path: Path) -> None:
    result, unit_dir, _ = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    drop_in = (unit_dir / "c-lord.service.d" / "10-install.conf").read_text(encoding="utf-8")
    assert f"WorkingDirectory={REPO}\n" in drop_in
    # ExecStart は一度空にしてから上書きしないと systemd は 2 本目として足してしまう
    assert "ExecStart=\nExecStart=" in drop_in
    assert "run python -m c_lord.main" in drop_in
    assert "Environment=PATH=" in drop_in


def test_resets_failed_state_before_starting(tmp_path: Path) -> None:
    """立て直しに諦めた (start-limit-hit) unit でも install で起動し直せる。"""
    result, _, log = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert "systemctl --user daemon-reload" in calls
    assert "systemctl --user reset-failed c-lord.service" in calls
    assert calls.index("systemctl --user reset-failed c-lord.service") < calls.index(
        "systemctl --user enable --now c-lord.service"
    )
