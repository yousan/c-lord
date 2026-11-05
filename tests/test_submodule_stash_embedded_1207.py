"""#1207 — a stash inside an embedded repo with no ``.gitmodules`` keeps the workspace.

A nested repo that was ``git add``-ed (a bare ``160000`` gitlink, its ``.git``
dir inside the folder, no ``.gitmodules`` entry) holds its own stash. Deleting
the workspace deletes it, and the parent's ``git status`` / ``refs/stash`` never
show it. The stash check only looked at repos with a ``.gitmodules``, so the
workspace read as clean and /workspace-delete removed it with the stash's only
copy (``removed=True reason='clean'``). Same promise as #1121 / #1045.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from c_lord import session_dir as sd
from c_lord.session_dir import SessionDirManager, describe_changes, worktree_status

from .test_workspace_cleanup_reasons import _git, _session_clone


def _stash_in(path: Path) -> None:
    (path / "README.md").write_text("wip\n")
    _git(path, "stash", "-q")


def _delete(ws: Path) -> sd.CleanupResult:
    return SessionDirManager(base_dir=str(ws.parent), source_repo="/repo").cleanup_for_thread(
        int(ws.name)
    )


def _embedded(tmp_path: Path) -> Path:
    """A nested repo added as a bare gitlink (no .gitmodules, .git dir inside).

    A clone, so its commits are on its ``origin`` — a copy of ``seed`` has no
    remote, and its commit then is the only copy (#1198 keeps that).
    """
    ws = _session_clone(tmp_path)
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "origin.git"), str(ws / "emb")], check=True
    )
    _git(ws, "add", "emb")
    _git(ws, "commit", "-qm", "emb")
    _git(ws, "push", "-q", "origin", "main")
    return ws


def test_embedded_repo_without_gitmodules_stash(tmp_path: Path) -> None:
    ws = _embedded(tmp_path)
    _stash_in(ws / "emb")
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)


def test_embedded_repo_without_gitmodules_delete(tmp_path: Path) -> None:
    ws = _embedded(tmp_path)
    _stash_in(ws / "emb")
    r = _delete(ws)
    assert ws.exists(), f"removed={r.removed} reason={r.reason!r}"


def test_embedded_repo_without_a_stash_is_still_clean(tmp_path: Path) -> None:
    ws = _embedded(tmp_path)
    st = worktree_status(str(ws), sessions_root=str(ws.parent))
    assert st.clean, describe_changes(st)


# ---- #1227: the same, one level down — inside a checked-out submodule ----

ALLOW = ("-c", "protocol.file.allow=always")


def _local_repo(path: Path, name: str = "f.txt") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / name).write_text("only copy\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "local only")
    return path


def test_embedded_inside_submodule_stash_is_kept(tmp_path: Path) -> None:
    """#1227: a nested repo with no .gitmodules inside a checked-out submodule.

    #1207 looked for such repos only in the workspace's own index, so this
    stash was never asked about and was deleted with the workspace.
    """
    ws = _session_clone(tmp_path)
    sub_src = _local_repo(tmp_path / "subsrc")
    bare = tmp_path / "sub.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(sub_src), str(bare)], check=True)
    _git(ws, *ALLOW, "submodule", "add", "-q", str(bare), "sub")
    _git(ws, "commit", "-qm", "sub")
    sub = ws / "sub"
    # nested repo inside the submodule, cloned from a remote so its commits are not "only copy"
    inner_bare = tmp_path / "inner.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(sub_src), str(inner_bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(inner_bare), str(sub / "inner")], check=True)
    _git(sub, "add", "inner")
    _git(sub, "commit", "-qm", "inner")
    _git(sub, "push", "-q", "origin", "HEAD:main")
    _git(ws, "add", "sub")
    _git(ws, "commit", "-qm", "bump sub")
    _git(ws, "push", "-q", "origin", "main")
    (sub / "inner" / "f.txt").write_text("wip\n")
    _git(sub / "inner", "stash", "-q")
    st = worktree_status(str(ws))
    r = _delete(ws)
    assert ws.exists(), f"removed={r.removed} reason={r.reason!r} status={describe_changes(st)!r}"
    assert "sub/inner: 1 stash entry" in r.reason
