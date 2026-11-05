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
import tempfile
import time
from pathlib import Path

import pytest

from c_lord import session_dir as sd

from .test_stale_tracking_ref_1245 import _count_ls_remote
from .test_submodule_stash_1121 import _delete
from .test_submodule_unpushed_1198 import _commit, _kept, _workspace, git


@pytest.fixture(autouse=True)
def _fresh_ls_remote_cache(tmp_path: Path, monkeypatch):
    sd._ls_remote_cache.clear()
    # A TMPDIR of the test's own: the neutral dir of ls-remote lives there.
    t = tmp_path / "tmpdir"
    t.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(t))
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


def _removed(ws: Path) -> sd.CleanupResult:
    res = _delete(ws)
    assert res.removed and not ws.exists(), res.reason
    return res


def _wip(sub: Path, *tag_args: str) -> str:
    git(sub, "checkout", "-q", "--detach")
    sha = _commit(sub, "wip.txt", "only copy\n")
    git(sub, "tag", *tag_args)
    git(sub, "checkout", "-q", "main")
    return sha


# ---- W57 round 12 (PR #1264) -------------------------------------------
#
# Hand-made tags, upstream tags moved or deleted, other places a repo lives,
# the #903 network gate, and two workspaces that are each other's remote.

# hand-made tags


@pytest.mark.parametrize("args", [("wip",), ("-a", "-m", "wip", "wip")])
def test_hand_tag_only_commit_kept_even_with_upstream_tags(tmp_path: Path, args) -> None:
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    _wip(ws / "sub", *args)
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_hand_tag_packed_refs_kept(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    _wip(ws / "sub", "wip")
    git(ws / "sub", "pack-refs", "--all")
    shutil.rmtree(ws / ".git" / "modules" / "sub" / "logs", ignore_errors=True)
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


@pytest.mark.parametrize("annotated_upstream", [False, True])
@pytest.mark.parametrize("annotated_local", [False, True])
def test_retag_upstream_name_on_own_commit_kept(
    tmp_path: Path, annotated_upstream: bool, annotated_local: bool
) -> None:
    """``git tag -f v1 <own commit>``: same name as upstream, other object."""
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path, annotated=annotated_upstream)
    _reinit_sub(ws)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "v1")
    own = _commit(sub, "own.txt", "only copy\n")
    if annotated_local:
        git(sub, "tag", "-f", "-a", "-m", "mine", "v1", own)
    else:
        git(sub, "tag", "-f", "v1", own)
    git(sub, "checkout", "-q", "main")
    res = _kept(ws)
    # own commit + (old release commit no longer tagged locally? it is unreachable)
    assert "unpushed commit(s)" in res.reason, res.reason


def test_child_of_upstream_tag_with_hand_tag_counts_one(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "v1")
    _commit(sub, "child.txt", "only copy\n")
    git(sub, "tag", "wip")
    git(sub, "checkout", "-q", "main")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_wrapping_annotated_tag_of_upstream_tag_object(tmp_path: Path) -> None:
    """A tag of the upstream (annotated) tag adds no commit: removable."""
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path, annotated=True)
    _reinit_sub(ws)
    sub = ws / "sub"
    git(sub, "tag", "-a", "-m", "wrap", "wrap", "v1")
    _removed(ws)


def test_pushed_hand_tag_is_removed(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    sub = ws / "sub"
    _wip(sub, "wip")
    git(sub, "push", "-q", "origin", "wip")
    _removed(ws)


# ---- upstream tag moved / deleted ----------------------------------------


def test_upstream_tag_force_moved_kept(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    seed = tmp_path / "subremote-seed"
    git(seed, "checkout", "-q", "main")
    git(seed, "tag", "-f", "v1", "main")
    git(seed, "push", "-q", "-f", "origin", "v1")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_fetch_tags_brings_upstream_no_branch_tags_removable(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    git(ws / "sub", "fetch", "-q", "--tags")
    _removed(ws)


def test_tag_of_tag_upstream_removable(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    seed = tmp_path / "subremote-seed"
    git(seed, "checkout", "-q", "-b", "release")
    _commit(seed, "rel.txt")
    git(seed, "tag", "-a", "-m", "in", "inner")
    git(seed, "tag", "-a", "-m", "out", "outer", "inner")
    git(seed, "tag", "-d", "inner")  # only the outer remains, wrapping inner
    git(seed, "push", "-q", "origin", "release", "outer")
    git(seed, "push", "-q", "origin", ":release")
    _reinit_sub(ws)
    _removed(ws)


@pytest.mark.parametrize("kind", ["tree", "blob"])
def test_upstream_tag_on_tree_or_blob(tmp_path: Path, kind: str) -> None:
    """Upstream also has a tag on a tree/blob (linux's v2.6.11-tree)."""
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    seed = tmp_path / "subremote-seed"
    obj = git(seed, "rev-parse", "main^{tree}" if kind == "tree" else "main:README.md").strip()
    git(seed, "tag", "-a", "-m", kind, f"t-{kind}", obj)
    git(seed, "push", "-q", "origin", f"t-{kind}")
    _reinit_sub(ws)
    _removed(ws)


# ---- other places ---------------------------------------------------------


def test_deinit_upstream_tag_removable(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    _removed(ws)


def test_deinit_wip_kept(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    _wip(ws / "sub", "wip")
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_nested_sub_of_sub_wip_kept(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    inner = tmp_path / "inner.git"
    git(tmp_path, "init", "-q", "--bare", str(inner))
    iseed = tmp_path / "inner-seed"
    git(tmp_path, "clone", "-q", str(inner), str(iseed))
    _commit(iseed, "i.txt")
    git(iseed, "push", "-q", "origin", "HEAD:main")
    seed = tmp_path / "subremote-seed"
    git(seed, "pull", "-q", "origin", "main")
    git(seed, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "inner")
    git(seed, "commit", "-qm", "inner")
    git(seed, "push", "-q", "origin", "HEAD:main")
    sub = ws / "sub"
    git(sub, "pull", "-q", "origin", "main")
    git(sub, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    git(ws, "add", "sub")
    git(ws, "commit", "-qm", "bump")
    git(ws, "push", "-q", "origin", "HEAD:main")
    _wip(sub / "inner", "wip")
    res = _kept(ws)
    assert "sub/inner: 1 unpushed commit(s)" in res.reason, res.reason


def test_ignored_vendor_clone_wip_kept_upstream_tag_removed(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    (ws / ".gitignore").write_text("vendor/\n")
    git(ws, "add", ".gitignore")
    git(ws, "commit", "-qm", "ignore")
    git(ws, "push", "-q", "origin", "HEAD:main")
    (ws / "vendor").mkdir()
    git(ws / "vendor", "clone", "-q", str(subr), "lib")
    lib = ws / "vendor" / "lib"
    assert "v1" in git(lib, "tag")
    _removed(ws)


def test_ignored_vendor_clone_wip_kept(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    (ws / ".gitignore").write_text("vendor/\n")
    git(ws, "add", ".gitignore")
    git(ws, "commit", "-qm", "ignore")
    git(ws, "push", "-q", "origin", "HEAD:main")
    (ws / "vendor").mkdir()
    git(ws / "vendor", "clone", "-q", str(subr), "lib")
    _wip(ws / "vendor" / "lib", "wip")
    res = _kept(ws)
    assert "vendor/lib: 1 unpushed commit(s)" in res.reason, res.reason


# ---- network ---------------------------------------------------------------


def test_idle_no_tag_only_zero_network(tmp_path: Path, monkeypatch) -> None:
    ws, _ = _workspace(tmp_path)
    git(ws / "sub", "fetch", "-q", "--tags")
    calls = _count_ls_remote(monkeypatch)
    _removed(ws)
    assert calls == []


def test_dirty_workspace_asks_no_tags(tmp_path: Path, monkeypatch) -> None:
    """#903: the network is asked only when nothing else keeps the dir."""
    ws, _ = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    (ws / "dirty.txt").write_text("user work\n")
    calls = _count_ls_remote(monkeypatch)
    res = _kept(ws)
    assert "dirty.txt" in res.reason, res.reason
    assert calls == [], calls


def test_real_unpushed_branch_plus_dead_remote_reason(tmp_path: Path) -> None:
    """A real unpushed branch commit: reason should still say 1 unpushed commit(s)."""
    ws, subr = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "-b", "work")
    _commit(sub, "work.txt", "only copy\n")
    url = git(sub, "config", "remote.origin.url").strip()
    git(sub, "config", "remote.origin.url", url + "-gone")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def _hang_wrapper(tmp: Path, tag: str) -> Path:
    w = tmp / "ssh-hang.sh"
    w.write_text(f'#!/bin/bash\nif [ "$1" = -G ]; then exit 0; fi\nsleep 300{tag}\nexit 1\n')
    w.chmod(0o755)
    return w


def test_n_subs_with_dead_remotes_cost_one_timeout(tmp_path: Path, monkeypatch) -> None:
    ws, subr = _workspace(tmp_path)
    _release_tag_on_deleted_branch(tmp_path)
    _reinit_sub(ws)
    # 3 more submodules from the same upstream, each with a distinct dead ssh URL
    for i in range(3):
        git(ws, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(subr), f"s{i}")
    git(ws, "commit", "-qm", "more subs")
    git(ws, "push", "-q", "origin", "HEAD:main")
    names = ["sub", "s0", "s1", "s2"]
    for i, n in enumerate(names):
        assert "v1" in git(ws / n, "tag")
        git(ws / n, "config", "remote.origin.url", f"ssh://fakehost{i}/x.git")
    monkeypatch.setenv("GIT_SSH_COMMAND", str(_hang_wrapper(tmp_path, "1264")))
    monkeypatch.setattr(sd, "_LS_REMOTE_TIMEOUT_SECONDS", 2)
    t0 = time.monotonic()
    res = _delete(ws)
    took = time.monotonic() - t0
    assert not res.removed
    assert took < 4, f"{took:.1f}s for {len(names)} subs: {res.reason}"


# ---- cache across workspaces ----------------------------------------------


def _second_ws(tmp: Path, name: str) -> Path:
    ws = tmp / "sessions" / name
    git(tmp, "clone", "-q", "--single-branch", str(tmp / "parent.git"), str(ws))
    git(ws, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    return ws


def test_two_workspaces_vouching_for_each_other_one_is_kept(tmp_path: Path) -> None:
    """A's sub has remote → B's sub, B's sub has remote → A's sub; the only copy
    of wip is in A and B. A status check (dry run) fills the 60 s cache; both
    are then deleted and the commit is gone."""
    a, _ = _workspace(tmp_path)
    b = _second_ws(tmp_path, "4343")
    sha = _wip(a / "sub", "wip")
    git(b / "sub", "remote", "add", "a", str(a / "sub"))
    git(b / "sub", "fetch", "-q", "a", "tag", "wip")
    git(a / "sub", "remote", "add", "b", str(b / "sub"))
    # sequential deletes without prior check: one survives
    sd.worktree_status(str(a))
    sd.worktree_status(str(b))  # e.g. /workspace-cleanup dry run
    ra = _delete(a)
    rb = _delete(b)
    assert not (ra.removed and rb.removed), f"both deleted, {sha} lost"


def test_two_workspaces_no_prior_check(tmp_path: Path) -> None:
    a, _ = _workspace(tmp_path)
    b = _second_ws(tmp_path, "4343")
    _wip(a / "sub", "wip")
    git(b / "sub", "remote", "add", "a", str(a / "sub"))
    git(b / "sub", "fetch", "-q", "a", "tag", "wip")
    git(a / "sub", "remote", "add", "b", str(b / "sub"))
    ra = _delete(a)
    rb = _delete(b)
    assert not (ra.removed and rb.removed)


def test_tmpdir_repo_insteadof_cannot_redirect_tags_into_workspace(tmp_path: Path) -> None:
    """1259b-A on the tag path: tempdir inside a repo whose config rewrites the
    sub's URL to the workspace's own module dir (which has wip)."""
    ws, subr = _workspace(tmp_path)
    _wip(ws / "sub", "wip")
    t = Path(tempfile.gettempdir())
    git(t.parent, "init", "-q")
    git(t.parent, "config", f"url.{ws / '.git' / 'modules' / 'sub'}.insteadOf", str(subr))
    res = _delete(ws)
    assert not res.removed, "deleted the only copy of wip via tmpdir insteadOf"


def test_one_sweep_two_workspaces_vouching_for_each_other_one_is_kept(tmp_path: Path) -> None:
    """The real sweep: find_session_dirs() runs worktree_status on every dir first
    (cache filled while both exist), then _try_remove reuses the cached answers."""
    a, _ = _workspace(tmp_path)
    b = _second_ws(tmp_path, "4343")
    sha = _wip(a / "sub", "wip")
    git(b / "sub", "remote", "add", "a", str(a / "sub"))
    git(b / "sub", "fetch", "-q", "a", "tag", "wip")
    git(a / "sub", "remote", "add", "b", str(b / "sub"))
    mgr = sd.SessionDirManager(base_dir=str(tmp_path / "sessions"), source_repo="/repo")
    results = mgr.cleanup_orphaned(set())
    reasons = [(r.thread_id, r.removed, r.reason) for r in results]
    assert a.exists() or b.exists(), f"one sweep deleted both; {sha} is gone: {reasons}"
