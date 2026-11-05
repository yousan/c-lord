"""#1241 — a repo cloned into an ignored ``vendor/`` keeps its local-only commits.

The workspace's ``.gitignore`` ignores ``vendor/``, and the user (or Claude)
clones another repository under it and commits there without pushing.
``vendor/`` is on the list of directories a tool rebuilds, so it was skipped as
a whole: /workspace-delete read the workspace as clean and deleted the only
copy of that commit. A tool can rebuild ``vendor/``; it cannot rebuild a commit.
"""

from __future__ import annotations

from pathlib import Path

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
