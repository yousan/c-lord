"""#1198 — commits in a submodule with no checkout keep the workspace.

After ``git submodule deinit`` or ``git rm`` the submodule's folder has no
``.git``, but its git dir ``<ws>/.git/modules/<name>`` stays — and with it any
commit made there that was never pushed. #1181 started looking at those git
dirs, but only for stashes: a work branch with a commit on it was judged clean,
and /workspace-delete removed the only copy of that commit.

The W57 review showed the same loss with the submodule still checked out: move
its HEAD back to the recorded commit (the branch keeps the commit), or record
the commit in the parent and push — the parent's ``git status`` is empty either
way. And a ``git add``-ed repo with no remote holds its commits nowhere else.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from c_lord import session_dir as sd
from c_lord.session_dir import describe_changes, worktree_status

from .test_submodule_stash_1121 import _delete, _with_submodule
from .test_workspace_cleanup_reasons import _git, _session_clone


def _commit_on_work_branch(sub: Path) -> None:
    _git(sub, "checkout", "-q", "-b", "work")
    (sub / "new.txt").write_text("only here\n")
    _git(sub, "add", "new.txt")
    _git(sub, "commit", "-qm", "unpushed work")


def test_deinit_submodule_with_unpushed_commit_is_kept(tmp_path: Path) -> None:
    ws = _with_submodule(tmp_path)
    _commit_on_work_branch(ws / "sub")
    _git(ws, "submodule", "deinit", "-q", "-f", "sub")
    assert not (ws / "sub" / ".git").exists()
    st = worktree_status(str(ws))
    assert not st.clean, f"clean={st.clean} why={describe_changes(st)!r}"
    assert "sub: 1 unpushed commit(s)" in describe_changes(st)
    r = _delete(ws)
    assert ws.exists(), f"removed={r.removed} reason={r.reason!r}"
    assert "sub: 1 unpushed commit(s)" in r.reason


def test_rm_submodule_with_unpushed_commit_is_kept(tmp_path: Path) -> None:
    ws = _with_submodule(tmp_path)
    _commit_on_work_branch(ws / "sub")
    _git(ws, "rm", "-q", "-f", "sub")
    _git(ws, "commit", "-qm", "drop sub")
    _git(ws, "push", "-q", "origin", "main")
    st = worktree_status(str(ws))
    assert not st.clean, f"clean={st.clean} why={describe_changes(st)!r}"
    r = _delete(ws)
    assert ws.exists(), f"removed={r.removed} reason={r.reason!r}"
    assert "sub: 1 unpushed commit(s)" in r.reason


def test_deinit_submodule_with_detached_unpushed_commit_is_kept(tmp_path: Path) -> None:
    """A commit on a detached HEAD is on no branch at all — still work."""
    ws = _with_submodule(tmp_path)
    sub = ws / "sub"
    (sub / "new.txt").write_text("detached\n")
    _git(sub, "add", "new.txt")
    _git(sub, "commit", "-qm", "detached work")
    _git(ws, "submodule", "deinit", "-q", "-f", "sub")
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)
    assert "sub: 1 unpushed commit(s)" in describe_changes(st)


def test_deinit_submodule_with_pushed_branch_is_clean(tmp_path: Path) -> None:
    """A local branch whose commits are on a remote-tracking ref is nothing to lose."""
    ws = _with_submodule(tmp_path)
    _git(ws / "sub", "checkout", "-q", "-b", "work")  # same commit as origin/main
    _git(ws, "submodule", "deinit", "-q", "-f", "sub")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)


def test_git_failure_in_module_dir_counts_as_work(tmp_path: Path, monkeypatch) -> None:
    """When git cannot count the module's commits, the workspace is kept."""
    ws = _with_submodule(tmp_path)
    _git(ws, "submodule", "deinit", "-q", "-f", "sub")
    real = sd._run

    def fake(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if any(a.startswith("--git-dir=") for a in cmd) and "rev-list" in cmd and "--not" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal")
        return real(cmd, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(sd, "_run", fake)
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)
    assert "sub: unpushed commits unknown (git failed)" in describe_changes(st)


# ---- W57 review: the same loss with separate remotes, and while checked out ----
#
# A bare "remote" for the parent and one for the submodule, so "on no remote"
# is checked against the submodule's own remote. (e) and the two after it show
# the premise "a checked-out submodule's new commit shows in the parent's
# status" does not hold: move HEAD back, or record the commit in the parent and
# push, and the parent is clean while the commit exists nowhere else.

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ALLOW_PROTOCOL": "file",
}
ALLOW = ("-c", "protocol.file.allow=always")


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *ALLOW, "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        env=_ENV,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout


def _bare_with_commit(tmp: Path, name: str) -> Path:
    bare = tmp / f"{name}.git"
    git(tmp, "init", "-q", "--bare", str(bare))
    seed = tmp / f"{name}-seed"
    git(tmp, "clone", "-q", str(bare), str(seed))
    (seed / "README.md").write_text(f"{name}\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-qm", "init")
    git(seed, "push", "-q", "origin", "HEAD:main")
    return bare


def _workspace(tmp: Path) -> tuple[Path, Path]:
    parent = _bare_with_commit(tmp, "parent")
    subr = _bare_with_commit(tmp, "subremote")
    base = tmp / "sessions"
    base.mkdir()
    ws = base / "4242"
    git(tmp, "clone", "-q", "--single-branch", str(parent), str(ws))
    git(ws, "submodule", "add", "-q", str(subr), "sub")
    git(ws, "commit", "-qm", "add sub")
    git(ws, "push", "-q", "origin", "HEAD:main")
    return ws, subr


def _commit_in_sub(ws: Path, *, branch: str | None = "work") -> str:
    sub = ws / "sub"
    if branch:
        git(sub, "checkout", "-q", "-b", branch)
    else:
        git(sub, "checkout", "-q", "--detach")
    (sub / "work.txt").write_text("only copy of this work\n")
    git(sub, "add", "work.txt")
    git(sub, "commit", "-qm", "unpushed work")
    return git(sub, "rev-parse", "HEAD").strip()


def _object_exists(repo: Path, sha: str) -> bool:
    return (
        subprocess.run(
            [
                "git",
                f"--git-dir={repo}",
                f"--work-tree={repo}",
                "cat-file",
                "-e",
                f"{sha}^{{commit}}",
            ],
            capture_output=True,
            env=_ENV,
        ).returncode
        == 0
    )


def _assert_kept(ws: Path, subr: Path, sha: str) -> None:
    modules = ws / ".git" / "modules" / "sub"
    assert _object_exists(modules, sha), "setup: commit should be in .git/modules/sub"
    assert not _object_exists(subr, sha), "setup: commit must not be on the sub remote"
    st = worktree_status(str(ws))
    why = describe_changes(st)
    res = _delete(ws)
    gone = not ws.exists()
    assert not res.removed, (
        f"workspace deleted although .git/modules/sub held the only copy of {sha[:8]}: "
        f"clean={st.clean} why={why!r} reason={res.reason!r} ws_gone={gone} "
        f"modules_gone={not modules.exists()} on_remote={_object_exists(subr, sha)}"
    )
    assert ws.exists() and _object_exists(modules, sha)


def test_a_deinit_after_unpushed_branch_commit_keeps(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    sha = _commit_in_sub(ws, branch="work")
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    _assert_kept(ws, subr, sha)


def test_b_git_rm_committed_after_unpushed_commit_keeps(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    sha = _commit_in_sub(ws, branch="work")
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    git(ws, "rm", "-q", "sub")
    git(ws, "commit", "-qm", "drop sub")
    git(ws, "push", "-q", "origin", "HEAD:main")
    _assert_kept(ws, subr, sha)


def test_b2_git_rm_without_deinit_keeps(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    sha = _commit_in_sub(ws, branch="work")
    git(ws, "rm", "-q", "-f", "sub")
    git(ws, "commit", "-qm", "drop sub")
    git(ws, "push", "-q", "origin", "HEAD:main")
    _assert_kept(ws, subr, sha)


def test_c_detached_commit_only_in_module_git_dir_keeps(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    sha = _commit_in_sub(ws, branch=None)  # on no branch: only .git/modules/sub/HEAD holds it
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    _assert_kept(ws, subr, sha)


def test_d_control_pushed_commit_may_be_deleted(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    sha = _commit_in_sub(ws, branch="work")
    git(ws / "sub", "push", "-q", "origin", "work")
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    assert _object_exists(subr, sha)
    res = _delete(ws)
    assert res.removed, res.reason
    assert not ws.exists()


def test_e_checked_out_submodule_branch_moved_back_keeps(tmp_path: Path) -> None:
    """Checked-out submodule, but HEAD back on the recorded commit: the parent's
    status shows nothing, yet branch ``work`` in .git/modules/sub has the only copy."""
    ws, subr = _workspace(tmp_path)
    recorded = git(ws / "sub", "rev-parse", "HEAD").strip()
    sha = _commit_in_sub(ws, branch="work")
    git(ws / "sub", "checkout", "-q", recorded)
    assert git(ws, "status", "--porcelain").strip() == ""
    _assert_kept(ws, subr, sha)
    assert "sub: 1 unpushed commit(s)" in describe_changes(worktree_status(str(ws)))


def test_checked_out_submodule_unpushed_commit_recorded_by_parent_is_kept(
    tmp_path: Path,
) -> None:
    """The parent recorded the submodule's new commit and pushed: the parent is
    clean, but the submodule's commit is on none of the submodule's remotes."""
    ws, subr = _workspace(tmp_path)
    sha = _commit_in_sub(ws, branch=None)
    git(ws, "add", "sub")
    git(ws, "commit", "-qm", "bump sub")
    git(ws, "push", "-q", "origin", "HEAD:main")
    assert git(ws, "status", "--porcelain").strip() == ""
    _assert_kept(ws, subr, sha)


def test_checked_out_submodule_on_its_remote_is_clean(tmp_path: Path) -> None:
    """Control: a checked-out submodule whose commits are all on its remote."""
    ws, _ = _workspace(tmp_path)
    git(ws / "sub", "checkout", "-q", "-b", "work")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)


def _local_repo(path: Path, name: str = "f.txt") -> Path:
    """A repo with one commit that exists nowhere else (no remote)."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / name).write_text("only copy\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "local only")
    return path


def test_embedded_repo_local_only_commits_are_kept(tmp_path: Path) -> None:
    """A ``git add``-ed repo (no .gitmodules, no remote): its commit exists only here."""
    ws = _session_clone(tmp_path)
    _local_repo(ws / "emb")
    _git(ws, "add", "emb")
    _git(ws, "commit", "-qm", "embed emb")
    _git(ws, "push", "-q", "origin", "main")
    st = worktree_status(str(ws))
    r = _delete(ws)
    assert ws.exists(), f"removed={r.removed} reason={r.reason!r} status={describe_changes(st)!r}"
    assert "emb: 1 unpushed commit(s)" in r.reason


def test_deinit_submodule_pinned_to_a_tag_on_no_branch_is_clean(tmp_path: Path) -> None:
    """A submodule pinned to a release tag whose commit is on no branch: the tag
    is on the remote, so nothing is lost — not kept forever as "unpushed"."""
    ws, subr = _workspace(tmp_path)
    seed = tmp_path / "subremote-seed"
    git(seed, "checkout", "-q", "--detach")
    (seed / "rel.txt").write_text("release\n")
    git(seed, "add", "rel.txt")
    git(seed, "commit", "-qm", "release")
    git(seed, "tag", "v1")
    git(seed, "push", "-q", "origin", "v1")
    sub = ws / "sub"
    git(sub, "fetch", "-q", "--tags", "origin")
    git(sub, "checkout", "-q", "v1")
    assert not git(sub, "branch", "-r", "--contains", "v1").strip()  # on no branch
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
    assert _delete(ws).removed
