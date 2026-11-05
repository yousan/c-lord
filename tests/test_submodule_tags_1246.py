"""#1246 — an upstream tag on no branch no longer keeps an untouched workspace forever.

#1198 counts every commit a submodule's refs reach and no remote branch does,
tags included: a ``git tag wip`` on a detached commit was its only copy. But
a release tag upstream that points at a commit on no branch (its release
branch deleted) came along with ``git submodule update --init`` too, and a
workspace nobody touched read ``sub: 1 unpushed commit(s)`` and was never
deleted.

Now, whenever some commit is reachable only from tags, the remote is asked
(``git ls-remote --tags``): a tag whose name and object are upstream vouches
for what it reaches; every other tag-only commit is kept. The reflogs never
decide that such a commit came from upstream (review of 4e4bd18): a commit
made in a removed worktree, by plumbing (``commit-tree``), or as a dropped
stash has no "created here" entry, and a ``git tag wip`` was its only copy.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from c_lord import session_dir as sd

from .test_stale_tracking_ref_1245 import _count_ls_remote
from .test_submodule_stash_1121 import _delete
from .test_submodule_unpushed_1198 import _commit, _kept, _workspace, git


@pytest.fixture(autouse=True)
def _fresh_ls_remote_cache():
    sd._ls_remote_cache.clear()
    yield
    sd._ls_remote_cache.clear()


def _release_tag_on_deleted_branch(tmp: Path, *, annotated: bool = False) -> str:
    """Upstream: tag ``v1`` on a commit of ``release``, then ``release`` deleted."""
    seed = tmp / "subremote-seed"
    git(seed, "checkout", "-q", "-b", "release")
    sha = _commit(seed, "rel.txt")
    git(seed, *(["tag", "-a", "-m", "v1", "v1"] if annotated else ["tag", "v1"]))
    git(seed, "push", "-q", "origin", "release", "v1")
    git(seed, "push", "-q", "origin", ":release")
    return sha


def _reinit_sub(ws: Path) -> None:
    """A fresh ``git submodule update --init``: the clone brings every upstream tag."""
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    shutil.rmtree(ws / ".git" / "modules" / "sub")
    git(ws, "submodule", "update", "-q", "--init")


def _tag_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "--tags" in c]


def test_untouched_submodule_with_upstream_tag_on_no_branch_is_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    ws, _ = _workspace(tmp_path)
    sha = _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    assert git(ws / "sub", "rev-parse", "v1").strip() == sha
    assert not git(ws / "sub", "branch", "-r", "--contains", "v1").strip()  # on no branch
    calls = _count_ls_remote(monkeypatch)
    res = _delete(ws)
    assert res.removed, res.reason
    assert len(_tag_calls(calls)) == 1
    assert len(calls) == 1  # no ``--heads``: the tracking refs came from the clone


def test_fetched_tag_deleted_upstream_is_kept_accepted_over_keep(
    tmp_path: Path, monkeypatch
) -> None:
    """Accepted over-keep (spec): the tag is gone upstream, and a commit only it
    holds cannot be told from one made here without a trace in the reflogs."""
    ws, subr = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    git(subr, "update-ref", "-d", "refs/tags/v1")
    calls = _count_ls_remote(monkeypatch)
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason
    assert len(_tag_calls(calls)) == 1


def test_local_tag_on_new_commit_is_kept(tmp_path: Path, monkeypatch) -> None:
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "--detach")
    _commit(sub, "wip.txt")
    git(sub, "tag", "wip")
    git(sub, "checkout", "-q", "main")  # only the tag holds the commit now
    calls = _count_ls_remote(monkeypatch)
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason
    assert len(_tag_calls(calls)) == 1  # made here: upstream asked, wip not there


def test_strict_mode_upstream_tag_is_deleted_after_one_ls_remote_tags(
    tmp_path: Path, monkeypatch
) -> None:
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path, annotated=True)
    _reinit_sub(ws)
    git(ws / "sub", "config", "core.logAllRefUpdates", "false")
    calls = _count_ls_remote(monkeypatch)
    res = _delete(ws)
    assert res.removed, res.reason
    assert len(_tag_calls(calls)) == 1


def test_strict_mode_tag_not_upstream_is_kept(tmp_path: Path, monkeypatch) -> None:
    ws, subr = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    git(subr, "update-ref", "-d", "refs/tags/v1")
    git(ws / "sub", "config", "core.logAllRefUpdates", "false")
    calls = _count_ls_remote(monkeypatch)
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason
    assert len(_tag_calls(calls)) == 1


def test_strict_mode_ls_remote_failure_is_kept(tmp_path: Path) -> None:
    """Unknown stays unknown: a remote that cannot be asked vouches for nothing."""
    ws, subr = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    git(ws / "sub", "config", "core.logAllRefUpdates", "false")
    shutil.rmtree(subr)
    res = _kept(ws)
    assert "sub: unpushed commits unknown (ls-remote failed)" in res.reason, res.reason


def test_tag_only_on_a_remote_inside_the_workspace_is_kept(tmp_path: Path, monkeypatch) -> None:
    """A copy inside the workspace is deleted with it: that remote vouches for no tag."""
    ws, _ = _workspace(tmp_path)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "--detach")
    _commit(sub, "wip.txt")
    git(sub, "tag", "wip")
    git(sub, "checkout", "-q", "main")
    backup = ws / ".git" / "backup.git"
    git(ws, "init", "-q", "--bare", str(backup))
    git(sub, "remote", "add", "backup", str(backup))
    git(sub, "push", "-q", "backup", "wip")
    calls = _count_ls_remote(monkeypatch)
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason
    assert all(str(backup) not in " ".join(c) for c in calls)


# ---- review of 4e4bd18: commits with no "created here" reflog entry ----


def test_a_commit_made_in_a_removed_worktree_with_local_tag_is_kept(tmp_path: Path) -> None:
    """(A) ``worktree remove`` + ``branch -D`` took both reflogs with them."""
    ws, _ = _workspace(tmp_path)
    sub = ws / "sub"
    wt = tmp_path / "wt"
    git(sub, "worktree", "add", "-q", "-b", "tmp", str(wt))
    _commit(wt, "wt.txt")
    git(wt, "tag", "wip")
    git(sub, "worktree", "remove", str(wt))
    git(sub, "branch", "-q", "-D", "tmp")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_e_plumbing_commit_with_local_tag_is_kept(tmp_path: Path) -> None:
    """(E) ``write-tree`` + ``commit-tree`` write no reflog entry."""
    ws, _ = _workspace(tmp_path)
    sub = ws / "sub"
    (sub / "p.txt").write_text("plumbing\n")
    git(sub, "add", "p.txt")
    tree = git(sub, "write-tree").strip()
    commit = git(sub, "commit-tree", tree, "-p", "HEAD", "-m", "plumbing").strip()
    git(sub, "tag", "wip", commit)
    git(sub, "reset", "-q", "--hard", "HEAD")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_f_dropped_stash_with_local_tag_is_kept(tmp_path: Path) -> None:
    """(F) ``git stash drop`` removes the ``logs/refs/stash`` line."""
    ws, _ = _workspace(tmp_path)
    sub = ws / "sub"
    (sub / "README.md").write_text("changed\n")
    git(sub, "stash", "-q")
    git(sub, "tag", "wip", "stash@{0}")
    git(sub, "stash", "drop", "-q")
    res = _kept(ws)
    assert "unpushed commit(s)" in res.reason, res.reason
