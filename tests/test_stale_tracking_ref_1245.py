"""#1245 — a submodule branch deleted on the remote is not "pushed" any more.

Commit on ``work`` in a submodule, push it, and the PR is closed with its
branch deleted on the remote. The submodule still has ``refs/remotes/origin/work``,
and "pushed" was judged from local tracking refs only — so /workspace-delete
read the commit as safe and deleted its only copy with the workspace.

Now, when nothing else keeps the workspace, each submodule's remote is asked
(``git ls-remote --heads``, as the workspace already does for itself), and a
tracking ref whose branch is gone vouches for nothing. A remote that cannot be
asked keeps the workspace.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from c_lord import session_dir as sd
from c_lord.session_dir import describe_changes, worktree_status

from .test_submodule_unpushed_1198 import _commit, _delete, _kept, _object_exists, _workspace, git


@pytest.fixture(autouse=True)
def _fresh_ls_remote_cache():
    sd._ls_remote_cache.clear()
    yield
    sd._ls_remote_cache.clear()


def _pushed_then_deleted_on_remote(ws: Path, subr: Path) -> str:
    sub = ws / "sub"
    git(sub, "checkout", "-q", "-b", "work")
    sha = _commit(sub, "w.txt")
    git(sub, "push", "-q", "origin", "work")
    git(sub, "checkout", "-q", "--detach", "HEAD~1")
    git(sub, "branch", "-q", "-D", "work")
    git(subr, "update-ref", "-d", "refs/heads/work")
    git(subr, "gc", "-q", "--prune=now")
    assert not _object_exists(subr, sha)
    return sha


def _workspace_with_upstream_branch(tmp: Path, branch: str) -> tuple[Path, Path]:
    """A workspace whose submodule was cloned while upstream had *branch* too."""
    from .test_submodule_unpushed_1198 import _bare_with_commit

    parent = _bare_with_commit(tmp, "parent")
    subr = _bare_with_commit(tmp, "subremote")
    seed = tmp / "subremote-seed"
    git(seed, "checkout", "-q", "-b", branch)
    _commit(seed, "branch.txt")
    git(seed, "push", "-q", "origin", branch)
    git(seed, "checkout", "-q", "main")
    base = tmp / "sessions"
    base.mkdir()
    ws = base / "4242"
    git(tmp, "clone", "-q", "--single-branch", str(parent), str(ws))
    git(ws, "submodule", "add", "-q", str(subr), "sub")
    git(ws, "commit", "-qm", "add sub")
    git(ws, "push", "-q", "origin", "HEAD:main")
    assert f"origin/{branch}" in git(ws / "sub", "branch", "-r")
    return ws, subr


def _count_ls_remote(monkeypatch) -> list[list[str]]:  # noqa: ANN001
    calls: list[list[str]] = []
    real = sd._run

    def spy(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if "ls-remote" in cmd:
            calls.append(cmd)
        return real(cmd, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(sd, "_run", spy)
    return calls


def test_deinit_submodule_with_branch_deleted_on_remote_is_kept(tmp_path: Path) -> None:
    """Same loss with the checkout gone: only ``.git/modules/sub`` holds the commit."""
    ws, subr = _workspace(tmp_path)
    _pushed_then_deleted_on_remote(ws, subr)
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s) (remote branch deleted or rewritten)" in res.reason


def test_pushed_branch_still_on_remote_is_clean(tmp_path: Path) -> None:
    """Control: the branch is still there — nothing to lose."""
    ws, _ = _workspace(tmp_path)
    git(ws / "sub", "checkout", "-q", "-b", "work")
    _commit(ws / "sub", "w.txt")
    git(ws / "sub", "push", "-q", "origin", "work")
    git(ws / "sub", "checkout", "-q", "--detach", "HEAD~1")
    git(ws / "sub", "branch", "-q", "-D", "work")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
    assert _delete(ws).removed


def test_untouched_submodule_whose_upstream_moved_is_deleted_offline(
    tmp_path: Path, monkeypatch
) -> None:
    """Second review of 0e9b09c: a commit to upstream ``main`` made every workspace
    with that submodule undeletable. Nothing was pushed from here, so the
    tracking refs hold only what came from the remote — trusted, no network."""
    ws, _ = _workspace(tmp_path)
    _commit(tmp_path / "subremote-seed", "later.txt")
    git(tmp_path / "subremote-seed", "push", "-q", "origin", "HEAD:main")
    calls = _count_ls_remote(monkeypatch)
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
    assert calls == []
    assert _delete(ws).removed
    assert calls == []


def test_upstream_force_push_of_a_branch_present_at_clone_is_deleted(tmp_path: Path) -> None:
    """The reviewer's ``dependabot/x``: fetched with the clone, force-pushed since."""
    seed = tmp_path / "subremote-seed"
    ws, _ = _workspace_with_upstream_branch(tmp_path, "dependabot/x")
    git(seed, "checkout", "-q", "-B", "dependabot/x", "main")
    _commit(seed, "rewritten.txt")
    git(seed, "push", "-q", "-f", "origin", "dependabot/x")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
    assert _delete(ws).removed


def test_upstream_branch_deleted_after_the_clone_is_deleted(tmp_path: Path) -> None:
    ws, subr = _workspace_with_upstream_branch(tmp_path, "feature/gone")
    git(subr, "update-ref", "-d", "refs/heads/feature/gone")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
    assert "unpushed commit" not in describe_changes(st)
    assert _delete(ws).removed


def test_user_pushed_branch_moved_on_the_remote_to_a_commit_not_here_is_kept(
    tmp_path: Path,
) -> None:
    """Pushed from here, then someone else pushed on top: unknown without a fetch."""
    ws, _ = _workspace(tmp_path)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "-b", "work")
    _commit(sub, "w.txt")
    git(sub, "push", "-q", "origin", "work")
    git(sub, "checkout", "-q", "--detach", "HEAD~1")
    git(sub, "branch", "-q", "-D", "work")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "-b", "work", str(tmp_path / "subremote.git"), str(other))
    _commit(other, "theirs.txt")
    git(other, "push", "-q", "origin", "work")
    res = _kept(ws)
    assert "sub: unpushed commits unknown (remote branch moved to a commit not here)" in res.reason


def test_without_reflogs_a_moved_branch_is_verified(tmp_path: Path) -> None:
    """``core.logAllRefUpdates=false``: a push leaves no trace, so every tracking
    ref is verified; one that moved to a commit not here is unknown — kept."""
    ws, _ = _workspace(tmp_path)
    git(ws / "sub", "config", "core.logAllRefUpdates", "false")
    _commit(tmp_path / "subremote-seed", "later.txt")
    git(tmp_path / "subremote-seed", "push", "-q", "origin", "HEAD:main")
    res = _kept(ws)
    assert "remote branch moved to a commit not here" in res.reason


def test_branch_deleted_and_recreated_elsewhere_is_unknown(tmp_path: Path) -> None:
    """``work`` deleted on the remote and recreated on other history, not fetched."""
    ws, subr = _workspace(tmp_path)
    sha = _pushed_then_deleted_on_remote(ws, subr)
    git(ws / "sub", "update-ref", "refs/remotes/origin/work", sha)  # the stale view
    seed = tmp_path / "subremote-seed"
    git(seed, "checkout", "-q", "--orphan", "fresh")
    _commit(seed, "fresh.txt")
    git(seed, "push", "-q", "origin", "HEAD:work")
    assert not _kept(ws).removed


def test_remote_branch_moved_and_the_new_head_is_here_is_clean(tmp_path: Path) -> None:
    """Control: the new head is here and descends from the tracking ref — the
    old tracking ref's commits are on the live branch."""
    ws, _ = _workspace(tmp_path)
    _commit(tmp_path / "subremote-seed", "later.txt")
    git(tmp_path / "subremote-seed", "push", "-q", "origin", "HEAD:main")
    # The new head's objects arrive without moving origin/main (no refmap) and
    # without a local ref holding them.
    git(ws / "sub", "fetch", "-q", "--refmap=", "origin", "main:refs/tmp/upstream")
    git(ws / "sub", "update-ref", "-d", "refs/tmp/upstream")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)


def test_remote_branch_rewritten_and_fetched_is_kept(tmp_path: Path) -> None:
    """``work`` was force-pushed to other history, and the new head is here. The
    old commit is held only by the stale ``origin/work`` — kept."""
    ws, subr = _workspace(tmp_path)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "-b", "work")
    _commit(sub, "w.txt")
    git(sub, "push", "-q", "origin", "work")
    git(sub, "tag", "old-work")  # tags never vouch; keeps the old commit around
    git(sub, "reset", "-q", "--hard", "HEAD~1")
    _commit(sub, "other.txt")
    git(sub, "push", "-q", "-f", "origin", "work")
    git(sub, "fetch", "-q", "origin")  # the new head is here
    git(sub, "update-ref", "refs/remotes/origin/work", "old-work")  # a stale view of work
    git(sub, "checkout", "-q", "--detach", "origin/main")
    git(sub, "branch", "-q", "-D", "work")
    git(sub, "tag", "-d", "old-work")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s) (remote branch deleted or rewritten)" in res.reason


def _pushed_branch(ws: Path) -> None:
    sub = ws / "sub"
    git(sub, "checkout", "-q", "-b", "work")
    _commit(sub, "w.txt")
    git(sub, "push", "-q", "origin", "work")
    git(sub, "checkout", "-q", "--detach", "HEAD~1")


def test_submodule_remote_that_cannot_be_asked_keeps(tmp_path: Path) -> None:
    """A branch pushed from here needs the remote; it cannot be asked — kept."""
    ws, _ = _workspace(tmp_path)
    _pushed_branch(ws)
    git(ws / "sub", "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    res = _kept(ws)
    assert "sub: unpushed commits unknown (ls-remote failed)" in res.reason


def test_untouched_submodule_whose_remote_is_gone_is_deleted(tmp_path: Path) -> None:
    """Nothing pushed from here: the remote is not needed, so not asked."""
    ws, _ = _workspace(tmp_path)
    git(ws / "sub", "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert _delete(ws).removed


def test_network_is_not_asked_when_something_else_keeps(tmp_path: Path, monkeypatch) -> None:
    ws, subr = _workspace(tmp_path)
    _pushed_then_deleted_on_remote(ws, subr)
    (ws / ".env").write_text("SECRET=1\n")
    calls = _count_ls_remote(monkeypatch)
    st = worktree_status(str(ws))
    assert not st.clean
    assert calls == []


def test_one_ls_remote_per_submodule_remote(tmp_path: Path, monkeypatch) -> None:
    """A branch pushed from here: the remote is asked once (cached per URL)."""
    ws, _ = _workspace(tmp_path)
    _pushed_branch(ws)
    calls = _count_ls_remote(monkeypatch)
    assert worktree_status(str(ws)).clean
    assert worktree_status(str(ws)).clean
    assert len(calls) == 1, calls


def test_remote_inside_the_workspace_never_vouches(tmp_path: Path) -> None:
    """Second review (b): the submodule's origin re-pointed to ``build/mirror.git``
    inside the workspace. Deleting the workspace deletes that remote too."""
    ws, subr = _workspace(tmp_path)
    (ws / ".gitignore").write_text("build/\n")
    git(ws, "add", ".gitignore")
    git(ws, "commit", "-qm", "ignore build")
    git(ws, "push", "-q", "origin", "HEAD:main")
    mirror = ws / "build" / "mirror.git"
    git(tmp_path, "clone", "-q", "--bare", str(subr), str(mirror))
    git(ws / "sub", "remote", "set-url", "origin", str(mirror))
    _pushed_branch(ws)
    res = _kept(ws)
    # As if there were no remote: every commit counts, the cloned ``main`` too.
    assert "sub: 2 unpushed commit(s) (only on a remote inside the workspace)" in res.reason


# ---- review of 43caee1: the ls-remote cache ----


def _bare(tmp: Path, name: str) -> Path:
    from .test_submodule_unpushed_1198 import _bare_with_commit

    return _bare_with_commit(tmp, name)


def test_same_relative_url_in_two_repos_does_not_share_an_answer(tmp_path: Path) -> None:
    answers = []
    for side in ("a", "b"):
        d = tmp_path / side
        d.mkdir()
        _bare(d, "r")  # d/r.git, each with its own commit
        _commit(d / "r-seed", f"{side}.txt")
        git(d / "r-seed", "push", "-q", "origin", "HEAD:main")
        repo = d / "repo"
        git(d, "init", "-q", str(repo))
        git(repo, "remote", "add", "origin", "../r.git")
        answers.append(sd._ls_remote_refs(str(repo), "origin", "../r.git", ["git"]))
    assert answers[0] and answers[1]
    assert answers[0] != answers[1], "two repos' ../r.git shared one cached answer"


def test_a_failed_ls_remote_is_cached_as_unknown(tmp_path: Path, monkeypatch) -> None:
    """An offline host waits for each URL once per TTL, not once per workspace."""
    calls = _count_ls_remote(monkeypatch)
    repos = []
    for side in ("a", "b"):
        repo = tmp_path / side
        git(tmp_path, "init", "-q", str(repo))
        git(repo, "remote", "add", "origin", str(tmp_path / "gone.git"))
        repos.append(repo)
    for repo in repos:
        assert sd._ls_remote_refs(str(repo), "origin", str(tmp_path / "gone.git"), ["git"]) is None
    assert len(calls) == 1, calls


# ---- third review of ef8f0fc: a remote inside the workspace, top level too ----


def _backup_inside(ws: Path) -> Path:
    (ws / ".gitignore").write_text("build/\n")
    git(ws, "add", ".gitignore")
    git(ws, "commit", "-qm", "ignore build")
    git(ws, "push", "-q", "origin", "HEAD:main")
    backup = ws / "build" / "backup.git"
    git(ws, "init", "-q", "--bare", str(backup))
    git(ws, "remote", "add", "backup", str(backup))
    return backup


def test_b3_workspace_branch_pushed_only_to_a_remote_inside_it_is_kept(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _backup_inside(ws)
    git(ws, "checkout", "-q", "-b", "work")
    _commit(ws, "w.txt")
    git(ws, "push", "-q", "backup", "work")
    res = _kept(ws)
    assert "1 unpushed commit(s)" in res.reason, res.reason


def test_claude_worktree_judges_inside_by_the_outer_workspace(tmp_path: Path) -> None:
    """A Claude Code worktree under ``.claude/worktrees/x``: ``build/backup.git``
    is inside the workspace, not inside the worktree — it still never vouches."""
    ws, _ = _workspace(tmp_path)
    _backup_inside(ws)
    wt = ws / ".claude" / "worktrees" / "x"
    git(ws, "worktree", "add", "-q", "-b", "wt-work", str(wt))
    _commit(wt, "w.txt")
    git(wt, "push", "-q", "backup", "wt-work")
    st = worktree_status(str(ws))
    assert "?? .claude/worktrees/x/" in st.user_changes, st
