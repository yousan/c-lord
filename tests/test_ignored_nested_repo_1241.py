"""#1241 — a repo cloned into an ignored ``vendor/`` keeps its local-only commits.

The workspace's ``.gitignore`` ignores ``vendor/``, and the user (or Claude)
clones another repository under it and commits there without pushing.
``vendor/`` is on the list of directories a tool rebuilds, so it was skipped as
a whole: /workspace-delete read the workspace as clean and deleted the only
copy of that commit. A tool can rebuild ``vendor/``; it cannot rebuild a commit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from c_lord.session_dir import describe_changes, worktree_status

from .test_submodule_unpushed_1198 import _commit, _delete, _kept, _workspace, git


def _ignore(ws: Path, pattern: str) -> None:
    (ws / ".gitignore").write_text(f"{pattern}\n")
    git(ws, "add", ".gitignore")
    git(ws, "commit", "-qm", "ignore")
    git(ws, "push", "-q", "origin", "HEAD:main")


def test_ignored_vendor_repo_with_local_commits(tmp_path: Path) -> None:
    """A repo cloned into an ignored ``vendor/`` dir with a local-only commit."""
    ws, _ = _workspace(tmp_path)
    _ignore(ws, "vendor/")
    v = ws / "vendor" / "lib"
    v.mkdir(parents=True)
    git(v, "init", "-q")
    _commit(v, "patch.txt")
    res = _kept(ws)
    assert "vendor/lib: 1 unpushed commit(s)" in res.reason, res.reason


def test_ignored_node_modules_repo_with_stash_is_kept(tmp_path: Path) -> None:
    """Same for a stash, and for the ignored dir itself being the repo."""
    ws, subr = _workspace(tmp_path)
    _ignore(ws, "node_modules/")
    nm = ws / "node_modules"
    git(tmp_path, "clone", "-q", str(subr), str(nm))
    (nm / "README.md").write_text("changed\n")
    git(nm, "stash", "-q")
    res = _kept(ws)
    assert "node_modules: 1 stash entry" in res.reason, res.reason


def test_ignored_repo_with_uncommitted_change_is_kept(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    _ignore(ws, "vendor/")
    lib = ws / "vendor" / "lib"
    git(tmp_path, "clone", "-q", str(subr), str(lib))
    (lib / "README.md").write_text("edited, never committed\n")
    res = _kept(ws)
    assert "vendor/lib: uncommitted changes" in res.reason, res.reason


def test_ignored_repo_fully_pushed_is_still_regenerable(tmp_path: Path) -> None:
    """Control: a clean clone of a pushed repo under ``vendor/`` is not work."""
    ws, subr = _workspace(tmp_path)
    _ignore(ws, "vendor/")
    git(tmp_path, "clone", "-q", str(subr), str(ws / "vendor" / "lib"))
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
    assert _delete(ws).removed


def test_ignored_file_inside_an_ignored_repo_is_kept(tmp_path: Path) -> None:
    """Review of 43caee1: the vendor repo's own ignored files (a local ``.env``)
    are in no commit and nowhere else."""
    ws, subr = _workspace(tmp_path)
    _ignore(ws, "vendor/")
    lib = ws / "vendor" / "lib"
    git(tmp_path, "clone", "-q", str(subr), str(lib))
    (lib / ".git" / "info" / "exclude").write_text("/.env\n")
    (lib / ".env").write_text("TOKEN=local\n")
    res = _kept(ws)
    assert "vendor/lib: uncommitted changes" in res.reason, res.reason


def test_regenerable_files_inside_an_ignored_repo_are_not_work(tmp_path: Path) -> None:
    """Control: the same rule as the workspace's own — ``__pycache__`` is rebuilt."""
    ws, subr = _workspace(tmp_path)
    _ignore(ws, "vendor/")
    lib = ws / "vendor" / "lib"
    git(tmp_path, "clone", "-q", str(subr), str(lib))
    (lib / ".git" / "info" / "exclude").write_text("__pycache__/\n")
    (lib / "__pycache__").mkdir()
    (lib / "__pycache__" / "m.pyc").write_bytes(b"\0")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)


def test_repo_whose_origin_is_a_bare_repo_inside_the_workspace_is_kept(tmp_path: Path) -> None:
    """Second review (a): ``vendor/up.git`` is ``vendor/lib``'s origin. Pushing
    there is no backup — it is deleted with the workspace."""
    ws, _ = _workspace(tmp_path)
    _ignore(ws, "vendor/")
    # Deeper than the ignored-dir scan (2 levels): only "inside the workspace" keeps it.
    up = ws / "vendor" / "a" / "b" / "up.git"
    git(tmp_path, "init", "-q", "--bare", str(up))
    lib = ws / "vendor" / "lib"
    git(tmp_path, "clone", "-q", str(up), str(lib))
    _commit(lib, "patch.txt")
    git(lib, "push", "-q", "origin", "HEAD:main")
    res = _kept(ws)
    assert "vendor/lib: 1 unpushed commit(s)" in res.reason, res.reason


def test_undecodable_ignored_file_name_inside_a_vendor_repo_is_kept(tmp_path: Path) -> None:
    """Second review (C): a non-UTF-8 name made ``git status`` output undecodable,
    and the UnicodeDecodeError escaped /workspace-delete."""
    ws, subr = _workspace(tmp_path)
    _ignore(ws, "vendor/")
    lib = ws / "vendor" / "lib"
    git(tmp_path, "clone", "-q", str(subr), str(lib))
    (lib / ".git" / "info" / "exclude").write_text("*.bin\n")
    try:
        with open(os.path.join(os.fsencode(lib), b"caf\xe9.bin"), "wb") as f:
            f.write(b"x")
    except OSError:  # APFS (macOS) refuses non-UTF-8 file names
        pytest.skip("this file system refuses non-UTF-8 file names")
    res = _kept(ws)
    assert "vendor/lib: uncommitted changes" in res.reason, res.reason


def test_slow_nested_repos_cost_one_timeout(tmp_path: Path, monkeypatch) -> None:
    """W57 round 12: each repo in an ignored dir was asked even after the first
    already kept the workspace — N slow repos cost N timeouts."""
    import time

    from c_lord import session_dir as sd

    monkeypatch.setattr(sd._run, "__kwdefaults__", {**sd._run.__kwdefaults__, "timeout": 2.0})
    ws, subr = _workspace(tmp_path)
    (ws / ".git" / "info" / "exclude").write_text("vendor/\n")
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nsleep 30\ncat\n")
    slow.chmod(0o755)
    for i in range(4):
        lib = ws / "vendor" / f"lib{i}"
        git(tmp_path, "clone", "-q", str(subr), str(lib))
        git(lib, "config", "filter.x.clean", str(slow))
        (lib / ".git" / "info" / "attributes").write_text("* filter=x\n")
        future = time.time() + 5
        os.utime(lib / "README.md", (future, future))
    t0 = time.monotonic()
    res = _delete(ws)
    took = time.monotonic() - t0
    assert not res.removed
    assert took < 4.0, f"took {took:.1f}s: {res.reason!r}"
    assert "more not checked" in res.reason, res.reason


def test_bare_repo_with_refs_in_an_ignored_dir_is_kept(tmp_path: Path) -> None:
    """A bare repo under ``build/`` (a push target, a mirror) is deleted with the workspace."""
    ws, subr = _workspace(tmp_path)
    _ignore(ws, "build/")
    git(tmp_path, "clone", "-q", "--bare", str(subr), str(ws / "build" / "x.git"))
    res = _kept(ws)
    assert "build/x.git: bare repository" in res.reason, res.reason
