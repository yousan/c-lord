"""#1265 — two workspaces that are each other's remote are not both deleted in one sweep.

A's submodule has a commit only on its local branch ``work``. B's submodule
adds A's as a remote, fetches it and makes ``work`` from ``a/work``; A then
adds B's as a remote and fetches. Each now has a remote-tracking ref that
reaches the commit, so each read as "pushed" — and one ``cleanup_orphaned``
sweep deleted both workspaces, and with them the commit's only copies.

A remote whose path is under the sessions root (another workspace, which the
same sweep may delete) is no copy anywhere: its tracking refs vouch for
nothing, as #1246 already made it for tags.
"""

from __future__ import annotations

from pathlib import Path

from c_lord import session_dir as sd

from .test_submodule_unpushed_1198 import _commit, _workspace, git


def _second_ws(tmp: Path, name: str) -> Path:
    ws = tmp / "sessions" / name
    git(tmp, "clone", "-q", "--single-branch", str(tmp / "parent.git"), str(ws))
    git(ws, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    return ws


def _cycle(tmp_path: Path) -> tuple[Path, Path, str]:
    a, _ = _workspace(tmp_path)
    b = _second_ws(tmp_path, "4343")
    sa = a / "sub"
    git(sa, "checkout", "-q", "-b", "work")
    sha = _commit(sa, "w.txt", "only copy\n")
    git(sa, "checkout", "-q", "main")
    git(b / "sub", "remote", "add", "a", str(sa))
    git(b / "sub", "fetch", "-q", "a")
    git(b / "sub", "branch", "-q", "work", "a/work")
    git(sa, "remote", "add", "b", str(b / "sub"))
    git(sa, "fetch", "-q", "b")
    return a, b, sha


def test_one_sweep_cycle_heads_variant(tmp_path: Path) -> None:
    """Same cycle as the #1246 tag test, with branches + tracking refs (the #1245 heads path)."""
    a, b, sha = _cycle(tmp_path)
    mgr = sd.SessionDirManager(base_dir=str(tmp_path / "sessions"), source_repo="/repo")
    results = mgr.cleanup_orphaned(set())
    reasons = [(r.thread_id, r.removed, r.reason) for r in results]
    assert a.exists() or b.exists(), f"one sweep deleted both; {sha} is gone: {reasons}"


def test_cycle_kept_with_sessions_root_unknown(tmp_path: Path) -> None:
    """Without the sessions root, no local-path remote outside the workspace vouches."""
    a, b, _ = _cycle(tmp_path)
    assert not sd.worktree_status(str(a)).clean
    assert not sd.worktree_status(str(b)).clean


def test_top_level_remote_in_a_neighbour_workspace_does_not_vouch(tmp_path: Path) -> None:
    """The workspace's own repo: a branch only on a neighbour workspace is unpushed."""
    a, _ = _workspace(tmp_path)
    b = _second_ws(tmp_path, "4343")
    git(a, "checkout", "-q", "-b", "work")
    _commit(a, "w.txt", "only copy\n")
    git(a, "checkout", "-q", "main")
    git(b, "remote", "add", "a", str(a))
    git(b, "fetch", "-q", "a")
    git(b, "branch", "-q", "work", "a/work")
    git(a, "remote", "add", "b", str(b))
    git(a, "fetch", "-q", "b")
    status = sd.worktree_status(str(a), sessions_root=str(tmp_path / "sessions"))
    assert not status.clean, sd.describe_changes(status)
    assert status.unpushed_commits == 1
