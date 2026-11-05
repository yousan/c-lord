"""#1242 — an unreadable ``.git/modules`` keeps the workspace, without raising.

Submodule git dirs live in ``<ws>/.git/modules`` (#1181/#1198). When the
permissions there are broken, the check did not fall back to "keep":

- ``.git/modules/<name>`` at mode 000: ``worktree_status`` raised
  ``PermissionError`` (Python 3.13's ``Path.is_file``) — the sweep stopped.
- ``.git/modules`` that cannot be listed (0300): read as clean, so
  /workspace-delete started removing — the work tree went, ``.git/modules``
  could not be removed, and the user was left with half a workspace.

Unknown is work: it is reported and the workspace stays, whole.
"""

from __future__ import annotations

from pathlib import Path

from c_lord.session_dir import describe_changes, worktree_status

from .test_submodule_unpushed_1198 import _commit, _delete, _workspace, git


def _deinit_with_work(ws: Path) -> str:
    git(ws / "sub", "checkout", "-q", "-b", "work")
    sha = _commit(ws / "sub", "w.txt")
    git(ws, "submodule", "deinit", "-q", "-f", "sub")
    return sha


def test_unreadable_module_git_dir_is_kept(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _deinit_with_work(ws)
    mod = ws / ".git" / "modules" / "sub"
    mod.chmod(0)
    try:
        st = worktree_status(str(ws))
        assert not st.clean, f"unreadable module dir read as clean: {st!r}"
        res = _delete(ws)
        assert not res.removed and (ws / "README.md").exists(), res.reason
        assert "sub: modules unknown (unreadable)" in res.reason, res.reason
    finally:
        mod.chmod(0o755)


def test_unreadable_modules_parent_dir_is_kept(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _deinit_with_work(ws)
    mods = ws / ".git" / "modules"
    mods.chmod(0o300)  # cannot list
    try:
        st = worktree_status(str(ws))
        assert not st.clean, f"unlistable .git/modules read as clean: {st!r}"
        res = _delete(ws)
        assert not res.removed, res.reason
        assert (ws / "README.md").exists(), "deletion went partway"
        assert "modules unknown (unreadable)" in describe_changes(st)
    finally:
        if mods.exists():
            mods.chmod(0o755)


def test_unstatable_modules_dir_is_kept(tmp_path: Path) -> None:
    """``.git`` itself searchable but not ``.git/modules``'s parent: ``is_dir`` fails."""
    ws, _ = _workspace(tmp_path)
    _deinit_with_work(ws)
    mods = ws / ".git" / "modules"
    mods.chmod(0o600)  # listable names, but nothing below can be stat'ed
    try:
        st = worktree_status(str(ws))
        assert not st.clean, describe_changes(st)
        assert ws.exists() and not _delete(ws).removed
    finally:
        mods.chmod(0o755)
