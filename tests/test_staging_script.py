"""Tests for scripts/staging.sh — the guarded bot lifecycle launcher (#327).

The script is environment-agnostic: it derives every value (identity, log
name, venv path) from the clone directory it is invoked in. These tests
exercise the guard rails that do not require a live Discord connection;
the login/identity path is verified on staging (see the PR's Staging
Evidence).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "staging.sh"


def run_script(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestScriptGuards:
    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.is_file()

    def test_refuses_dir_without_env_file(self, tmp_path: Path) -> None:
        """Any command outside a clone (no .env) must fail with a clear message."""
        result = run_script(["status"], cwd=tmp_path)
        assert result.returncode != 0
        assert ".env" in result.stdout + result.stderr

    def test_status_reports_zero_instances(self, tmp_path: Path) -> None:
        """status in a clone-shaped dir with no running bot -> instances: 0."""
        (tmp_path / ".env").write_text(
            "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\nEXPECTED_BOT_USER_ID=42\n",
            encoding="utf-8",
        )
        result = run_script(["status"], cwd=tmp_path)
        assert result.returncode == 0
        assert "instances: 0" in result.stdout

    def test_restart_refuses_without_venv(self, tmp_path: Path) -> None:
        """restart must not attempt a launch when the clone has no .venv."""
        (tmp_path / ".env").write_text(
            "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\n", encoding="utf-8"
        )
        result = run_script(["restart"], cwd=tmp_path)
        assert result.returncode != 0
        assert ".venv" in result.stdout + result.stderr

    def test_unknown_command_fails_with_usage(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(
            "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\n", encoding="utf-8"
        )
        result = run_script(["frobnicate"], cwd=tmp_path)
        assert result.returncode != 0
        assert "usage" in (result.stdout + result.stderr).lower()


class TestScriptSideIdentityCheck:
    """check-log: スクリプト側の identity 照合 (#327).

    Bot 側ガード (#323) に依存しない: 対象 clone が古いコード (#323/#324
    以前) のときの最後の砦。2026-06-10 の検証中、この照合の無い初版が
    継承 env + 旧コードの組み合わせで prod-identity boot を再現させた。
    """

    def _clone(self, tmp_path: Path, expected: str) -> Path:
        (tmp_path / ".env").write_text(
            f"DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\nEXPECTED_BOT_USER_ID={expected}\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_matching_identity_passes(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path, "111")
        log = tmp_path / "bot.log"
        log.write_text("[INFO] c_lord.bot: Logged in as Good#1 (ID: 111)\n", encoding="utf-8")
        result = run_script(["check-log", str(log)], cwd=clone)
        assert result.returncode == 0
        assert "identity verified: 111" in result.stdout

    def test_wrong_identity_fails(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path, "111")
        log = tmp_path / "bot.log"
        log.write_text("[INFO] c_lord.bot: Logged in as Evil#2 (ID: 222)\n", encoding="utf-8")
        result = run_script(["check-log", str(log)], cwd=clone)
        assert result.returncode != 0
        assert "222" in result.stdout
        assert "111" in result.stdout

    def test_unreadable_identity_fails(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path, "111")
        log = tmp_path / "bot.log"
        log.write_text("no login line here\n", encoding="utf-8")
        result = run_script(["check-log", str(log)], cwd=clone)
        assert result.returncode != 0

    def test_missing_expected_warns_but_passes(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(
            "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\n", encoding="utf-8"
        )
        log = tmp_path / "bot.log"
        log.write_text("[INFO] c_lord.bot: Logged in as Any#1 (ID: 999)\n", encoding="utf-8")
        result = run_script(["check-log", str(log)], cwd=tmp_path)
        assert result.returncode == 0
        assert "WARNING" in result.stdout
