"""push していないコミットや、git が無視するファイルのある作業場所は「きれい」ではない — #903。

30日スイープ (#575) と孤児スイープ (#613) は ``git status`` が空なら消していた。
ところが ``git status`` には

* **どの remote ブランチにも載っていないコミット**（Claude がブランチに commit
  したが push していない作業）も、
* **``.gitignore`` / ``.git/info/exclude`` で無視されたファイル**（ローカルのメモ・
  ``.env``・手順書 ``AUDIT.md``）も

出てこない。どちらも消えたら取り戻せないので、残す側に倒す。

ただし ``node_modules/`` や ``.venv/`` のような**再生成できる**無視ディレクトリまで
「作業」と数えると、スイープがほぼ効かなくなる（#749 の 118 GB の再来）。c-lord が
自分で書いたファイル（#749/#779 の SKILL.md、#882 の履歴、#528 の添付）も同様に数えない。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from c_lord import session_dir
from c_lord.database.repository import SessionRecord
from c_lord.orphan_dirs import remove_orphan_dir
from c_lord.session_cleanup import DirOutcome, remove_clean_session_dir
from c_lord.session_dir import _is_clean, exclude_thread_history, worktree_status
from c_lord.session_reattach import HISTORY_FILENAME
from c_lord.skills.injector import inject_read_skill

THREAD = 1559120173389062204

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
}


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(path), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _session_clone(tmp_path: Path) -> Path:
    """What ``create_session_dir`` leaves: a clone whose ``main`` is on origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("hello\n")
    (seed / ".gitignore").write_text(".env\nnode_modules/\n.venv/\n__pycache__/\nnotes/\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", str(origin), "main")
    ws = tmp_path / "sessions" / "1505747831447883806" / str(THREAD)
    ws.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(ws)], check=True, env=_GIT_ENV)
    return ws


def _sweep(ws: Path, tmp_path: Path) -> DirOutcome:
    record = SessionRecord(
        thread_id=THREAD,
        session_id="a" * 32,
        working_dir=str(ws),
        model="opus",
        origin="discord",
        summary=None,
        created_at="2026-09-01 00:00:00",
        last_used_at="2026-09-11 00:00:00",
    )
    return remove_clean_session_dir(record, sessions_root=tmp_path / "sessions")


def test_a_plain_clone_is_still_removed(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)

    assert _sweep(ws, tmp_path) is DirOutcome.REMOVED
    assert not ws.exists()


# ── AC1: push されていないコミット ─────────────────────────────────────────────


def test_unpushed_commit_on_a_branch_is_kept(tmp_path: Path, caplog) -> None:
    """Issue 本文の再現そのもの。``git status --porcelain`` は空。"""
    ws = _session_clone(tmp_path)
    _git(ws, "checkout", "-q", "-b", "feature/wip")
    (ws / "w.txt").write_text("work\n")
    _git(ws, "add", "w.txt")
    _git(ws, "commit", "-qm", "wip")
    assert _git(ws, "status", "--porcelain") == ""

    with caplog.at_level(logging.INFO, logger="c_lord.session_cleanup"):
        outcome = _sweep(ws, tmp_path)

    assert outcome is not DirOutcome.REMOVED
    assert outcome is DirOutcome.KEPT_DIRTY
    assert (ws / "w.txt").exists()
    assert "1 unpushed commit" in caplog.text  # AC4


def test_unpushed_commit_on_a_branch_not_checked_out_is_kept(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)
    _git(ws, "checkout", "-q", "-b", "feature/wip")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "wip")
    _git(ws, "checkout", "-q", "main")

    assert _sweep(ws, tmp_path) is DirOutcome.KEPT_DIRTY


def test_commit_on_a_detached_head_is_kept(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)
    _git(ws, "checkout", "-q", "--detach")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "detached work")

    assert _sweep(ws, tmp_path) is DirOutcome.KEPT_DIRTY


def test_a_stash_is_kept(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)
    (ws / "README.md").write_text("half-done\n")
    _git(ws, "stash", "-q")
    assert _git(ws, "status", "--porcelain") == ""

    assert _sweep(ws, tmp_path) is DirOutcome.KEPT_DIRTY


def test_a_pushed_branch_is_removed(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)
    _git(ws, "checkout", "-q", "-b", "feature/done")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "done")
    _git(ws, "push", "-q", "origin", "feature/done")

    assert _sweep(ws, tmp_path) is DirOutcome.REMOVED


def _single_branch_clone(tmp_path: Path) -> Path:
    """本物の session dir の形: ``main`` だけを取る clone（#903 相談）。

    ``remote.origin.fetch`` が ``main`` 1本なので、作業ブランチを push しても
    手元に ``origin/<branch>`` ができない。
    """
    ws = _session_clone(tmp_path)
    shutil.rmtree(ws)
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--single-branch",
            "-b",
            "main",
            str(tmp_path / "origin.git"),
            str(ws),
        ],
        check=True,
        env=_GIT_ENV,
    )
    return ws


def test_a_branch_pushed_without_a_tracking_ref_is_removed(tmp_path: Path) -> None:
    """先端が remote の head と一致すれば push 済み — ``git ls-remote`` で照合する。"""
    ws = _single_branch_clone(tmp_path)
    _git(ws, "checkout", "-q", "-b", "feature/done")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "done")
    _git(ws, "push", "-q", "origin", "feature/done")
    assert "feature/done" not in _git(ws, "for-each-ref", "refs/remotes")

    assert _sweep(ws, tmp_path) is DirOutcome.REMOVED


def test_a_commit_on_top_of_a_pushed_branch_is_kept(tmp_path: Path, caplog) -> None:
    ws = _single_branch_clone(tmp_path)
    _git(ws, "checkout", "-q", "-b", "feature/wip")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "pushed")
    _git(ws, "push", "-q", "origin", "feature/wip")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "not yet")

    with caplog.at_level(logging.INFO, logger="c_lord.session_cleanup"):
        assert _sweep(ws, tmp_path) is DirOutcome.KEPT_DIRTY
    assert "1 unpushed commit" in caplog.text


def test_a_pushed_branch_with_a_stash_is_kept(tmp_path: Path) -> None:
    ws = _single_branch_clone(tmp_path)
    _git(ws, "checkout", "-q", "-b", "feature/done")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "done")
    _git(ws, "push", "-q", "origin", "feature/done")
    (ws / "README.md").write_text("half-done\n")
    _git(ws, "stash", "-q")

    assert _sweep(ws, tmp_path) is DirOutcome.KEPT_DIRTY


def test_an_unreachable_remote_keeps_the_dir(tmp_path: Path, caplog) -> None:
    """照合できなければ「残す」。理由にも照合できなかったことが出る。"""
    ws = _single_branch_clone(tmp_path)
    _git(ws, "checkout", "-q", "-b", "feature/done")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "done")
    _git(ws, "push", "-q", "origin", "feature/done")
    _git(ws, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    with caplog.at_level(logging.INFO, logger="c_lord.session_cleanup"):
        assert _sweep(ws, tmp_path) is DirOutcome.KEPT_DIRTY
    assert "1 unpushed commit" in caplog.text
    assert "ls-remote failed" in caplog.text


def test_an_ls_remote_timeout_keeps_the_dir(tmp_path: Path, monkeypatch) -> None:
    ws = _single_branch_clone(tmp_path)
    _git(ws, "checkout", "-q", "-b", "feature/done")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "done")
    _git(ws, "push", "-q", "origin", "feature/done")

    real_run = subprocess.run

    def hanging_run(args, *a, **kw):
        if "ls-remote" in args:
            raise subprocess.TimeoutExpired(args, kw.get("timeout") or 0)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(session_dir.subprocess, "run", hanging_run)

    assert _sweep(ws, tmp_path) is DirOutcome.KEPT_DIRTY


def test_ls_remote_runs_only_for_a_dir_that_would_otherwise_be_removed(
    tmp_path: Path, monkeypatch
) -> None:
    """消す直前の候補だけ照合する。

    他の理由で残る dir・未 push の無い dir ではネットワークを叩かない。
    """
    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(args, *a, **kw):
        if "ls-remote" in args:
            calls.append(list(args))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(session_dir.subprocess, "run", counting_run)

    (tmp_path / "a").mkdir()
    plain = _single_branch_clone(tmp_path / "a")
    assert _sweep(plain, tmp_path / "a") is DirOutcome.REMOVED
    assert calls == []

    (tmp_path / "b").mkdir()
    dirty = _single_branch_clone(tmp_path / "b")
    _git(dirty, "checkout", "-q", "-b", "feature/wip")
    _git(dirty, "commit", "-q", "--allow-empty", "-m", "wip")
    (dirty / ".env").write_text("SECRET=1\n")
    assert _sweep(dirty, tmp_path / "b") is DirOutcome.KEPT_DIRTY
    assert calls == []


# ── AC2: git が無視する、利用者のファイル ──────────────────────────────────────


def test_file_hidden_by_info_exclude_is_kept(tmp_path: Path, caplog) -> None:
    """#902 で消えた ``AUDIT.md`` の形。"""
    ws = _session_clone(tmp_path)
    (ws / "AUDIT.md").write_text("手順書\n")
    with (ws / ".git" / "info" / "exclude").open("a") as f:
        f.write("/AUDIT.md\n")
    assert _git(ws, "status", "--porcelain") == ""

    with caplog.at_level(logging.INFO, logger="c_lord.session_cleanup"):
        outcome = _sweep(ws, tmp_path)

    assert outcome is DirOutcome.KEPT_DIRTY
    assert (ws / "AUDIT.md").exists()
    assert "AUDIT.md" in caplog.text  # AC4


def test_ignored_dotenv_is_kept(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)
    (ws / ".env").write_text("API_KEY=local\n")

    assert _sweep(ws, tmp_path) is DirOutcome.KEPT_DIRTY


def test_file_inside_an_ignored_directory_is_kept(tmp_path: Path) -> None:
    """``notes/`` は丸ごと無視されている。git はディレクトリ1行しか返さない。"""
    ws = _session_clone(tmp_path)
    (ws / "notes").mkdir()
    (ws / "notes" / "todo.md").write_text("続き\n")

    assert _sweep(ws, tmp_path) is DirOutcome.KEPT_DIRTY


@pytest.mark.parametrize(
    "rel",
    [
        "node_modules/left-pad/index.js",
        ".venv/bin/python",
        "pkg/__pycache__/mod.cpython-312.pyc",
        ".pytest_cache/v/cache/nodeids",
        "storage/framework/views/0a1b.php",
        "bootstrap/cache/packages.php",
        "src/pkg/_version.py",
    ],
)
def test_regenerable_ignored_files_do_not_keep_the_dir(tmp_path: Path, rel: str) -> None:
    """線引き: ツールが作り直せるものは作業ではない（さもないとスイープが効かない）。"""
    ws = _session_clone(tmp_path)
    with (ws / ".git" / "info" / "exclude").open("a") as f:
        f.write(".pytest_cache/\nstorage/framework/\nbootstrap/cache/\n_version.py\n")
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("generated\n")

    assert _sweep(ws, tmp_path) is DirOutcome.REMOVED


# ── AC3: c-lord が自分で書いた無視ファイルだけなら従来どおり消える ──────────


def test_only_clord_owned_ignored_files_is_still_removed(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)
    inject_read_skill(ws)  # #779: hidden by info/exclude → "!!" not "??"
    (ws / HISTORY_FILENAME).write_text("# thread\n")
    exclude_thread_history(ws)
    attachments = ws / ".clord" / "attachments"
    attachments.mkdir(parents=True)
    (attachments / "shot.png").write_bytes(b"\x89PNG")
    with (ws / ".git" / "info" / "exclude").open("a") as f:
        f.write("/.clord/\n")

    status = worktree_status(str(ws))
    assert status.clean, status

    assert _sweep(ws, tmp_path) is DirOutcome.REMOVED


def test_wholly_ignored_claude_dir_with_only_the_skill_is_removed(tmp_path: Path) -> None:
    """本番 18 件: リポジトリが ``.claude/`` ごと無視していて、中は c-lord の SKILL.md だけ。"""
    ws = _session_clone(tmp_path)
    with (ws / ".git" / "info" / "exclude").open("a") as f:
        f.write(".claude/\n")
    inject_read_skill(ws)

    assert _sweep(ws, tmp_path) is DirOutcome.REMOVED


def test_wholly_ignored_claude_dir_with_a_user_file_is_kept(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)
    with (ws / ".git" / "info" / "exclude").open("a") as f:
        f.write(".claude/\n")
    inject_read_skill(ws)
    (ws / ".claude" / "skills" / "mine").mkdir(parents=True)
    (ws / ".claude" / "skills" / "mine" / "SKILL.md").write_text("my own skill\n")

    assert _sweep(ws, tmp_path) is DirOutcome.KEPT_DIRTY


# ── AC5: 孤児スイープと /workspace-cleanup も同じ判定 ──────────────────────────


def test_orphan_sweep_and_workspace_cleanup_share_the_rule(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)
    _git(ws, "checkout", "-q", "-b", "feature/wip")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "wip")

    assert _is_clean(str(ws)) is False  # /workspace-cleanup dry
    assert remove_orphan_dir(ws) is DirOutcome.KEPT_DIRTY
    assert ws.exists()
