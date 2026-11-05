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


def test_remote_branch_moved_to_a_commit_not_fetched_is_unknown(tmp_path: Path) -> None:
    """Review of 43caee1: upstream ``main`` moved to a commit this clone never
    fetched. Fast-forward or force-push cannot be told without a fetch, and a
    fetch is not made — unknown, kept (an accepted over-keep)."""
    ws, _ = _workspace(tmp_path)
    _commit(tmp_path / "subremote-seed", "later.txt")
    git(tmp_path / "subremote-seed", "push", "-q", "origin", "HEAD:main")
    res = _kept(ws)
    assert "sub: unpushed commits unknown (remote branch moved to a commit not here)" in res.reason


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


def test_submodule_remote_that_cannot_be_asked_keeps(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    git(ws / "sub", "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    res = _kept(ws)
    assert "sub: unpushed commits unknown (ls-remote failed)" in res.reason


def test_network_is_not_asked_when_something_else_keeps(tmp_path: Path, monkeypatch) -> None:
    ws, subr = _workspace(tmp_path)
    _pushed_then_deleted_on_remote(ws, subr)
    (ws / ".env").write_text("SECRET=1\n")
    calls = _count_ls_remote(monkeypatch)
    st = worktree_status(str(ws))
    assert not st.clean
    assert calls == []


def test_one_ls_remote_per_submodule_remote(tmp_path: Path, monkeypatch) -> None:
    """The clean path asks each submodule remote once (cached per URL)."""
    ws, _ = _workspace(tmp_path)
    calls = _count_ls_remote(monkeypatch)
    assert worktree_status(str(ws)).clean
    assert worktree_status(str(ws)).clean
    assert len(calls) == 1, calls


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
