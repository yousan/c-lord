"""#1121 — a stash that exists only inside a submodule keeps the workspace.

The submodule's stash lives in ``<ws>/.git/modules/<name>``: deleting the
workspace deletes it, and the parent's ``git status`` / ``refs/stash`` never
show it. /workspace-cleanup, /workspace-delete, the 30-day sweep and the orphan
sweep all promise to keep a folder with stashed work (#1045, #1080).
"""

from __future__ import annotations

from pathlib import Path

from c_lord.session_dir import describe_changes, worktree_status

from .test_workspace_cleanup_reasons import _git, _session_clone


def _with_submodule(tmp_path: Path) -> Path:
    ws = _session_clone(tmp_path)
    sub_origin = tmp_path / "seed"
    _git(ws, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_origin), "sub")
    _git(ws, "commit", "-qm", "sub")
    _git(ws, "push", "-q", "origin", "main")
    return ws


def test_stash_inside_a_submodule_keeps_the_workspace(tmp_path: Path) -> None:
    ws = _with_submodule(tmp_path)
    (ws / "sub" / "README.md").write_text("work in progress\n")
    _git(ws / "sub", "stash", "-q")
    st = worktree_status(str(ws))
    # The stash lives in ws/.git/modules/sub — deleting ws deletes it.
    assert not st.clean, f"clean={st.clean} why={describe_changes(st)!r}"
    assert "sub: 1 stash entry" in describe_changes(st)


def test_a_submodule_without_a_stash_is_still_clean(tmp_path: Path) -> None:
    ws = _with_submodule(tmp_path)
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
