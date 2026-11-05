"""#1198 — commits in a submodule with no checkout keep the workspace.

After ``git submodule deinit`` or ``git rm`` the submodule's folder has no
``.git``, but its git dir ``<ws>/.git/modules/<name>`` stays — and with it any
commit made there that was never pushed. #1181 started looking at those git
dirs, but only for stashes: a work branch with a commit on it was judged clean,
and /workspace-delete removed the only copy of that commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from c_lord import session_dir as sd
from c_lord.session_dir import describe_changes, worktree_status

from .test_submodule_stash_1121 import _delete, _with_submodule
from .test_workspace_cleanup_reasons import _git


def _commit_on_work_branch(sub: Path) -> None:
    _git(sub, "checkout", "-q", "-b", "work")
    (sub / "new.txt").write_text("only here\n")
    _git(sub, "add", "new.txt")
    _git(sub, "commit", "-qm", "unpushed work")


def test_deinit_submodule_with_unpushed_commit_is_kept(tmp_path: Path) -> None:
    ws = _with_submodule(tmp_path)
    _commit_on_work_branch(ws / "sub")
    _git(ws, "submodule", "deinit", "-q", "-f", "sub")
    assert not (ws / "sub" / ".git").exists()
    st = worktree_status(str(ws))
    assert not st.clean, f"clean={st.clean} why={describe_changes(st)!r}"
    assert "sub: 1 unpushed commit(s)" in describe_changes(st)
    r = _delete(ws)
    assert ws.exists(), f"removed={r.removed} reason={r.reason!r}"
    assert "sub: 1 unpushed commit(s)" in r.reason


def test_rm_submodule_with_unpushed_commit_is_kept(tmp_path: Path) -> None:
    ws = _with_submodule(tmp_path)
    _commit_on_work_branch(ws / "sub")
    _git(ws, "rm", "-q", "-f", "sub")
    _git(ws, "commit", "-qm", "drop sub")
    _git(ws, "push", "-q", "origin", "main")
    st = worktree_status(str(ws))
    assert not st.clean, f"clean={st.clean} why={describe_changes(st)!r}"
    r = _delete(ws)
    assert ws.exists(), f"removed={r.removed} reason={r.reason!r}"
    assert "sub: 1 unpushed commit(s)" in r.reason


def test_deinit_submodule_with_detached_unpushed_commit_is_kept(tmp_path: Path) -> None:
    """A commit on a detached HEAD is on no branch at all — still work."""
    ws = _with_submodule(tmp_path)
    sub = ws / "sub"
    (sub / "new.txt").write_text("detached\n")
    _git(sub, "add", "new.txt")
    _git(sub, "commit", "-qm", "detached work")
    _git(ws, "submodule", "deinit", "-q", "-f", "sub")
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)
    assert "sub: 1 unpushed commit(s)" in describe_changes(st)


def test_deinit_submodule_with_pushed_branch_is_clean(tmp_path: Path) -> None:
    """A local branch whose commits are on a remote-tracking ref is nothing to lose."""
    ws = _with_submodule(tmp_path)
    _git(ws / "sub", "checkout", "-q", "-b", "work")  # same commit as origin/main
    _git(ws, "submodule", "deinit", "-q", "-f", "sub")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)


def test_git_failure_in_module_dir_counts_as_work(tmp_path: Path, monkeypatch) -> None:
    """When git cannot count the module's commits, the workspace is kept."""
    ws = _with_submodule(tmp_path)
    _git(ws, "submodule", "deinit", "-q", "-f", "sub")
    real = sd._run

    def fake(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if any(a.startswith("--git-dir=") for a in cmd) and "rev-list" in cmd and "--not" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal")
        return real(cmd, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(sd, "_run", fake)
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)
    assert "sub: unpushed commits unknown" in describe_changes(st)
