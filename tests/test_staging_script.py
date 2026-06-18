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


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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
        """restart must not attempt a launch when the clone has no .venv.

        (#328 以降 restart はリース必須なので、borrow してから venv 層に到達する)
        """
        (tmp_path / ".env").write_text(
            "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\n", encoding="utf-8"
        )
        run_script(["borrow", "--owner", "sess-T", "--purpose", "test"], cwd=tmp_path)
        result = run_script(["restart", "--owner", "sess-T"], cwd=tmp_path)
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


class TestLease:
    """borrow/release/TTL — staging 占有リース (#328).

    1 つの staging working tree を複数セッションが取り合う問題の機械的防止。
    リースは clone 直下の .staging-lease(環境ごとに 1 枚、中央台帳なし)。
    """

    def _clone(self, tmp_path: Path) -> Path:
        (tmp_path / ".env").write_text(
            "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\n", encoding="utf-8"
        )
        return tmp_path

    def test_borrow_creates_lease(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path)
        result = run_script(["borrow", "--owner", "sess-A", "--purpose", "PR #999 検証"], cwd=clone)
        assert result.returncode == 0
        assert (clone / ".staging-lease").is_file()
        assert "sess-A" in (clone / ".staging-lease").read_text()

    def test_second_borrower_is_refused_with_owner_info(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path)
        run_script(["borrow", "--owner", "sess-A", "--purpose", "PR #999 検証"], cwd=clone)
        result = run_script(["borrow", "--owner", "sess-B", "--purpose", "別件"], cwd=clone)
        assert result.returncode != 0
        out = result.stdout + result.stderr
        assert "sess-A" in out  # 誰が
        assert "PR #999" in out  # 何のために

    def test_same_owner_can_reborrow(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path)
        run_script(["borrow", "--owner", "sess-A", "--purpose", "x"], cwd=clone)
        result = run_script(["borrow", "--owner", "sess-A", "--purpose", "x続き"], cwd=clone)
        assert result.returncode == 0

    def test_expired_lease_can_be_taken_over(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path)
        run_script(
            ["borrow", "--owner", "sess-A", "--purpose", "放置", "--ttl-hours", "0"],
            cwd=clone,
        )
        result = run_script(["borrow", "--owner", "sess-B", "--purpose", "奪取"], cwd=clone)
        assert result.returncode == 0
        out = result.stdout + result.stderr
        assert "sess-A" in out  # 奪取時は旧リース内容をログに残す
        assert "sess-B" in (clone / ".staging-lease").read_text()

    def test_release_by_owner(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path)
        run_script(["borrow", "--owner", "sess-A", "--purpose", "x"], cwd=clone)
        result = run_script(["release", "--owner", "sess-A"], cwd=clone)
        assert result.returncode == 0
        assert not (clone / ".staging-lease").exists()

    def test_release_by_non_owner_is_refused(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path)
        run_script(["borrow", "--owner", "sess-A", "--purpose", "x"], cwd=clone)
        result = run_script(["release", "--owner", "sess-B"], cwd=clone)
        assert result.returncode != 0
        assert (clone / ".staging-lease").exists()

    def test_after_release_other_owner_can_borrow_immediately(self, tmp_path: Path) -> None:
        """AC: release 後は別 owner が即 borrow できる。"""
        clone = self._clone(tmp_path)
        run_script(["borrow", "--owner", "sess-A", "--purpose", "x"], cwd=clone)
        run_script(["release", "--owner", "sess-A"], cwd=clone)
        result = run_script(["borrow", "--owner", "sess-B", "--purpose", "y"], cwd=clone)
        assert result.returncode == 0
        assert "sess-B" in (clone / ".staging-lease").read_text()

    def test_refusal_shows_remaining_time(self, tmp_path: Path) -> None:
        """AC: 拒否時に所有者・目的・残り時間が表示される。"""
        clone = self._clone(tmp_path)
        run_script(
            ["borrow", "--owner", "sess-A", "--purpose", "PR #999", "--ttl-hours", "2"],
            cwd=clone,
        )
        result = run_script(["borrow", "--owner", "sess-B", "--purpose", "z"], cwd=clone)
        assert result.returncode != 0
        assert "remaining=" in result.stdout + result.stderr

    def test_restart_refused_while_leased_to_other(self, tmp_path: Path) -> None:
        """他人の有効リース中の restart は venv チェックより前に拒否される。"""
        clone = self._clone(tmp_path)
        run_script(["borrow", "--owner", "sess-A", "--purpose", "PR #999 検証"], cwd=clone)
        result = run_script(["restart", "--owner", "sess-B"], cwd=clone)
        assert result.returncode != 0
        assert "sess-A" in result.stdout + result.stderr

    def test_restart_allowed_for_lease_owner(self, tmp_path: Path) -> None:
        """自リースなら restart はリース層を通過する(.venv が無いので後段で落ちる)。"""
        clone = self._clone(tmp_path)
        run_script(["borrow", "--owner", "sess-A", "--purpose", "x"], cwd=clone)
        result = run_script(["restart", "--owner", "sess-A"], cwd=clone)
        assert result.returncode != 0
        assert ".venv" in result.stdout + result.stderr  # リースでなく venv で落ちた

    def test_restart_without_lease_is_refused(self, tmp_path: Path) -> None:
        """AC: 有効な自リースなしの restart は拒否(必ず borrow が先)。"""
        clone = self._clone(tmp_path)
        result = run_script(["restart", "--owner", "sess-A"], cwd=clone)
        assert result.returncode != 0
        assert "borrow" in (result.stdout + result.stderr).lower()

    def test_stop_is_also_lease_guarded(self, tmp_path: Path) -> None:
        """stop も他人の有効リース中は拒否(他人の検証中 bot を殺す事故の防止)。"""
        clone = self._clone(tmp_path)
        run_script(["borrow", "--owner", "sess-A", "--purpose", "PR #999 検証"], cwd=clone)
        result = run_script(["stop", "--owner", "sess-B"], cwd=clone)
        assert result.returncode != 0
        assert "sess-A" in result.stdout + result.stderr


class TestRestartBranchSync:
    """restart <branch> は origin/<branch> へ確実に同期してから起動する (#436).

    単なる `git checkout <branch>` はローカルブランチを古い HEAD のまま切り替える
    だけで、`git fetch` 済みでも origin に追従しない。検証者は「最新の fix を回した
    つもりで古いコード」を起動し、偽の RED/GREEN を得る (#399 検証中に実害)。
    """

    def _origin_and_clone(self, tmp_path: Path) -> tuple[Path, str, str]:
        """origin/feature を 2 コミット先 (c2) に進め、clone は feature@c1 で stale。

        戻り値: (clone, c1, c2) — c1=stale ローカル HEAD, c2=origin/feature。
        """
        origin = tmp_path / "origin"
        origin.mkdir()
        _git(origin, "init", "-q", "-b", "main")
        _git(origin, "config", "user.email", "t@example.com")
        _git(origin, "config", "user.name", "t")
        (origin / "VERSION").write_text("c1\n", encoding="utf-8")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-qm", "c1")
        _git(origin, "branch", "feature")  # feature@c1

        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(clone, "config", "user.email", "t@example.com")
        _git(clone, "config", "user.name", "t")
        _git(clone, "checkout", "-q", "feature")  # ローカル feature@c1 (stale)
        c1 = _git(clone, "rev-parse", "HEAD")

        # origin/feature を c2 に進める (clone はまだ知らない)
        _git(origin, "checkout", "-q", "feature")
        (origin / "VERSION").write_text("c2\n", encoding="utf-8")
        _git(origin, "commit", "-aqm", "c2")
        c2 = _git(origin, "rev-parse", "HEAD")
        _git(origin, "checkout", "-q", "main")  # fetch 専用にしておく

        (clone / ".env").write_text(
            "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\nEXPECTED_BOT_USER_ID=42\n",
            encoding="utf-8",
        )
        return clone, c1, c2

    def test_restart_fast_forwards_branch_to_origin(self, tmp_path: Path) -> None:
        """AC1/AC2: restart <branch> は HEAD を origin/<branch> に ff し、回す sha を出す。

        .venv が無いので launch 自体は後段で落ちるが、ブランチ同期はそれより前に
        完了していなければならない (古いコードを起動させない)。
        """
        clone, c1, c2 = self._origin_and_clone(tmp_path)
        run_script(["borrow", "--owner", "sess-S", "--purpose", "sync test"], cwd=clone)
        assert _git(clone, "rev-parse", "HEAD") == c1  # 前提: stale

        result = run_script(["restart", "feature", "--owner", "sess-S"], cwd=clone)

        after = _git(clone, "rev-parse", "HEAD")
        assert after == c2, (
            f"branch not fast-forwarded to origin (after={after[:7]} want={c2[:7]})\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        # AC2: 回しているコミットを一目で確認できる行
        assert f"checked out feature @ {c2[:7]}" in result.stdout

    def test_restart_refuses_when_local_diverges(self, tmp_path: Path) -> None:
        """AC1: ff 不能 (ローカルが分岐) なら黙って古いコードを起動せず明示エラーで止まる。"""
        clone, _, _ = self._origin_and_clone(tmp_path)
        run_script(["borrow", "--owner", "sess-S", "--purpose", "diverge test"], cwd=clone)
        # ローカル feature に origin に無いコミットを積む → origin/feature と分岐
        (clone / "LOCAL").write_text("local only\n", encoding="utf-8")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-qm", "local-only")
        local_head = _git(clone, "rev-parse", "HEAD")

        result = run_script(["restart", "feature", "--owner", "sess-S"], cwd=clone)

        assert result.returncode != 0
        assert "fast-forward" in (result.stdout + result.stderr).lower()
        # 黙って origin の c2 に飛んだり起動したりしない (HEAD は触らず止まる)
        assert _git(clone, "rev-parse", "HEAD") == local_head
