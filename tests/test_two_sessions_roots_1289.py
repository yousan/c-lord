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


@pytest.mark.parametrize("marker", ["coauthor", "skill"])
@pytest.mark.parametrize("sub", [False, True])
def test_neighbour_workspace_with_clord_markers_does_not_vouch(
    tmp_path: Path, sub: bool, marker: str
) -> None:
    """No root marker (an older instance), but B at ``<root>/<ch>/<thr>`` is c-lord's."""
    root = _seed(tmp_path)
    a = _ws(tmp_path, root / "111", "4242")
    b = _ws(tmp_path, tmp_path / "other-sessions" / "222", "4343")
    p = _sub(sub)
    sha = _work(p(a))
    git(p(b), "fetch", "-q", str(p(a)), "work:work")
    git(p(a), "remote", "add", "b", str(p(b)))
    git(p(a), "fetch", "-q", "b")
    _mark_workspace(b, marker)
    assert not (tmp_path / "other-sessions" / sd.SESSIONS_ROOT_MARKER).exists()
    st = sd.worktree_status(str(a), sessions_root=str(root))
    assert not st.clean, f"A vouched by a c-lord workspace outside its root: {sha}"


def _mark_workspace(ws: Path, marker: str) -> None:
    if marker == "coauthor":
        assert install_coauthor_hook(ws) is not None
    else:
        skill = ws / ".claude" / "skills" / "discord-read"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("x\n")


@pytest.mark.parametrize("marker", ["coauthor", "skill"])
def test_local_source_repo_with_clord_markers_still_vouches(tmp_path: Path, marker: str) -> None:
    """Review of c309374 (over-keep): c-lord also injects into checkouts outside any root.

    A channel bound to a local source repo (``origin=/home/<user>/c-lord``) that
    carries the co-author file or the skill must still vouch, or every untouched
    clone of it would be kept forever.
    """
    _seed(tmp_path)
    src = tmp_path / "home" / "src"
    git(tmp_path, "clone", "-q", str(tmp_path / "parent.git"), str(src))
    _mark_workspace(src, marker)
    root = tmp_path / "sessions"
    mgr = sd.SessionDirManager(base_dir=str(root / "111"), source_repo=str(src))
    ws = Path(mgr.create_session_dir(4242))
    r = mgr.cleanup_for_thread(4242)
    assert not ws.exists(), r.reason


@pytest.mark.parametrize("sub", [False, True])
def test_unmarked_roots_swept_one_by_one_keep_a_copy(tmp_path: Path, sub: bool) -> None:
    """Review of c309374 (loss): no manager was ever built, so neither root is marked.

    Old workspaces carry no c-lord marker. The orphan sweep of each root (it
    builds no ``SessionDirManager``) must mark its own root before it judges,
    so the second sweep does not trust the first one's deleted workspace.
    """
    from c_lord.orphan_dirs import remove_orphan_dir

    root, other, a, b, sha = _mutual(tmp_path, sub)
    assert not (root / sd.SESSIONS_ROOT_MARKER).exists()
    assert not (other / sd.SESSIONS_ROOT_MARKER).exists()
    r = [remove_orphan_dir(a, sessions_root=root), remove_orphan_dir(b, sessions_root=other)]
    assert _alive(a, b), f"prod then staging orphan sweep deleted {sha}: {r}"


class _NoRows:
    async def all_working_dirs(self) -> set[str]:
        return set()


async def test_orphan_sweep_loop_marks_its_root(tmp_path: Path) -> None:
    """The loop's first pass runs 5 min after start: the root is marked at start."""
    from c_lord.orphan_dirs import OrphanSweepLoop

    root = tmp_path / "sessions"
    root.mkdir()
    loop = OrphanSweepLoop(_NoRows(), root, min_idle_days=30, container_scan=_no_containers)
    loop.start()
    try:
        assert (root / sd.SESSIONS_ROOT_MARKER).is_file()
    finally:
        await loop.stop()
    (root / sd.SESSIONS_ROOT_MARKER).unlink()
    await loop.tick()
    assert (root / sd.SESSIONS_ROOT_MARKER).is_file()


async def _no_containers(known: set[str]) -> list:
    return []


def test_thirty_day_sweep_marks_its_root(tmp_path: Path) -> None:
    from c_lord.database.repository import SessionRecord
    from c_lord.session_cleanup import remove_clean_session_dir

    root = tmp_path / "sessions"
    root.mkdir()
    rec = SessionRecord(1, "s", str(root / "1" / "1"), None, "chat", None, "", "")
    remove_clean_session_dir(rec, sessions_root=root)
    assert (root / sd.SESSIONS_ROOT_MARKER).is_file()


async def test_startup_marks_the_session_dir_base(tmp_path: Path, monkeypatch) -> None:
    """An idle bot builds no manager: setup itself marks the root it was given."""
    from .test_setup import _boot_for_base

    base = tmp_path / "sessions"
    base.mkdir()
    await _boot_for_base(tmp_path, monkeypatch, session_dir_base=str(base))
    assert (base / sd.SESSIONS_ROOT_MARKER).is_file()


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
