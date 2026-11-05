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
import time
from pathlib import Path

import pytest

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


def test_deinit_submodule_pinned_to_a_tag_on_no_branch_is_kept(tmp_path: Path) -> None:
    """A submodule pinned to a release tag whose commit is on no branch is kept.

    Accepted over-keep: tags never count as pushed. The tag lives in the same
    ``.git/modules/sub`` that deleting removes, and a fetched tag cannot be told
    from a hand-made ``git tag wip`` (which lost the only copy of a commit)."""
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
    assert "sub: 1 unpushed commit(s)" in describe_changes(st)
    assert not _delete(ws).removed


# ---- review of 7890ea3 and 2e59e3b: tags never count as pushed ----


def test_tagged_commit_in_repo_with_no_remote_is_kept(tmp_path: Path) -> None:
    """A tag in a repo with no remote is no copy anywhere else: every commit counts."""
    ws = _session_clone(tmp_path)
    _local_repo(ws / "emb")
    _git(ws / "emb", "tag", "v1")
    _git(ws, "add", "emb")
    _git(ws, "commit", "-qm", "embed emb")
    _git(ws, "push", "-q", "origin", "main")
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)
    assert "emb: 1 unpushed commit(s)" in describe_changes(st)


def test_local_tag_does_not_hide_unpushed_branch_commits(tmp_path: Path) -> None:
    """A local-only tag on a work branch: ``git submodule update`` puts HEAD back on
    the recorded commit, and the branch's commit is still on no remote."""
    ws, subr = _workspace(tmp_path)
    sha = _commit_in_sub(ws, branch="work")
    git(ws / "sub", "tag", "wip")
    git(ws, "submodule", "update", "-q")
    assert git(ws, "status", "--porcelain").strip() == ""
    _assert_kept(ws, subr, sha)
    assert "sub: 1 unpushed commit(s)" in describe_changes(worktree_status(str(ws)))


def _embedded_with_submodule(tmp_path: Path) -> tuple[Path, Path]:
    """``ws/emb``: a clone (own remote) ``git add``-ed and pushed, with submodule ``inner``."""
    ws = _session_clone(tmp_path)
    emb_bare = _bare_with_commit(tmp_path, "emb")
    inner_bare = _bare_with_commit(tmp_path, "inner")
    emb = ws / "emb"
    git(tmp_path, "clone", "-q", str(emb_bare), str(emb))
    git(emb, "submodule", "add", "-q", str(inner_bare), "inner")
    git(emb, "commit", "-qm", "inner")
    git(emb, "push", "-q", "origin", "HEAD:main")
    git(ws, "add", "emb")
    git(ws, "commit", "-qm", "emb")
    git(ws, "push", "-q", "origin", "main")
    return ws, emb


def test_deinit_submodule_inside_embedded_repo_unpushed_commit_is_kept(tmp_path: Path) -> None:
    """The embedded repo's own ``.git/modules`` holds the deinit'd submodule's commit."""
    ws, emb = _embedded_with_submodule(tmp_path)
    assert worktree_status(str(ws)).clean
    _commit_on_work_branch(emb / "inner")
    git(emb, "submodule", "deinit", "-q", "-f", "inner")
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)
    assert "emb/inner: 1 unpushed commit(s)" in describe_changes(st)


def test_deinit_submodule_inside_embedded_repo_stash_is_kept(tmp_path: Path) -> None:
    ws, emb = _embedded_with_submodule(tmp_path)
    (emb / "inner" / "README.md").write_text("wip\n")
    git(emb / "inner", "stash", "-q")
    git(emb, "submodule", "deinit", "-q", "-f", "inner")
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)
    assert "emb/inner: 1 stash entry" in describe_changes(st)


def _detached_commit_with_local_tag(ws: Path) -> str:
    sha = _commit_in_sub(ws, branch=None)
    git(ws / "sub", "tag", "wip")  # made here: the tag is no copy anywhere else
    return sha


def test_local_tag_on_detached_commit_recorded_by_parent_is_kept(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    sha = _detached_commit_with_local_tag(ws)
    git(ws, "add", "sub")
    git(ws, "commit", "-qm", "bump sub")
    git(ws, "push", "-q", "origin", "HEAD:main")
    assert git(ws, "status", "--porcelain").strip() == ""
    _assert_kept(ws, subr, sha)


def test_local_tag_on_detached_commit_then_deinit_is_kept(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    sha = _detached_commit_with_local_tag(ws)
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    _assert_kept(ws, subr, sha)


def test_detached_commit_in_linked_worktree_of_submodule_is_kept(tmp_path: Path) -> None:
    """``git worktree add --detach`` elsewhere: its HEAD is only in
    ``<gitdir>/worktrees/*/HEAD``, and its commit only in .git/modules/sub."""
    ws, subr = _workspace(tmp_path)
    wt = tmp_path / "elsewhere" / "wt"
    git(ws / "sub", "worktree", "add", "-q", "--detach", str(wt))
    (wt / "wt.txt").write_text("only in the worktree\n")
    git(wt, "add", "wt.txt")
    git(wt, "commit", "-qm", "worktree work")
    sha = git(wt, "rev-parse", "HEAD").strip()
    assert git(ws, "status", "--porcelain").strip() == ""
    _assert_kept(ws, subr, sha)
    assert "sub: 1 unpushed commit(s)" in describe_changes(worktree_status(str(ws)))


# ---- review of 7f2dd2b: every local ref, per-worktree refs, unreadable = unknown ----


def _wt_detached_commit(ws: Path, tmp: Path, repo: Path | None = None) -> tuple[Path, str]:
    wt = tmp / "elsewhere" / "wt"
    git(repo or ws / "sub", "worktree", "add", "-q", "--detach", str(wt))
    (wt / "wt.txt").write_text("x\n")
    git(wt, "add", "wt.txt")
    git(wt, "commit", "-qm", "wt work")
    return wt, git(wt, "rev-parse", "HEAD").strip()


def _wt_meta(ws: Path) -> Path:
    (d,) = list((ws / ".git" / "modules" / "sub" / "worktrees").iterdir())
    return d


@pytest.mark.parametrize("ref", ["refs/worktree/keep", "refs/bisect/bad"])
def test_per_worktree_ref_in_linked_worktree_is_kept(tmp_path: Path, ref: str) -> None:
    """A commit held only by a per-worktree ref (``<gitdir>/worktrees/*/refs``):
    ``rev-list --all`` does not see other worktrees' refs on git 2.43."""
    ws, subr = _workspace(tmp_path)
    wt, sha = _wt_detached_commit(ws, tmp_path)
    git(wt, "update-ref", ref, sha)
    git(wt, "checkout", "-q", "--detach", "HEAD~1")
    _assert_kept(ws, subr, sha)


def test_custom_ref_in_submodule_is_kept(tmp_path: Path) -> None:
    """``refs/keep/*`` (or notes, ``refs/original``): no branch, but the commit is work."""
    ws, subr = _workspace(tmp_path)
    recorded = git(ws / "sub", "rev-parse", "HEAD").strip()
    sha = _commit_in_sub(ws, branch=None)
    git(ws / "sub", "update-ref", "refs/keep/x", sha)
    git(ws / "sub", "checkout", "-q", recorded)
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    _assert_kept(ws, subr, sha)


def test_garbage_worktree_head_is_unknown(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    _, sha = _wt_detached_commit(ws, tmp_path)
    (_wt_meta(ws) / "HEAD").write_text("garbage\n")
    _assert_kept(ws, subr, sha)
    assert "sub: unpushed commits unknown" in describe_changes(worktree_status(str(ws)))


def test_crlf_worktree_head_is_read(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    _, sha = _wt_detached_commit(ws, tmp_path)
    (_wt_meta(ws) / "HEAD").write_text(sha + "\r\n")
    _assert_kept(ws, subr, sha)


def test_unreadable_worktree_head_is_unknown(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    _, sha = _wt_detached_commit(ws, tmp_path)
    head = _wt_meta(ws) / "HEAD"
    head.chmod(0)
    try:
        _assert_kept(ws, subr, sha)
        why = describe_changes(worktree_status(str(ws)))
        assert "sub: unpushed commits unknown (worktree HEAD unreadable)" in why
    finally:
        if head.exists():
            head.chmod(0o644)


def test_unlistable_worktrees_dir_is_unknown(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _wt_detached_commit(ws, tmp_path)
    d = ws / ".git" / "modules" / "sub" / "worktrees"
    d.chmod(0o300)  # writable+exec but not listable
    try:
        why = describe_changes(worktree_status(str(ws)))
        assert "sub: unpushed commits unknown (worktree HEAD unreadable)" in why
        assert not _delete(ws).removed
    finally:
        if d.exists():
            d.chmod(0o755)


def test_workspace_own_detached_linked_worktree_is_kept(tmp_path: Path) -> None:
    """The workspace's own ``git worktree add --detach``: its commit is only in ws/.git."""
    ws, _ = _workspace(tmp_path)
    _, sha = _wt_detached_commit(ws, tmp_path, repo=ws)
    st = worktree_status(str(ws))
    r = _delete(ws)
    assert ws.exists(), f"removed={r.removed} reason={r.reason!r} why={describe_changes(st)!r}"
    assert "1 unpushed commit(s)" in r.reason


# Controls: nothing to lose stays deletable.


def test_pushed_submodule_with_detached_worktree_is_clean(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    git(ws / "sub", "worktree", "add", "-q", "--detach", str(tmp_path / "elsewhere" / "wt"))
    assert worktree_status(str(ws)).clean, describe_changes(worktree_status(str(ws)))
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    assert _delete(ws).removed


def test_worktree_head_naming_a_missing_object_is_kept(tmp_path: Path) -> None:
    """A dangling worktree HEAD makes the count unknown — kept (accepted over-keep).

    Ignoring missing objects also silenced a branch whose tip object was gone,
    and deleted the commits below it (review of e6fcc04)."""
    ws, _ = _workspace(tmp_path)
    git(ws / "sub", "worktree", "add", "-q", "--detach", str(tmp_path / "elsewhere" / "wt"))
    (_wt_meta(ws) / "HEAD").write_text("1" * 40 + "\n")
    st = worktree_status(str(ws))
    assert "sub: unpushed commits unknown (git failed)" in describe_changes(st)


def test_fetched_tag_on_a_pushed_commit_is_clean(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    seed = tmp_path / "subremote-seed"
    git(seed, "tag", "v0")
    git(seed, "push", "-q", "origin", "v0")
    git(ws / "sub", "fetch", "-q", "--tags")
    git(ws / "sub", "checkout", "-q", "v0")
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    assert _delete(ws).removed, describe_changes(worktree_status(str(ws)))


def test_corrupt_module_git_dir_is_kept(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    (ws / ".git" / "modules" / "sub" / "HEAD").write_text("garbage\n")
    assert not _delete(ws).removed


# ---- review of e6fcc04: symlinked ref dirs, missing objects, many refs, no hang ----


def test_ref_under_symlinked_dir_in_worktree_refs_is_kept(tmp_path: Path) -> None:
    """git follows a symlinked directory in ``worktrees/<n>/refs/``; so must we."""
    ws, subr = _workspace(tmp_path)
    wt, sha = _wt_detached_commit(ws, tmp_path)
    ext = tmp_path / "extrefs"
    ext.mkdir()
    (ext / "keep").write_text(sha + "\n")
    refs = _wt_meta(ws) / "refs" / "worktree"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "ext").symlink_to(ext, target_is_directory=True)
    (ext / "loop").symlink_to(refs, target_is_directory=True)  # a cycle must not hang
    assert git(wt, "rev-parse", "refs/worktree/ext/keep").strip() == sha
    git(wt, "checkout", "-q", "--detach", "HEAD~1")
    # counted through the link — not merely "unknown"
    assert "sub: 1 unpushed commit(s)" in describe_changes(worktree_status(str(ws)))
    _assert_kept(ws, subr, sha)


def _drop_object(git_dir: Path, sha: str) -> None:
    (git_dir / "objects" / sha[:2] / sha[2:]).unlink()


def test_branch_whose_tip_object_is_missing_is_kept(tmp_path: Path) -> None:
    """``work`` = c1 → c2 with c2's object gone: c1 is still the only copy."""
    ws, subr = _workspace(tmp_path)
    c1 = _commit_in_sub(ws, branch="work")
    sub = ws / "sub"
    (sub / "c2.txt").write_text("c2\n")
    git(sub, "add", "c2.txt")
    git(sub, "commit", "-qm", "c2")
    c2 = git(sub, "rev-parse", "HEAD").strip()
    git(sub, "checkout", "-q", "--detach", "origin/main")
    _drop_object(ws / ".git" / "modules" / "sub", c2)
    _assert_kept(ws, subr, c1)


def _many_commits(repo: Path, n: int) -> list[str]:
    """*n* distinct commits that no ref holds, made fast with ``git fast-import``."""
    lines = []
    for i in range(1, n + 1):
        lines.append(
            f"commit refs/heads/tmp-many\nmark :{i}\ncommitter t <t@e> {i} +0000\ndata 0\n"
        )
        if i > 1:
            lines.append(f"from :{i - 1}\n")
        lines.append("\n")
    subprocess.run(
        ["git", "fast-import", "--quiet"], cwd=repo, input="".join(lines), text=True, check=True
    )
    shas = git(repo, "rev-list", "refs/heads/tmp-many").split()
    git(repo, "update-ref", "-d", "refs/heads/tmp-many")
    return shas


def _write_refs(meta: Path, shas: list[str]) -> None:
    many = meta / "refs" / "worktree" / "many"
    many.mkdir(parents=True)
    for i, sha in enumerate(shas):
        (many / f"r{i}").write_text(sha + "\n")


_MANY = 60_000  # 60,000 x 41 bytes is past the kernel's argument limit


def test_many_worktree_refs_do_not_break_the_count(tmp_path: Path) -> None:
    """60,000 per-worktree refs: the tips go through ``--stdin``, not argv
    (``OSError: Argument list too long`` stopped the sweep for every dir)."""
    ws, subr = _workspace(tmp_path)
    wt, sha = _wt_detached_commit(ws, tmp_path)
    _write_refs(_wt_meta(ws), _many_commits(wt, _MANY))
    _assert_kept(ws, subr, sha)


def test_many_refs_in_the_workspace_own_worktree_do_not_break_the_count(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    wt, _ = _wt_detached_commit(ws, tmp_path, repo=ws)
    (meta,) = list((ws / ".git" / "worktrees").iterdir())
    _write_refs(meta, _many_commits(wt, _MANY))
    st = worktree_status(str(ws))
    assert not st.clean
    assert st.unpushed_commits is not None and st.unpushed_commits > _MANY, describe_changes(st)


def test_git_calls_have_a_timeout_and_never_lazy_fetch(monkeypatch) -> None:
    """A partial clone's missing object must not start a network fetch, no
    inspection call may hang the sweep forever, and none takes optional locks."""
    seen: dict[str, object] = {}
    real = subprocess.Popen

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, args, **kw):  # noqa: ANN001, ANN003
            seen.update(kw)
            super().__init__(args, **kw)

        def communicate(self, input=None, timeout=None):  # noqa: A002, ANN001, ANN201
            seen["timeout"] = timeout
            return super().communicate(input, timeout)

    monkeypatch.setattr(sd.subprocess, "Popen", Spy)
    sd._run(["git", "--version"], cwd="/")
    env = seen["env"]
    assert env["GIT_NO_LAZY_FETCH"] == "1"  # type: ignore[index]
    assert env["GIT_OPTIONAL_LOCKS"] == "0"  # type: ignore[index]
    assert seen["start_new_session"] is True
    assert seen["timeout"] and float(seen["timeout"]) <= 120  # type: ignore[arg-type]


def _time_out(monkeypatch, match) -> None:  # noqa: ANN001
    """Make the git calls *match* selects time out (a real process, killed by group)."""
    real = subprocess.Popen

    class Slow(real):  # type: ignore[misc, valid-type]
        def communicate(self, input=None, timeout=None):  # noqa: A002, ANN001, ANN201
            if timeout is not None and timeout > 5 and match(self.args):
                raise subprocess.TimeoutExpired(self.args, timeout)
            return super().communicate(input, timeout)

    monkeypatch.setattr(sd.subprocess, "Popen", Slow)


def test_a_timed_out_stash_check_is_not_no_stash(tmp_path: Path, monkeypatch) -> None:
    """Only ``rev-parse refs/stash`` in the submodule times out; it has a stash."""
    ws, _ = _workspace(tmp_path)
    (ws / "sub" / "README.md").write_text("wip\n")
    git(ws / "sub", "stash", "-q")
    _time_out(monkeypatch, lambda a: "refs/stash" in a and "rev-parse" in a)
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)


def test_a_timed_out_branch_listing_is_not_no_branches(tmp_path: Path, monkeypatch) -> None:
    """Only the workspace's ``for-each-ref`` times out; a branch has a commit."""
    ws, _ = _workspace(tmp_path)
    git(ws, "checkout", "-q", "-b", "work")
    (ws / "w.txt").write_text("w\n")
    git(ws, "add", "w.txt")
    git(ws, "commit", "-qm", "work")
    git(ws, "checkout", "-q", "main")
    _time_out(monkeypatch, lambda a: "for-each-ref" in a)
    st = worktree_status(str(ws))
    assert not st.clean and st.unpushed_commits is None, describe_changes(st)


def test_a_timed_out_ls_files_is_not_no_submodules(tmp_path: Path, monkeypatch) -> None:
    """Only the workspace's ``ls-files`` times out; an embedded repo holds the only copy."""
    ws = _session_clone(tmp_path)
    _local_repo(ws / "emb")
    _git(ws, "add", "emb")
    _git(ws, "commit", "-qm", "embed emb")
    _git(ws, "push", "-q", "origin", "main")
    _time_out(monkeypatch, lambda a: "ls-files" in a)
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)


# ---- W57 round 8 and review of f30cacc ----


def _commit(repo: Path, name: str, text: str = "x\n") -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD").strip()


def _kept(ws: Path) -> sd.CleanupResult:
    st = worktree_status(str(ws))
    res = _delete(ws)
    assert not res.removed and ws.exists(), (
        f"deleted: clean={st.clean} why={describe_changes(st)!r} reason={res.reason!r}"
    )
    return res


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timed_out_status_leaves_no_index_lock_and_no_orphans(tmp_path: Path, monkeypatch) -> None:
    """``git status`` holds ``index.lock`` while it runs ``git status`` in a
    submodule; killed at the timeout, the lock stayed and the next ``git
    commit`` in the kept workspace failed — and the submodule's status and its
    filter kept running."""
    ws, _ = _workspace(tmp_path)
    sub = ws / "sub"
    pidfile = tmp_path / "filter.pid"
    git(sub, "config", "filter.slow.clean", f"echo $$ > {pidfile}; sleep 20; cat")
    info = ws / ".git" / "modules" / "sub" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* filter=slow\n")
    os.utime(sub / "README.md", (2_000_000_000, 2_000_000_000))  # same size, new mtime
    monkeypatch.setattr(sd._run, "__kwdefaults__", {**sd._run.__kwdefaults__, "timeout": 2.0})
    try:
        st = worktree_status(str(ws))
        assert not (ws / ".git" / "index.lock").exists(), "index.lock left behind"
        assert pidfile.exists(), "setup: the slow filter should have started"
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_alive(pid), "the submodule's filter outlived the timeout"
        assert not st.clean
        assert "git timed out" in describe_changes(st), describe_changes(st)
    finally:
        if pidfile.exists() and _pid_alive(int(pidfile.read_text())):
            os.kill(int(pidfile.read_text()), 9)


@pytest.mark.xfail(
    strict=True,
    reason="known limit: the workspace's own tags are not counted — a fetched tag cannot "
    "be told from a hand-made one, and counting them kept every workspace that fetched "
    "tags (shallow clone + fetch --tags, a tag on a deleted branch). Spec.",
)
def test_workspace_commit_held_only_by_a_local_tag_is_kept(tmp_path: Path) -> None:
    """Detached commit → ``git tag wip`` → ``checkout main``: only the tag holds it."""
    ws, _ = _workspace(tmp_path)
    git(ws, "checkout", "-q", "--detach")
    _commit(ws, "w.txt")
    git(ws, "tag", "wip")
    git(ws, "checkout", "-q", "main")
    res = _kept(ws)
    assert "1 unpushed commit(s)" in res.reason


@pytest.mark.parametrize("ref", ["refs/keep/wip", "refs/original/refs/heads/main"])
def test_workspace_commit_held_only_by_a_keep_ref_or_original_is_kept(
    tmp_path: Path, ref: str
) -> None:
    ws, _ = _workspace(tmp_path)
    git(ws, "checkout", "-q", "--detach")
    sha = _commit(ws, "w.txt")
    git(ws, "update-ref", ref, sha)
    git(ws, "checkout", "-q", "main")
    _kept(ws)


def test_workspace_pushed_with_fetched_tag_is_clean(tmp_path: Path) -> None:
    """Control: a tag on a pushed commit adds nothing."""
    ws, _ = _workspace(tmp_path)
    git(ws, "tag", "v0")
    assert worktree_status(str(ws)).clean


def test_depth4_embedded_repo_under_three_submodules(tmp_path: Path) -> None:
    """a/b/c are pushed submodules; c embeds d (gitlink, no .gitmodules) whose
    only commit is local. Past the depth limit the search must say "unknown"."""
    parent = _bare_with_commit(tmp_path, "parent")
    cr = _bare_with_commit(tmp_path, "cr")
    cseed = tmp_path / "cr-seed"
    d = cseed / "d"
    d.mkdir()
    git(d, "init", "-q")
    _commit(d, "d.txt")
    git(cseed, "add", "d")
    git(cseed, "commit", "-qm", "embed d")
    git(cseed, "push", "-q", "origin", "HEAD:main")
    br = _bare_with_commit(tmp_path, "br")
    git(tmp_path / "br-seed", "submodule", "add", "-q", str(cr), "c")
    git(tmp_path / "br-seed", "commit", "-qm", "c")
    git(tmp_path / "br-seed", "push", "-q", "origin", "HEAD:main")
    ar = _bare_with_commit(tmp_path, "ar")
    git(tmp_path / "ar-seed", "submodule", "add", "-q", str(br), "b")
    git(tmp_path / "ar-seed", "commit", "-qm", "b")
    git(tmp_path / "ar-seed", "push", "-q", "origin", "HEAD:main")
    base = tmp_path / "sessions"
    base.mkdir()
    ws = base / "4242"
    git(tmp_path, "clone", "-q", str(parent), str(ws))
    git(ws, "submodule", "add", "-q", str(ar), "a")
    git(ws / "a", "submodule", "update", "-q", "--init")
    git(ws / "a" / "b", "submodule", "update", "-q", "--init")
    git(ws, "commit", "-qm", "a")
    git(ws, "push", "-q", "origin", "HEAD:main")
    dd = ws / "a" / "b" / "c" / "d"
    if not (dd / ".git").exists():
        subprocess.run(["rm", "-rf", str(dd)], check=True)
        git(tmp_path, "clone", "-q", str(d), str(dd))
        git(dd, "remote", "remove", "origin")
    _commit(dd, "only-here.txt")
    git(ws / "a" / "b" / "c", "add", "d")
    git(ws / "a" / "b" / "c", "commit", "-qm", "bump d")
    git(ws / "a" / "b" / "c", "push", "-q", "origin", "HEAD:refs/heads/main")
    git(ws / "a" / "b", "add", "c")
    git(ws / "a" / "b", "commit", "-qm", "bump c")
    git(ws / "a" / "b", "push", "-q", "origin", "HEAD:refs/heads/main")
    git(ws / "a", "add", "b")
    git(ws / "a", "commit", "-qm", "bump b")
    git(ws / "a", "push", "-q", "origin", "HEAD:refs/heads/main")
    git(ws, "add", "a")
    git(ws, "commit", "-qm", "bump a")
    git(ws, "push", "-q", "origin", "HEAD:main")
    assert git(ws, "status", "--porcelain").strip() == ""
    res = _kept(ws)
    assert "a/b/c: nested repos too deep to check" in res.reason


def test_depth3_embedded_repo_control(tmp_path: Path) -> None:
    """Same as above one level shallower: a/b pushed submodules, b embeds d."""
    parent = _bare_with_commit(tmp_path, "parent")
    br = _bare_with_commit(tmp_path, "br")
    bseed = tmp_path / "br-seed"
    d = bseed / "d"
    d.mkdir()
    git(d, "init", "-q")
    _commit(d, "d.txt")
    git(bseed, "add", "d")
    git(bseed, "commit", "-qm", "embed d")
    git(bseed, "push", "-q", "origin", "HEAD:main")
    ar = _bare_with_commit(tmp_path, "ar")
    git(tmp_path / "ar-seed", "submodule", "add", "-q", str(br), "b")
    git(tmp_path / "ar-seed", "commit", "-qm", "b")
    git(tmp_path / "ar-seed", "push", "-q", "origin", "HEAD:main")
    base = tmp_path / "sessions"
    base.mkdir()
    ws = base / "4242"
    git(tmp_path, "clone", "-q", str(parent), str(ws))
    git(ws, "submodule", "add", "-q", str(ar), "a")
    git(ws / "a", "submodule", "update", "-q", "--init")
    git(ws, "commit", "-qm", "a")
    git(ws, "push", "-q", "origin", "HEAD:main")
    dd = ws / "a" / "b" / "d"
    subprocess.run(["rm", "-rf", str(dd)], check=True)
    git(tmp_path, "clone", "-q", str(d), str(dd))
    git(dd, "remote", "remove", "origin")
    assert git(ws, "status", "--porcelain").strip() == ""
    _kept(ws)


def test_module_dirs_past_the_depth_limit_are_unknown(tmp_path: Path) -> None:
    """The ``.git/modules`` walk stops at its bound too — and says so."""
    deep = tmp_path / "m"
    (deep / "x").mkdir(parents=True)
    notes = sd._module_dir_stashes(deep, prefix="p/", depth=sd._MAX_SUBMODULE_DEPTH * 4, seen=set())
    assert notes and "too deep" in notes[0]


def test_upstream_tag_on_no_branch_is_kept_known_over_keep(tmp_path: Path) -> None:
    """Known over-keep (documented): the submodule's upstream tags a commit no
    branch holds (a deleted release branch). Nothing local, yet tags never
    count as pushed, so the workspace is kept with a reason naming the module."""
    parent = _bare_with_commit(tmp_path, "parent")
    subr = _bare_with_commit(tmp_path, "subremote")
    seed = tmp_path / "subremote-seed"
    git(seed, "checkout", "-q", "-b", "release")
    _commit(seed, "rel.txt")
    git(seed, "tag", "v1.0")
    git(seed, "push", "-q", "origin", "release", "v1.0")
    git(seed, "push", "-q", "origin", "--delete", "release")
    base = tmp_path / "sessions"
    base.mkdir()
    ws = base / "4242"
    git(tmp_path, "clone", "-q", "--single-branch", str(parent), str(ws))
    git(ws, "submodule", "add", "-q", str(subr), "sub")
    git(ws, "commit", "-qm", "add sub")
    git(ws, "push", "-q", "origin", "HEAD:main")
    assert "v1.0" in git(ws / "sub", "tag")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason
    git(ws / "sub", "tag", "-d", "v1.0")  # how a user clears it (spec)
    assert _delete(ws).removed


@pytest.mark.xfail(
    strict=True,
    reason="known limit: a branch pushed and then deleted on the remote still has a stale "
    "local tracking ref; telling that apart needs the network (pre-existing, spec)",
)
def test_stale_tracking_ref_branch_deleted_on_remote(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    git(ws / "sub", "checkout", "-q", "-b", "work")
    sha = _commit(ws / "sub", "w.txt")
    git(ws / "sub", "push", "-q", "origin", "work")
    git(ws / "sub", "checkout", "-q", "--detach", "HEAD~1")
    git(ws / "sub", "branch", "-q", "-D", "work")
    git(subr, "update-ref", "-d", "refs/heads/work")
    git(subr, "gc", "-q", "--prune=now")
    assert not _object_exists(subr, sha)
    assert git(ws, "status", "--porcelain").strip() == ""
    _kept(ws)


# ---- review of 77df9cc: the workspace's own tags kept pushed workspaces ----


def _origin_with_tags(tmp: Path) -> Path:
    """A bare origin whose ``main`` has 3 commits, the first two tagged, plus a
    tag on a commit of a branch that was deleted since."""
    origin = _bare_with_commit(tmp, "tagged")
    seed = tmp / "tagged-seed"
    git(seed, "tag", "-a", "-m", "t0", "v0")
    _commit(seed, "c1.txt")
    git(seed, "tag", "v1")
    _commit(seed, "c2.txt")
    git(seed, "checkout", "-q", "-b", "release")
    _commit(seed, "rel.txt")
    git(seed, "tag", "-a", "-m", "rel", "rel-1")
    git(seed, "checkout", "-q", "main")
    git(seed, "push", "-q", "origin", "main", "release", "--tags")
    git(seed, "push", "-q", "origin", "--delete", "release")
    git(seed, "branch", "-q", "-D", "release")
    return origin


def test_depth1_clone_after_fetch_tags_is_clean(tmp_path: Path) -> None:
    """A ``--depth=1`` session clone, then ``git fetch --tags``: the tags point
    below the shallow boundary and at a deleted branch. Nothing is unpushed."""
    origin = _origin_with_tags(tmp_path)
    base = tmp_path / "sessions"
    base.mkdir()
    ws = base / "4242"
    git(tmp_path, "clone", "-q", "--depth=1", "--single-branch", f"file://{origin}", str(ws))
    git(ws, "fetch", "-q", "--tags")
    assert "v1" in git(ws, "tag")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
    assert _delete(ws).removed


def test_local_clone_with_a_tag_on_a_deleted_branch_is_clean(tmp_path: Path) -> None:
    """``git clone --local`` (what a local source repo gets) takes every tag."""
    _origin_with_tags(tmp_path)
    src = tmp_path / "tagged-seed"
    git(src, "fetch", "-q", "origin", "--tags")  # rel-1 back into the source
    base = tmp_path / "sessions"
    base.mkdir()
    ws = base / "4242"
    git(tmp_path, "clone", "-q", "--local", str(src), str(ws))
    assert "rel-1" in git(ws, "tag")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
    assert _delete(ws).removed


def test_workspace_ref_to_a_nested_annotated_tag_is_peeled_fully(tmp_path: Path) -> None:
    """A non-tag ref (``refs/keep/x``) to a tag of a tag of an unpushed commit:
    peeling one level saw a tag, not a commit, and dropped the ref."""
    ws, _ = _workspace(tmp_path)
    git(ws, "checkout", "-q", "--detach")
    sha = _commit(ws, "w.txt")
    t1 = subprocess.run(
        ["git", "mktag"],
        cwd=ws,
        input=f"object {sha}\ntype commit\ntag t1\ntagger t <t@e> 0 +0000\n\nt1\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    t2 = subprocess.run(
        ["git", "mktag"],
        cwd=ws,
        input=f"object {t1}\ntype tag\ntag t2\ntagger t <t@e> 0 +0000\n\nt2\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    git(ws, "update-ref", "refs/keep/x", t2)
    git(ws, "checkout", "-q", "main")
    res = _kept(ws)
    assert "1 unpushed commit(s)" in res.reason


def test_a_timed_out_ls_remote_leaves_no_child_running(tmp_path: Path, monkeypatch) -> None:
    """``ls-remote`` runs its transport (ssh, remote-https, upload-pack) as a
    child; a timeout must stop the whole group, not only ``git``."""
    ws, _ = _workspace(tmp_path)
    git(ws, "checkout", "-q", "-b", "feature")
    _commit(ws, "f.txt")  # an unpushed commit, so the remote is asked
    pidfile = tmp_path / "upload.pid"
    fake = tmp_path / "slow-upload-pack"
    fake.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nsleep 20\n")
    fake.chmod(0o755)
    git(ws, "config", "remote.origin.uploadpack", str(fake))
    sd._ls_remote_cache.clear()
    monkeypatch.setattr(sd, "_LS_REMOTE_TIMEOUT_SECONDS", 1.0)
    try:
        st = worktree_status(str(ws))
        assert st.remote_unchecked, describe_changes(st)
        assert pidfile.exists(), "setup: the slow upload-pack should have started"
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_alive(pid), "ls-remote's transport outlived the timeout"
    finally:
        sd._ls_remote_cache.clear()
        if pidfile.exists() and _pid_alive(int(pidfile.read_text())):
            os.kill(int(pidfile.read_text()), 9)


def test_workspace_with_pushed_notes_is_clean(tmp_path: Path) -> None:
    """Review of 8479268: notes pushed to (or fetched from) the remote are on no
    remote branch. Counting them kept the workspace forever."""
    ws, _ = _workspace(tmp_path)
    git(ws, "notes", "add", "-m", "a note")
    git(ws, "push", "-q", "origin", "refs/notes/commits")
    st = worktree_status(str(ws))
    assert st.clean, st
