"""Tests for scripts/staging.sh — the guarded bot lifecycle launcher (#327).

The script is environment-agnostic: it derives every value (identity, log
name, venv path) from the clone directory it is invoked in. These tests
exercise the guard rails that do not require a live Discord connection;
the login/identity path is verified on staging (see the PR's Staging
Evidence).
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

# scripts/staging.sh is the maintainers' Linux fleet tool — it finds bots through
# /proc and hands production to systemd. Not something a Mac user runs (#899).
pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="staging.sh is Linux-only (/proc, systemd)"
)

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


class TestInstanceCounting:
    """status / restart は parent+child（uv ラッパ + python 子）を 1 インスタンスと数える (#437).

    `uv run python -m c_lord.main` は uv ラッパ（親）+ python（実体・子）の 2 プロセスになり、
    `pgrep -f c_lord.main` は両方に当たる。これを 2 と誤カウントすると、検証者は「正常な
    parent+child」を「二重起動」と誤検出してしまう。論理インスタンス = 親が同一 clone の
    c_lord.main でない pid（= プロセスツリーの代表）だけを数える。

    テストは実プロセス（cwd=clone・cmdline に c_lord.main を含む sleep）を spawn して検証する。
    本物の bot や Discord 接続は不要。find_pids は cwd 一致で絞るので、この clone(tmp) の
    fake だけが対象になり、ホスト上の実 bot とは混ざらない。
    """

    def _clone_env(self, tmp_path: Path) -> Path:
        (tmp_path / ".env").write_text(
            "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\nEXPECTED_BOT_USER_ID=42\n",
            encoding="utf-8",
        )
        return tmp_path

    @staticmethod
    def _spawn_single(clone: Path) -> subprocess.Popen[bytes]:
        """staging 起動形: 単一 python プロセス（cmdline ~ c_lord.main, cwd=clone）。"""
        return subprocess.Popen(
            ["bash", "-c", 'exec -a "python -m c_lord.main" sleep 300'],
            cwd=str(clone),
            start_new_session=True,
        )

    @staticmethod
    def _spawn_uv_style(clone: Path) -> subprocess.Popen[bytes]:
        """prod 起動形: uv ラッパ（親）+ python（子）。子の ppid は親。両方 cmdline 一致。"""
        script = (
            'exec -a "uv run python -m c_lord.main" '
            "bash -c 'exec -a \"python -m c_lord.main child\" sleep 300 & wait'"
        )
        return subprocess.Popen(
            ["bash", "-c", script],
            cwd=str(clone),
            start_new_session=True,
        )

    @staticmethod
    def _count_procs(clone: Path) -> int:
        """staging.sh とは独立に、cwd=clone かつ cmdline ~ c_lord.main のプロセス数を数える。"""
        out = subprocess.run(
            ["pgrep", "-f", r"c_lord\.main"], capture_output=True, text=True
        ).stdout.split()
        n = 0
        for pid in out:
            with contextlib.suppress(OSError):
                if os.readlink(f"/proc/{pid}/cwd") == str(clone):
                    n += 1
        return n

    def _wait_procs(self, clone: Path, n: int, timeout: float = 6.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._count_procs(clone) >= n:
                return
            time.sleep(0.1)
        raise AssertionError(f"fake procs (cwd={clone}) が {n} に到達しない")

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)

    def test_status_counts_uv_wrapper_and_child_as_one(self, tmp_path: Path) -> None:
        """AC1: uv ラッパ(親)+python(子) は instances: 1（二重起動の誤検出をしない）。"""
        clone = self._clone_env(tmp_path)
        proc = self._spawn_uv_style(clone)
        try:
            self._wait_procs(clone, 2)  # 親+子の両方が上がるのを待つ
            result = run_script(["status"], cwd=clone)
            assert "instances: 1" in result.stdout, result.stdout
            assert "二重起動" not in result.stdout, result.stdout
            assert result.returncode == 0, result.stdout
        finally:
            self._kill(proc)

    def test_status_counts_two_independent_bots_as_two(self, tmp_path: Path) -> None:
        """本物の二重起動（独立した 2 プロセス）はちゃんと instances: 2 で検出する。"""
        clone = self._clone_env(tmp_path)
        p1 = self._spawn_single(clone)
        p2 = self._spawn_single(clone)
        try:
            self._wait_procs(clone, 2)
            result = run_script(["status"], cwd=clone)
            assert "instances: 2" in result.stdout, result.stdout
            assert result.returncode == 2, result.stdout  # WARNING: 二重起動の疑い
        finally:
            self._kill(p1)
            self._kill(p2)


class TestRestartReturnsToCaller:
    """restart は呼び出し元へ必ず return し、自分のプロセスを居残らせない (#401).

    旧起動行 ``(cd … && setsid env … nohup python … >log 2>&1 &)`` では、``&`` が
    作るサブシェル (cmdline は ``bash scripts/staging.sh restart …`` のまま) が
    bot の親として **bot の寿命いっぱい** ``wait`` し続け、しかもリダイレクトが
    nohup にしか掛かっていないため**呼び出し元の stdout/stderr を握ったまま**だった。
    staging.sh 本体は ``OK`` まで出して終わるのに、``$(…)`` やパイプで出力を
    読み切る呼び出し元には EOF が来ない — これが「restart が return しない」の正体。
    ``pgrep -af 'staging.sh restart'`` に残り続けたのもこのサブシェル。

    偽 bot (cwd=clone, cmdline に ``c_lord.main``, ログイン行を出して眠るだけ) を
    ``.venv/bin/python3`` に置き、本物の Discord 接続なしで launch 経路を通す。
    find_pids は cwd で絞るので、ホスト上の実 bot には触れない。
    """

    FAKE_BOT = (
        "#!/usr/bin/env python3\n"
        "import time\n"
        'print("[INFO] c_lord.bot: Logged in as Fake#0001 (ID: 42)", flush=True)\n'
        "time.sleep(300)\n"
    )
    FAKE_BOT_DIES = "#!/usr/bin/env python3\nraise SystemExit(1)\n"

    def _clone(self, tmp_path: Path, fake_bot: str) -> Path:
        # 名前はログ名 (/tmp/clord-bot-<name>-*.log) になるので一意にし、後で消す
        clone = tmp_path / f"i401-{tmp_path.name}"
        (clone / ".venv" / "bin").mkdir(parents=True)
        (clone / ".env").write_text(
            "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\nEXPECTED_BOT_USER_ID=42\n",
            encoding="utf-8",
        )
        py = clone / ".venv" / "bin" / "python3"
        py.write_text(fake_bot, encoding="utf-8")
        py.chmod(0o755)
        run_script(["borrow", "--owner", "sess-R", "--purpose", "#401 test"], cwd=clone)
        return clone

    @staticmethod
    def _procs_in(clone: Path) -> dict[int, str]:
        """cwd が clone のプロセス → cmdline。staging.sh の残骸も偽 bot も拾う。"""
        found: dict[int, str] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            with contextlib.suppress(OSError):
                if os.readlink(entry / "cwd") != str(clone):
                    continue
                raw = (entry / "cmdline").read_bytes()
                found[int(entry.name)] = raw.replace(b"\0", b" ").decode(errors="replace")
        return found

    def _cleanup(self, clone: Path) -> None:
        run_script(["stop", "--owner", "sess-R"], cwd=clone)
        for pid in self._procs_in(clone):  # stop が取りこぼした分 (この clone の中だけ)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        for log in Path("/tmp").glob(f"clord-bot-{clone.name}*.log"):
            log.unlink(missing_ok=True)

    def test_restart_returns_to_a_caller_reading_stdout_to_eof(self, tmp_path: Path) -> None:
        """AC1: ``$(…)`` / パイプで待つ呼び出し元にも有限時間で EOF が届く。

        capture_output は stdout/stderr を EOF まで読む = ``$(bash staging.sh restart)``
        と同じ待ち方。bot を握ったサブシェルが stdout を持ち続けると timeout する。
        """
        clone = self._clone(tmp_path, self.FAKE_BOT)
        try:
            try:
                result = subprocess.run(
                    ["bash", str(SCRIPT), "restart", "--owner", "sess-R"],
                    cwd=clone,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except subprocess.TimeoutExpired as exc:
                pytest.fail(
                    "restart did not return to a caller reading its stdout within 20s "
                    f"(#401); output so far: {exc.stdout!r}"
                )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "OK" in result.stdout
        finally:
            self._cleanup(clone)

    def test_restart_leaves_no_staging_process_behind(self, tmp_path: Path) -> None:
        """AC2: restart の後に ``staging.sh`` のプロセスが残らず、bot だけが生きている。"""
        clone = self._clone(tmp_path, self.FAKE_BOT)
        try:
            with (tmp_path / "restart.out").open("w") as out:
                result = subprocess.run(
                    ["bash", str(SCRIPT), "restart", "--owner", "sess-R"],
                    cwd=clone,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    timeout=30,
                )
            assert result.returncode == 0, (tmp_path / "restart.out").read_text()

            procs = self._procs_in(clone)
            leftovers = {p: c for p, c in procs.items() if "staging.sh" in c}
            assert leftovers == {}, f"staging.sh が居残っている (#401): {leftovers}"
            bots = [p for p, c in procs.items() if "c_lord.main" in c]
            assert len(bots) == 1, f"bot は 1 つ生きているはず: {procs}"
        finally:
            self._cleanup(clone)

    def test_restart_returns_when_bot_dies_at_startup(self, tmp_path: Path) -> None:
        """AC1 (失敗時): bot が起動直後に死んでも、非 0 で有限時間に return する。"""
        clone = self._clone(tmp_path, self.FAKE_BOT_DIES)
        try:
            result = subprocess.run(
                ["bash", str(SCRIPT), "restart", "--owner", "sess-R"],
                cwd=clone,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode != 0
            assert "起動直後に終了" in result.stderr
        finally:
            self._cleanup(clone)


class TestStopEscalation(TestRestartReturnsToCaller):
    """#699: a bot that ignores SIGTERM is SIGKILLed by PID, and that is logged.

    Before, ``cmd_stop`` waited 15 s and ``die``d — ``restart`` then never
    reached the launch, leaving production stopped until a human killed it.
    Inherits the fake-bot helpers from :class:`TestRestartReturnsToCaller` (#401).
    """

    FAKE_BOT_IGNORES_TERM = (
        "#!/usr/bin/env python3\n"
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        'print("[INFO] c_lord.bot: Logged in as Fake#0001 (ID: 42)", flush=True)\n'
        "time.sleep(300)\n"
    )

    # Only the test cases below; don't re-run the inherited #401 cases here.
    test_restart_returns_to_a_caller_reading_stdout_to_eof = None  # type: ignore[assignment]
    test_restart_leaves_no_staging_process_behind = None  # type: ignore[assignment]
    test_restart_returns_when_bot_dies_at_startup = None  # type: ignore[assignment]

    def test_stop_escalates_to_sigkill_and_logs_it(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path, self.FAKE_BOT_IGNORES_TERM)
        env = {**os.environ, "CLORD_STOP_GRACE_SECONDS": "2"}
        try:
            started = subprocess.run(
                ["bash", str(SCRIPT), "restart", "--owner", "sess-R"],
                cwd=clone,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert started.returncode == 0, started.stdout + started.stderr
            log = Path(f"/tmp/clord-bot-{clone.name}.log").resolve()

            result = subprocess.run(
                ["bash", str(SCRIPT), "stop", "--owner", "sess-R"],
                cwd=clone,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            output = result.stdout + result.stderr
            assert result.returncode == 0, output
            assert "SIGKILL" in output
            bots = [p for p, c in self._procs_in(clone).items() if "c_lord.main" in c]
            assert bots == [], f"bot survived stop: {bots}"
            # AC4: the bot's own log records that it was killed, not stopped.
            assert "SIGKILL" in log.read_text(encoding="utf-8")
        finally:
            self._cleanup(clone)


FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# staging.sh が叩く `systemctl --user ...` の偽物。呼ばれた引数を記録し、
# `show -p <PROP> --value <UNIT>` には環境変数で決めた値を返す。
echo "$*" >>"$FAKE_SYSTEMCTL_LOG"
if [ -n "${FAKE_SYSTEMCTL_FAIL:-}" ]; then
  echo "Failed to connect to bus: No such file or directory" >&2
  exit 1
fi
if [ "$2" = "show" ]; then
  case "$4" in
  ActiveState) echo "${FAKE_ACTIVE:-active}" ;;
  SubState) echo "${FAKE_SUB:-running}" ;;
  MainPID) echo "${FAKE_MAINPID:-0}" ;;
  NRestarts) echo "${FAKE_NRESTARTS:-0}" ;;
  ControlGroup) echo "${FAKE_CGROUP:-}" ;;
  FragmentPath) echo "${FAKE_FRAGMENT:-}" ;;
  *) echo "" ;;
  esac
fi
exit 0
"""

FAKE_JOURNALCTL = r"""#!/usr/bin/env bash
printf '%s\n' "${FAKE_JOURNAL:-}"
"""


def _own_cgroup() -> str:
    """この pytest プロセスの cgroup v2 パス（子プロセスはこれを継承する）。"""
    for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        if line.startswith("0::"):
            return line[3:]
    pytest.skip("cgroup v2 が無い環境")
    raise AssertionError  # unreachable


class TestSystemdManagedClone:
    """本番 (systemd の unit が管理する clone) では kill せず systemctl に委ねる (ops#2).

    staging.sh restart が本番の bot を kill → setsid で自前起動すると、systemd が
    「落ちた」と判断して立て直しを試み、単一インスタンスロックに弾かれて 6 回失敗 →
    `failed` で諦める。以後、本番は監視外で動き続ける（4 か月で少なくとも 3 回）。

    「本番かどうか」は unit ファイルの `WorkingDirectory=` が clone と一致するかで
    判定する（bus に繋がらなくても読める）。systemctl / journalctl は PATH の偽物で
    置き換え、呼び出しだけを検証する。ホストの本物の systemd には触れない。
    """

    def _setup(self, tmp_path: Path, *, managed: bool = True) -> tuple[Path, dict[str, str]]:
        clone = tmp_path / "clone"
        (clone / ".venv" / "bin").mkdir(parents=True)
        (clone / ".env").write_text(
            "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\nEXPECTED_BOT_USER_ID=42\n",
            encoding="utf-8",
        )
        xdg = tmp_path / "xdg"
        unit_dir = xdg / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        if managed:
            (unit_dir / "c-lord.service").write_text(
                f"[Service]\nWorkingDirectory={clone}\nExecStart=/bin/true\n", encoding="utf-8"
            )
        # 無関係な unit は拾わない
        (unit_dir / "other.service").write_text(
            f"[Service]\nWorkingDirectory={tmp_path}\n", encoding="utf-8"
        )
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("journalctl", FAKE_JOURNALCTL)):
            (bindir / name).write_text(body, encoding="utf-8")
            (bindir / name).chmod(0o755)
        log = tmp_path / "systemctl.log"
        log.touch()
        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "XDG_CONFIG_HOME": str(xdg),
            "FAKE_SYSTEMCTL_LOG": str(log),
            "FAKE_JOURNAL": "[INFO] c_lord.bot: Logged in as Prod#0001 (ID: 42)",
            "FAKE_MAINPID": "4242",
            "FAKE_CGROUP": _own_cgroup(),
            "CLORD_SYSTEMD_WAIT_SECONDS": "6",
            "CLORD_STOP_GRACE_SECONDS": "2",
        }
        env.pop("CLORD_LEASE_OWNER", None)
        return clone, env

    @staticmethod
    def _run(args: list[str], clone: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=clone,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @staticmethod
    def _calls(env: dict[str, str]) -> list[str]:
        return Path(env["FAKE_SYSTEMCTL_LOG"]).read_text(encoding="utf-8").splitlines()

    @staticmethod
    def _spawn_bot(clone: Path) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            ["bash", "-c", 'exec -a "python -m c_lord.main" sleep 300'],
            cwd=str(clone),
            start_new_session=True,
        )

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)

    @staticmethod
    def _wait_up(clone: Path, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = subprocess.run(
                ["pgrep", "-f", r"c_lord\.main"], capture_output=True, text=True
            ).stdout.split()
            for pid in out:
                with contextlib.suppress(OSError):
                    if os.readlink(f"/proc/{pid}/cwd") == str(clone):
                        return
            time.sleep(0.1)
        raise AssertionError("fake bot が上がらない")

    def test_restart_delegates_to_systemctl_and_does_not_kill(self, tmp_path: Path) -> None:
        """AC1: 本番 clone の restart は bot を kill せず `systemctl --user restart` を呼ぶ。"""
        clone, env = self._setup(tmp_path)
        bot = self._spawn_bot(clone)  # cgroup = unit の ControlGroup（= 監視下の bot）
        try:
            self._wait_up(clone)
            result = self._run(["restart"], clone, env)  # 本番はリース不要
            out = result.stdout + result.stderr
            assert result.returncode == 0, out
            assert "--user restart c-lord.service" in self._calls(env), self._calls(env)
            assert bot.poll() is None, "監視下の bot を staging.sh が kill した"
            assert "launched ->" not in out  # 自前起動の経路に落ちていない
            assert "OK" in out
        finally:
            self._kill(bot)

    def test_restart_errors_without_killing_when_systemctl_unusable(self, tmp_path: Path) -> None:
        """AC2: systemctl が使えないと本番に対してはエラーで止まり、bot を kill しない。"""
        clone, env = self._setup(tmp_path)
        env["FAKE_SYSTEMCTL_FAIL"] = "1"
        bot = self._spawn_bot(clone)
        try:
            self._wait_up(clone)
            result = self._run(["restart"], clone, env)
            out = result.stdout + result.stderr
            assert result.returncode != 0, out
            assert "systemctl" in out
            assert bot.poll() is None, "systemctl 不通なのに bot を kill した"
            assert "launched ->" not in out
        finally:
            self._kill(bot)

    def test_stop_delegates_to_systemctl(self, tmp_path: Path) -> None:
        clone, env = self._setup(tmp_path)
        result = self._run(["stop"], clone, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "--user stop c-lord.service" in self._calls(env)

    def test_stop_errors_when_systemctl_unusable(self, tmp_path: Path) -> None:
        clone, env = self._setup(tmp_path)
        env["FAKE_SYSTEMCTL_FAIL"] = "1"
        bot = self._spawn_bot(clone)
        try:
            self._wait_up(clone)
            result = self._run(["stop"], clone, env)
            assert result.returncode != 0
            assert bot.poll() is None
        finally:
            self._kill(bot)

    def test_restart_hands_unsupervised_bot_over_to_systemd(self, tmp_path: Path) -> None:
        """監視外の bot（unit の cgroup の外）が居たら、それを止めてから systemd に渡す。

        放っておくと systemd の起動が単一インスタンスロックに弾かれて `failed` に戻る。
        """
        clone, env = self._setup(tmp_path)
        env["FAKE_CGROUP"] = "/user.slice/fake.slice/c-lord.service"  # bot はこの外
        orphan = self._spawn_bot(clone)
        try:
            self._wait_up(clone)
            result = self._run(["restart"], clone, env)
            out = result.stdout + result.stderr
            assert result.returncode == 0, out
            assert "監視外" in out
            orphan.wait(timeout=10)  # 止められている
            calls = self._calls(env)
            assert "--user restart c-lord.service" in calls
            assert "--user reset-failed c-lord.service" in calls
        finally:
            self._kill(orphan)

    def test_restart_fails_when_unit_does_not_come_up(self, tmp_path: Path) -> None:
        clone, env = self._setup(tmp_path)
        env["FAKE_ACTIVE"] = "failed"
        env["FAKE_JOURNAL"] = "Start request repeated too quickly."
        result = self._run(["restart"], clone, env)
        assert result.returncode != 0
        assert "OK" not in result.stdout

    def test_restart_fails_on_identity_mismatch_in_journal(self, tmp_path: Path) -> None:
        clone, env = self._setup(tmp_path)
        env["FAKE_JOURNAL"] = "[INFO] c_lord.bot: Logged in as Evil#2 (ID: 999)"
        result = self._run(["restart"], clone, env)
        out = result.stdout + result.stderr
        assert result.returncode != 0, out
        assert "999" in out

    def test_status_reports_supervision(self, tmp_path: Path) -> None:
        """「監視されているか」は unit の状態と cgroup で見せる（親が systemd かでは見ない）。"""
        clone, env = self._setup(tmp_path)
        bot = self._spawn_bot(clone)
        try:
            self._wait_up(clone)
            result = self._run(["status"], clone, env)
            out = result.stdout
            assert result.returncode == 0, out
            assert "c-lord.service" in out
            assert "active" in out
            assert "監視下" in out
        finally:
            self._kill(bot)

    def test_status_flags_unsupervised_bot(self, tmp_path: Path) -> None:
        clone, env = self._setup(tmp_path)
        env["FAKE_ACTIVE"] = "failed"
        env["FAKE_MAINPID"] = "0"
        env["FAKE_CGROUP"] = ""
        bot = self._spawn_bot(clone)
        try:
            self._wait_up(clone)
            result = self._run(["status"], clone, env)
            assert result.returncode == 2, result.stdout
            assert "監視外" in result.stdout
        finally:
            self._kill(bot)

    def test_status_warns_when_unit_drifts_from_repo(self, tmp_path: Path) -> None:
        """本番の unit が repo の deploy/c-lord.service と違えば status が警告する。"""
        clone, env = self._setup(tmp_path)
        (clone / "deploy").mkdir()
        (clone / "deploy" / "c-lord.service").write_text("[Service]\n# repo\n", encoding="utf-8")
        env["FAKE_FRAGMENT"] = str(tmp_path / "xdg" / "systemd" / "user" / "c-lord.service")
        result = self._run(["status"], clone, env)
        assert "deploy/c-lord.service" in result.stdout, result.stdout

    def test_unmanaged_clone_never_calls_systemctl(self, tmp_path: Path) -> None:
        """staging（unit の無い clone）は従来どおり — systemctl を呼ばない。"""
        clone, env = self._setup(tmp_path, managed=False)
        env["CLORD_LEASE_OWNER"] = "sess-S"
        self._run(["borrow", "--purpose", "t"], clone, env)
        (clone / ".venv" / "bin").rmdir()
        result = self._run(["restart"], clone, env)
        assert ".venv" in result.stdout + result.stderr
        assert self._calls(env) == []


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestReleaseSleepsLeaseWindows:
    """release で、そのリース中に立った検証窓を寝かせる (#955).

    窓は conftest が隔離した tmux サーバ (``TMUX_TMPDIR``) に本物を立てる。
    ``sleep`` を抱えた窓を Claude の代わりにし、``@clord_owner`` / ``@thread_id``
    を bot と同じ形で付ける。
    """

    SESSION = "t955"

    @pytest.fixture(autouse=True)
    def _tmux_session(self):
        yield
        subprocess.run(["tmux", "kill-session", "-t", self.SESSION], capture_output=True)

    def _clone(self, tmp_path: Path, *, api_port: int | None = None) -> Path:
        clone = tmp_path / "clone"
        (clone / "data").mkdir(parents=True)
        env = "DISCORD_BOT_TOKEN=dummy\nDISCORD_CHANNEL_ID=1\n"
        env += f"CLORD_API_PORT={api_port if api_port is not None else _free_port()}\n"
        (clone / ".env").write_text(env, encoding="utf-8")
        db = sqlite3.connect(clone / "data" / "sessions.db")
        db.execute(
            "CREATE TABLE sessions (thread_id INTEGER PRIMARY KEY, closed_reason TEXT,"
            " created_at TIMESTAMP)"
        )
        db.commit()
        db.close()
        return clone

    def _row(self, clone: Path, thread_id: int, *, age: str = "+0 seconds") -> None:
        db = sqlite3.connect(clone / "data" / "sessions.db")
        db.execute(
            "INSERT INTO sessions (thread_id, created_at)"
            " VALUES (?, datetime('now', 'localtime', ?))",
            (thread_id, age),
        )
        db.commit()
        db.close()

    def _window(self, name: str, owner: str, thread_id: int) -> str:
        has = subprocess.run(["tmux", "has-session", "-t", self.SESSION], capture_output=True)
        cmd = (
            ["new-window", "-t", self.SESSION]
            if has.returncode == 0
            else ["new-session", "-d", "-s", self.SESSION]
        )
        pane = subprocess.run(
            ["tmux", *cmd, "-n", name, "-P", "-F", "#{pane_id}", "sleep", "300"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        for opt, value in (("@clord_owner", owner), ("@thread_id", str(thread_id))):
            subprocess.run(["tmux", "set-option", "-w", "-t", pane, opt, value], check=True)
        return pane

    def _panes(self) -> set[str]:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}"], capture_output=True, text=True
        )
        return set(out.stdout.split())

    def _logging_tmux(self, tmp_path: Path) -> dict[str, str]:
        """A ``tmux`` on PATH that logs each call, then runs the real one."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "tmux.log"
        real = shutil.which("tmux")
        (bindir / "tmux").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{real}" "$@"\n')
        (bindir / "tmux").chmod(0o755)
        return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "TMUX_LOG": str(log)}

    def _run(self, args: list[str], clone: Path, env: dict[str, str] | None = None):
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=clone,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def test_release_closes_only_this_leases_windows(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path)
        owner = os.path.realpath(clone)
        self._row(clone, 900, age="-1 day")  # リース前から居たスレッド
        before = self._window("w1", owner, 900)
        assert self._run(["borrow", "--owner", "A", "--purpose", "t"], clone).returncode == 0
        self._row(clone, 901)
        self._row(clone, 902)
        spawned = [self._window("w2", owner, 901), self._window("w3", owner, 902)]
        other = self._window("w4", "/home/someone/c-lord", 901)  # 他の台・本番の窓
        env = self._logging_tmux(tmp_path)

        result = self._run(["release", "--owner", "A"], clone, env)

        assert result.returncode == 0, result.stderr
        assert "検証窓: 2 本寝かせた / 約" in result.stdout
        assert "MB" in result.stdout
        alive = self._panes()
        assert not set(spawned) & alive
        assert before in alive
        assert other in alive
        calls = Path(env["TMUX_LOG"]).read_text()
        for forbidden in ("kill-server", "kill-session", "kill-window"):
            assert forbidden not in calls
        assert all(f"kill-pane -t {pane}" in calls for pane in spawned)  # API 不達 → pane id

    def test_release_asks_the_bot_api_first(self, tmp_path: Path) -> None:
        """bot に届けば、bot 自身のスリープ (POST /api/threads/<id>/sleep) に任せる。"""
        asked: list[str] = []
        panes: dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                asked.append(self.path)
                thread_id = self.path.split("/")[3]
                subprocess.run(["tmux", "kill-pane", "-t", panes[thread_id]])
                body = json.dumps({"status": "slept", "thread_id": thread_id}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            clone = self._clone(tmp_path, api_port=server.server_address[1])
            assert self._run(["borrow", "--owner", "A", "--purpose", "t"], clone).returncode == 0
            self._row(clone, 911)
            panes["911"] = self._window("w1", os.path.realpath(clone), 911)
            env = self._logging_tmux(tmp_path)

            result = self._run(["release", "--owner", "A"], clone, env)
        finally:
            server.shutdown()

        assert result.returncode == 0, result.stderr
        assert asked == ["/api/threads/911/sleep"]
        assert "検証窓: 1 本寝かせた" in result.stdout
        assert "API 1 本" in result.stdout
        assert "pane kill" not in result.stdout
        assert "kill-pane" not in Path(env["TMUX_LOG"]).read_text()

    def test_taking_over_an_expired_lease_sleeps_its_windows(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path)
        args = ["borrow", "--owner", "A", "--purpose", "放置", "--ttl-hours", "0"]
        assert self._run(args, clone).returncode == 0
        self._row(clone, 921)
        pane = self._window("w1", os.path.realpath(clone), 921)

        result = self._run(["borrow", "--owner", "B", "--purpose", "奪取"], clone)

        assert result.returncode == 0, result.stderr
        assert "検証窓: 1 本寝かせた" in result.stdout
        assert pane not in self._panes()

    def test_nothing_to_sleep_is_one_quiet_line(self, tmp_path: Path) -> None:
        clone = self._clone(tmp_path)
        assert self._run(["borrow", "--owner", "A", "--purpose", "t"], clone).returncode == 0

        result = self._run(["release", "--owner", "A"], clone)

        assert result.returncode == 0
        assert "検証窓: このリース中に立ったスレッドは無い" in result.stdout
        assert "released." in result.stdout

    # -- #1014: 同じ owner の再 borrow でリースの始まりを動かさない -----------

    def _lease(self, clone: Path) -> dict:
        return json.loads((clone / ".staging-lease").read_text())

    def _age_lease(self, clone: Path, seconds: int) -> None:
        """リースを *seconds* 秒前に取ったことにする (時計を待たずに)。"""
        data = self._lease(clone)
        data["acquired_epoch"] -= seconds
        (clone / ".staging-lease").write_text(json.dumps(data))

    def test_reborrow_keeps_the_lease_start(self, tmp_path: Path) -> None:
        """#1014 AC1. RED: 再 borrow が acquired_epoch を今の時刻に書き換えた。"""
        clone = self._clone(tmp_path)
        assert self._run(["borrow", "--owner", "A", "--purpose", "t"], clone).returncode == 0
        self._age_lease(clone, 3600)
        first = self._lease(clone)

        result = self._run(["borrow", "--owner", "A", "--purpose", "延長", "--ttl-hours", "2"], clone)

        assert result.returncode == 0, result.stderr
        lease = self._lease(clone)
        assert lease["acquired_epoch"] == first["acquired_epoch"]
        assert lease["acquired_at"] == first["acquired_at"]
        assert lease["purpose"] == "延長"
        # 延長は効く: 今から 2 時間 (始まりから 3 時間)
        remaining = lease["acquired_epoch"] + lease["ttl_hours"] * 3600 - time.time()
        assert 7100 < remaining <= 7200

    def test_release_after_a_reborrow_sleeps_the_first_half_too(self, tmp_path: Path) -> None:
        """#1014 AC2. RED: 1 回目と 2 回目の borrow の間に立てた窓が残った。"""
        clone = self._clone(tmp_path)
        owner = os.path.realpath(clone)
        self._row(clone, 940, age="-1 day")
        before = self._window("w1", owner, 940)
        assert self._run(["borrow", "--owner", "A", "--purpose", "t"], clone).returncode == 0
        self._age_lease(clone, 600)
        self._row(clone, 941, age="-300 seconds")  # 1 回目の borrow の後、再 borrow の前
        first_half = self._window("w2", owner, 941)
        assert self._run(["borrow", "--owner", "A", "--purpose", "延長"], clone).returncode == 0

        result = self._run(["release", "--owner", "A"], clone)

        assert result.returncode == 0, result.stderr
        alive = self._panes()
        assert first_half not in alive
        assert before in alive

    def test_taking_over_an_expired_lease_starts_a_new_one(self, tmp_path: Path) -> None:
        """#1014 AC3: 別 owner の奪取は新しい始まり。"""
        clone = self._clone(tmp_path)
        args = ["borrow", "--owner", "A", "--purpose", "放置", "--ttl-hours", "0"]
        assert self._run(args, clone).returncode == 0
        self._age_lease(clone, 3600)
        old = self._lease(clone)["acquired_epoch"]

        assert self._run(["borrow", "--owner", "B", "--purpose", "奪取"], clone).returncode == 0

        assert self._lease(clone)["acquired_epoch"] >= old + 3600

    # -- #1015: bot が答えたら kill しない ----------------------------------

    def _api(self, status: int, body: dict | None = None):
        asked: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                asked.append(self.path)
                payload = json.dumps(body or {"error": "x"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, asked

    @pytest.mark.parametrize("status", [500, 503])
    def test_an_http_error_from_the_bot_does_not_kill_the_pane(
        self, tmp_path: Path, status: int
    ) -> None:
        """#1015 AC1. RED: 答えが ``slept`` 以外なら全部 kill-pane に進んだ。

        bot が窓を把握していて寝かせるのに失敗した答え (500 / 503) だけ。"""
        server, asked = self._api(status)
        try:
            clone = self._clone(tmp_path, api_port=server.server_address[1])
            assert self._run(["borrow", "--owner", "A", "--purpose", "t"], clone).returncode == 0
            self._row(clone, 951)
            pane = self._window("w1", os.path.realpath(clone), 951)
            env = self._logging_tmux(tmp_path)

            result = self._run(["release", "--owner", "A"], clone, env)
        finally:
            server.shutdown()

        assert result.returncode == 0, result.stderr
        assert asked == ["/api/threads/951/sleep"]
        assert "kill-pane" not in Path(env["TMUX_LOG"]).read_text()
        assert pane in self._panes()
        assert f"HTTP {status}" in result.stdout

    @pytest.mark.parametrize(
        ("status", "body"),
        [(200, {"status": "not_resident", "thread_id": "952"}), (404, {"error": "x"})],
    )
    def test_an_answer_that_the_bot_has_no_such_window_kills_the_pane(
        self, tmp_path: Path, status: int, body: dict
    ) -> None:
        """#1015 (おぷー決定 2026-10-29): ``not_resident`` / 404 は bot がその窓を
        持っていない答え。4 時間スリープ (#572) も同じ経路なので拾わない — だから
        従来どおり pane id 指定で kill する。RED: 残していた (1 本 ~300MB が居座る)。"""
        server, asked = self._api(status, body)
        try:
            clone = self._clone(tmp_path, api_port=server.server_address[1])
            assert self._run(["borrow", "--owner", "A", "--purpose", "t"], clone).returncode == 0
            self._row(clone, 952)
            pane = self._window("w1", os.path.realpath(clone), 952)
            env = self._logging_tmux(tmp_path)

            result = self._run(["release", "--owner", "A"], clone, env)
        finally:
            server.shutdown()

        assert result.returncode == 0, result.stderr
        assert asked == ["/api/threads/952/sleep"]
        assert f"kill-pane -t {pane}" in Path(env["TMUX_LOG"]).read_text()
        assert pane not in self._panes()
        assert "検証窓: 1 本寝かせた" in result.stdout
        assert "pane kill 1 本" in result.stdout


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
