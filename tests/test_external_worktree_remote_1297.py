"""#1297 — a remote that is a linked worktree of a workspace must not vouch for anything.

Workspaces A and B in one sessions root each ran ``git worktree add /tmp/...``
and used the *other one's* worktree as a remote. The worktree directory is
outside every c-lord root, so it vouched — but a linked worktree keeps its
objects and refs in the ``.git`` of the workspace it came from, which the same
sweep deletes. One orphan sweep deleted both A and B as "clean", and the only
copy of the unpushed work was gone.

A local-path remote is now judged by where its objects actually live: the
``gitdir:`` a ``.git`` file points at, that git dir's ``commondir``, and any
``objects/info/alternates`` it borrows from. If any of those is inside the
workspace, under the sessions root, or in a c-lord workspace, the remote is no
copy. A plain local mirror and a linked worktree of a repo outside every root
still vouch.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from c_lord import session_dir as sd

from .test_submodule_unpushed_1198 import _commit, _workspace, git


def _seed(tmp: Path) -> None:
    seed, _ = _workspace(tmp)
    shutil.rmtree(seed)


def _ws(tmp: Path, parent: Path, name: str, trace: bool = True) -> Path:
    ws = parent / name
    parent.mkdir(parents=True, exist_ok=True)
    git(tmp, "clone", "-q", "--single-branch", str(tmp / "parent.git"), str(ws))
    git(ws, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    if trace:
        skill = ws / ".claude" / "skills" / "discord-read"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("x\n")
        git(ws, "config", "--local", "core.excludesFile", "/dev/null")
        (ws / ".git" / "info").mkdir(exist_ok=True)
        with open(ws / ".git" / "info" / "exclude", "a") as f:
            f.write(".claude/\n")
    return ws


def _work(repo: Path) -> str:
    git(repo, "checkout", "-q", "-b", "work")
    sha = _commit(repo, "w.txt", "only copy\n")
    git(repo, "checkout", "-q", "main")
    return sha


def _has(sha: str, *repos: Path) -> bool:
    for r in repos:
        if r.exists() and os.system(f"git -C {r} cat-file -e {sha} 2>/dev/null") == 0:  # noqa: S605
            return True
    return False


def _sweep(root: Path) -> list:
    mgr = sd.SessionDirManager(
        base_dir=str(root / "111"), source_repo="/repo", sessions_root=str(root)
    )
    return [(x.thread_id, x.removed, x.reason) for x in mgr.cleanup_orphaned(set())]


def test_external_linked_worktree_cycle(tmp_path: Path) -> None:
    """The Issue's repro: A <-> B through each other's worktrees outside the root."""
    _seed(tmp_path)
    root = tmp_path / "c-lord-sessions"
    a = _ws(tmp_path, root / "111", "4242")
    b = _ws(tmp_path, root / "111", "4343")
    sha = _work(a)
    git(b, "fetch", "-q", str(a), "work:work")
    wb = tmp_path / "ext" / "wb"
    wa = tmp_path / "ext" / "wa"
    git(b, "worktree", "add", "-q", "--detach", str(wb))
    git(a, "worktree", "add", "-q", "--detach", str(wa))
    git(a, "remote", "add", "b", str(wb))
    git(a, "fetch", "-q", "b")
    git(b, "remote", "add", "a", str(wa))
    git(b, "fetch", "-q", "a")
    git(b, "branch", "-q", "-D", "work")
    git(b, "branch", "-q", "work", "a/work")
    r = _sweep(root)
    assert _has(sha, a, b), f"{sha} lost: {r}"


def test_own_external_worktree_is_no_copy(tmp_path: Path) -> None:
    """A remote that is this workspace's own worktree outside the root vouches for nothing."""
    _seed(tmp_path)
    root = tmp_path / "c-lord-sessions"
    a = _ws(tmp_path, root / "111", "4242")
    wa = tmp_path / "ext" / "wa"
    git(a, "worktree", "add", "-q", "--detach", str(wa))
    assert sd._no_copy(str(a), str(wa), str(a), str(root))


def test_alternates_into_a_workspace_is_no_copy(tmp_path: Path) -> None:
    """A bare repo outside the root that borrows a workspace's objects is no copy."""
    _seed(tmp_path)
    root = tmp_path / "c-lord-sessions"
    a = _ws(tmp_path, root / "111", "4242")
    b = _ws(tmp_path, root / "111", "4343")
    borrowed = tmp_path / "ext" / "shared.git"
    git(tmp_path, "clone", "-q", "--bare", "--shared", str(a), str(borrowed))
    assert (borrowed / "objects" / "info" / "alternates").exists()
    assert sd._no_copy(str(b), str(borrowed), str(b), str(root))


def test_plain_local_mirror_still_vouches(tmp_path: Path) -> None:
    """Over-keep check: a bare mirror outside every root is a real copy."""
    _seed(tmp_path)
    root = tmp_path / "c-lord-sessions"
    a = _ws(tmp_path, root / "111", "4242")
    mirror = tmp_path / "srv" / "git" / "x.git"
    git(tmp_path, "init", "-q", "--bare", str(mirror))
    _work(a)
    git(a, "remote", "add", "x", str(mirror))
    git(a, "push", "-q", "x", "work")
    assert not sd._no_copy(str(a), str(mirror), str(a), str(root))
    r = _sweep(root)
    assert not a.exists(), r


def test_linked_worktree_of_an_outside_repo_still_vouches(tmp_path: Path) -> None:
    """Over-keep check: a worktree of ~/src/project (outside every root) is a real copy."""
    _seed(tmp_path)
    root = tmp_path / "c-lord-sessions"
    a = _ws(tmp_path, root / "111", "4242")
    src = tmp_path / "home" / "src" / "project"
    git(tmp_path, "clone", "-q", str(tmp_path / "parent.git"), str(src))
    wt = tmp_path / "home" / "wt"
    git(src, "worktree", "add", "-q", "--detach", str(wt))
    _work(a)
    git(a, "remote", "add", "x", str(wt))
    git(a, "push", "-q", "x", "work")
    assert not sd._no_copy(str(a), str(wt), str(a), str(root))
    r = _sweep(root)
    assert not a.exists(), r


# Review of b7720f4: other shapes git accepts for the same remote.


def _cycle(tmp: Path, remote_of) -> tuple[str, Path, Path, list]:
    """A <-> B through external worktrees; *remote_of(worktree)* is the URL each one uses."""
    _seed(tmp)
    root = tmp / "c-lord-sessions"
    a = _ws(tmp, root / "111", "4242")
    b = _ws(tmp, root / "111", "4343")
    sha = _work(a)
    git(b, "fetch", "-q", str(a), "work:work")
    wb, wa = remote_of(tmp, b, "wb"), remote_of(tmp, a, "wa")
    git(a, "remote", "add", "b", wb)
    git(a, "fetch", "-q", "b")
    git(b, "remote", "add", "a", wa)
    git(b, "fetch", "-q", "a")
    git(b, "branch", "-q", "-D", "work")
    git(b, "branch", "-q", "work", "a/work")
    return sha, a, b, _sweep(root)


def _gitfile_url(tmp: Path, ws: Path, name: str) -> str:
    wt = tmp / "ext" / name
    git(ws, "worktree", "add", "-q", "--detach", str(wt))
    return str(wt / ".git")  # the gitdir FILE itself


def _suffix_url(tmp: Path, ws: Path, name: str) -> str:
    wt = tmp / "ext" / f"{name}.git"
    git(ws, "worktree", "add", "-q", "--detach", str(wt))
    return str(tmp / "ext" / name)  # git tries <path>.git too


def _symlinked_git_dir_url(tmp: Path, ws: Path, name: str) -> str:
    wt = tmp / "ext" / name
    wt.mkdir(parents=True)
    (wt / ".git").symlink_to(ws / ".git")
    return str(wt)


def test_cycle_through_the_gitdir_file(tmp_path: Path) -> None:
    sha, a, b, r = _cycle(tmp_path, _gitfile_url)
    assert _has(sha, a, b), f"{sha} lost: {r}"


def test_cycle_through_git_suffix_fallback(tmp_path: Path) -> None:
    sha, a, b, r = _cycle(tmp_path, _suffix_url)
    assert _has(sha, a, b), f"{sha} lost: {r}"


def test_cycle_through_symlinked_git_dir(tmp_path: Path) -> None:
    sha, a, b, r = _cycle(tmp_path, _symlinked_git_dir_url)
    assert _has(sha, a, b), f"{sha} lost: {r}"


def _neighbours(tmp: Path) -> tuple[Path, Path, Path]:
    _seed(tmp)
    root = tmp / "c-lord-sessions"
    return root, _ws(tmp, root / "111", "4242"), _ws(tmp, root / "111", "4343")


def test_gitdir_file_url_is_no_copy(tmp_path: Path) -> None:
    root, a, b = _neighbours(tmp_path)
    url = _gitfile_url(tmp_path, a, "wa")
    assert sd._no_copy(str(b), url, str(b), str(root))


def test_symlinked_objects_dir_is_no_copy(tmp_path: Path) -> None:
    root, a, b = _neighbours(tmp_path)
    bare = tmp_path / "ext" / "x.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    shutil.rmtree(bare / "objects")
    (bare / "objects").symlink_to(a / ".git" / "objects")
    assert sd._no_copy(str(b), str(bare), str(b), str(root))


def test_symlinked_refs_dir_is_no_copy(tmp_path: Path) -> None:
    root, a, b = _neighbours(tmp_path)
    bare = tmp_path / "ext" / "x.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    shutil.rmtree(bare / "refs")
    (bare / "refs").symlink_to(a / ".git" / "refs")
    assert sd._no_copy(str(b), str(bare), str(b), str(root))


def test_quoted_alternates_line_is_no_copy(tmp_path: Path) -> None:
    root, a, b = _neighbours(tmp_path)
    bare = tmp_path / "ext" / "x.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    (bare / "objects" / "info" / "alternates").write_text(f'"{a / ".git" / "objects"}"\n')
    assert sd._no_copy(str(b), str(bare), str(b), str(root))


def test_nul_in_gitfile_or_alternates_is_no_copy(tmp_path: Path) -> None:
    root, _a, b = _neighbours(tmp_path)
    wt = tmp_path / "ext" / "w"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /x\x00y\n")
    assert sd._no_copy(str(b), str(wt), str(b), str(root))
    bare = tmp_path / "ext" / "x.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    (bare / "objects" / "info" / "alternates").write_text("/x\x00y\n")
    assert sd._no_copy(str(b), str(bare), str(b), str(root))


def test_mirror_borrowing_an_outside_cache_still_vouches(tmp_path: Path) -> None:
    """Over-keep check: ``--reference`` / ``--shared`` caches outside every root are real copies."""
    root, a, _b = _neighbours(tmp_path)
    cache = tmp_path / "cache" / "x.git"
    git(tmp_path, "clone", "-q", "--bare", str(tmp_path / "parent.git"), str(cache))
    ref = tmp_path / "srv" / "ref.git"
    git(tmp_path, "clone", "-q", "--bare", "--reference", str(cache), str(cache), str(ref))
    shared = tmp_path / "srv" / "shared.git"
    git(tmp_path, "clone", "-q", "--bare", "--shared", str(cache), str(shared))
    for mirror in (ref, shared):
        assert (mirror / "objects" / "info" / "alternates").exists()
        assert not sd._no_copy(str(a), str(mirror), str(a), str(root)), mirror
    _work(a)
    git(a, "remote", "add", "x", str(ref))
    git(a, "push", "-q", "x", "work")
    r = _sweep(root)
    assert not a.exists(), r
