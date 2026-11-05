"""#1289 — two sessions roots on one host (prod + staging) must not delete each other's work.

Workspace A lives under prod's sessions root, B under staging's, and each is
the other's remote. #1265 made a remote under *this* instance's root vouch for
nothing, but staging's root is outside prod's: prod's sweep deleted A because
B "had" the commit, then staging's sweep deleted B because its tracking refs
still pointed at A's commit — and the only copy was gone.

A local-path remote now vouches only when it is a live path that is not a
c-lord workspace of any instance: not under a directory carrying the
``.clord-sessions-root`` marker every ``SessionDirManager`` leaves at its root,
not inside a checkout c-lord has worked in (``.git/clord-coauthors``,
``.claude/skills/discord-read``). A path that is gone is judged by what is
left above it: under a marked root it vouches for nothing. A plain local
mirror (``/srv/git/x.git``) still vouches.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from c_lord import session_dir as sd
from c_lord.coauthor import install_coauthor_hook

from .test_submodule_unpushed_1198 import _commit, _workspace, git


def _ws(tmp: Path, root: Path, name: str) -> Path:
    ws = root / name
    root.mkdir(parents=True, exist_ok=True)
    git(tmp, "clone", "-q", "--single-branch", str(tmp / "parent.git"), str(ws))
    git(ws, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    return ws


def _seed(tmp: Path) -> Path:
    seed, _ = _workspace(tmp)
    shutil.rmtree(seed)
    return tmp / "sessions"


def _work(repo: Path) -> str:
    git(repo, "checkout", "-q", "-b", "work")
    sha = _commit(repo, "w.txt", "only copy\n")
    git(repo, "checkout", "-q", "main")
    return sha


def _sweep(root: Path) -> list:
    mgr = sd.SessionDirManager(base_dir=str(root), source_repo="/repo")
    return [(r.thread_id, r.removed, r.reason) for r in mgr.cleanup_orphaned(set())]


def _alive(*ws: Path) -> bool:
    return any(w.exists() for w in ws)


def _sub(sub: bool):
    return (lambda w: w / "sub") if sub else (lambda w: w)


def _mutual(tmp_path: Path, sub: bool) -> tuple[Path, Path, Path, Path, str]:
    root = _seed(tmp_path)
    other = tmp_path / "other-sessions"
    a, b = _ws(tmp_path, root, "4242"), _ws(tmp_path, other, "4343")
    p = _sub(sub)
    sha = _work(p(a))
    git(p(b), "remote", "add", "a", str(p(a)))
    git(p(b), "fetch", "-q", "a")
    git(p(b), "branch", "-q", "work", "a/work")
    git(p(a), "remote", "add", "b", str(p(b)))
    git(p(a), "fetch", "-q", "b")
    return root, other, a, b, sha


@pytest.mark.parametrize("sub", [False, True])
def test_two_roots_sequential(tmp_path: Path, sub: bool) -> None:
    """The Issue's repro: prod's sweep, then staging's, must leave a copy."""
    root = _seed(tmp_path)
    other = tmp_path / "other-sessions"
    a, b = _ws(tmp_path, root, "4242"), _ws(tmp_path, other, "4343")
    p = _sub(sub)
    sha = _work(p(a))
    git(p(b), "remote", "add", "a", str(p(a)))
    git(p(b), "fetch", "-q", "a")
    git(p(b), "branch", "-q", "work", "a/work")
    git(p(a), "remote", "add", "b", str(p(b)))
    git(p(a), "fetch", "-q", "b")
    r = _sweep(root) + _sweep(other)
    assert _alive(a, b), f"prod then staging sweep deleted {sha}: {r}"


@pytest.mark.parametrize("sub", [False, True])
def test_two_roots_both_instances_running(tmp_path: Path, sub: bool) -> None:
    """Both instances are up (each left its root marker): neither vouches for the other.

    Prod and staging sweep at their own startup, possibly at the same time —
    each must see the other's workspace as no copy while both still exist.
    """
    root, other, a, b, sha = _mutual(tmp_path, sub)
    sd.SessionDirManager(base_dir=str(root), source_repo="/repo")
    sd.SessionDirManager(base_dir=str(other), source_repo="/repo")
    assert (other / sd.SESSIONS_ROOT_MARKER).is_file()
    st_a = sd.worktree_status(str(a), sessions_root=str(root))
    st_b = sd.worktree_status(str(b), sessions_root=str(other))
    assert not st_a.clean, f"A vouched by staging's workspace: {sha}"
    assert not st_b.clean, f"B vouched by prod's workspace: {sha}"


@pytest.mark.parametrize("sub", [False, True])
def test_neighbour_workspace_with_clord_markers_does_not_vouch(tmp_path: Path, sub: bool) -> None:
    """No root marker (an older instance), but B is a checkout c-lord has worked in."""
    root, _other, a, b, sha = _mutual(tmp_path, sub)
    assert install_coauthor_hook(b) is not None
    st = sd.worktree_status(str(a), sessions_root=str(root))
    assert not st.clean, f"A vouched by a c-lord workspace outside its root: {sha}"


@pytest.mark.parametrize("sub", [False, True])
def test_deleted_neighbour_under_a_marked_root_does_not_vouch(tmp_path: Path, sub: bool) -> None:
    """Staging's step: A is already gone, only prod's root (with its marker) is left.

    B's tracking refs still reach A's commit; B is now its only copy.
    """
    root, other, a, b, sha = _mutual(tmp_path, sub)
    sd.SessionDirManager(base_dir=str(root), source_repo="/repo")
    shutil.rmtree(a)
    st = sd.worktree_status(str(b), sessions_root=str(other))
    assert not st.clean, f"B vouched by a deleted workspace of another root: {sha}"


@pytest.mark.parametrize("sub", [False, True])
def test_plain_local_mirror_outside_any_root_still_vouches(tmp_path: Path, sub: bool) -> None:
    """Over-keep check: a live bare mirror with no c-lord marker is a real copy."""
    root = _seed(tmp_path)
    a = _ws(tmp_path, root, "4242")
    p = _sub(sub)
    mirror = tmp_path / "srv" / "git" / "x.git"
    git(tmp_path, "init", "-q", "--bare", str(mirror))
    _work(p(a))
    git(p(a), "remote", "add", "x", str(mirror))
    git(p(a), "push", "-q", "x", "work")
    r = _sweep(root)
    assert not a.exists(), r


def test_root_marker_failure_never_stops_the_manager(tmp_path: Path) -> None:
    """The marker is a hint: an unwritable or missing root must not break startup."""
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        sd.SessionDirManager(base_dir=str(ro), source_repo="/repo")
        sd.SessionDirManager(base_dir=str(tmp_path / "missing" / "123"), source_repo="/repo")
    finally:
        ro.chmod(0o700)
    assert not (tmp_path / "missing").exists()


def test_root_marker_written_at_the_sessions_root(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    (root / "111").mkdir(parents=True)
    sd.SessionDirManager(base_dir=str(root / "111"), source_repo="/repo")
    assert (root / sd.SESSIONS_ROOT_MARKER).is_file()
    assert not (root / "111" / sd.SESSIONS_ROOT_MARKER).exists()
