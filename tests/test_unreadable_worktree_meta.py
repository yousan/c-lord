"""Review of 43caee1 — unreadable linked-worktree metadata keeps the workspace.

A submodule's ``.git/modules/<name>/worktrees/<wt>/`` at mode 000:
``refs.is_dir()`` raised ``PermissionError`` (#1198's ``_worktree_tips``), so
/workspace-delete failed with an exception, and the orphan sweep stopped at
that workspace instead of going on to the next. Unknown is kept, and one
workspace that cannot be judged never stops the others.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from c_lord import session_dir as sd
from c_lord.session_dir import SessionDirManager, describe_changes, worktree_status

from .test_submodule_unpushed_1198 import _delete, _workspace, _wt_detached_commit, _wt_meta, git

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")


def test_unreadable_worktree_meta_dir_is_unknown(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    wt, sha = _wt_detached_commit(ws, tmp_path)
    git(wt, "update-ref", "refs/worktree/keep", sha)
    meta = _wt_meta(ws)
    meta.chmod(0)
    try:
        st = worktree_status(str(ws))
        assert not st.clean
        assert "sub: unpushed commits unknown (worktree HEAD unreadable)" in describe_changes(st)
        assert not _delete(ws).removed
    finally:
        meta.chmod(0o755)


def test_one_failing_workspace_does_not_stop_the_orphan_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    base = tmp_path / "sessions"
    for n in ("1", "2"):
        (base / n).mkdir(parents=True)
        git(base / n, "init", "-q")
    real = sd._stash_entries

    def flaky(path: str, **kw: object) -> int | None:
        if path.endswith("/1"):
            raise PermissionError(13, "Permission denied", path)
        return real(path, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(sd, "_stash_entries", flaky)
    results = SessionDirManager(base_dir=str(base), source_repo="/x").cleanup_orphaned(set())
    by_id = {r.thread_id: r for r in results}
    assert set(by_id) == {1, 2}
    assert not by_id[1].removed and (base / "1").exists()
    assert "unknown" in by_id[1].reason
    assert by_id[2].removed and not (base / "2").exists()
