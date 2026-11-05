"""#1207 — a stash inside an embedded repo with no ``.gitmodules`` keeps the workspace.

A nested repo that was ``git add``-ed (a bare ``160000`` gitlink, its ``.git``
dir inside the folder, no ``.gitmodules`` entry) holds its own stash. Deleting
the workspace deletes it, and the parent's ``git status`` / ``refs/stash`` never
show it. The stash check only looked at repos with a ``.gitmodules``, so the
workspace read as clean and /workspace-delete removed it with the stash's only
copy (``removed=True reason='clean'``). Same promise as #1121 / #1045.
"""

from __future__ import annotations

import shutil
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
    """A nested repo added as a bare gitlink (no .gitmodules, .git dir inside)."""
    ws = _session_clone(tmp_path)
    shutil.copytree(tmp_path / "seed", ws / "emb")
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
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
