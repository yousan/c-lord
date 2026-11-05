"""#1121 — a stash that exists only inside a submodule keeps the workspace.

The submodule's stash lives in ``<ws>/.git/modules/<name>``: deleting the
workspace deletes it, and the parent's ``git status`` / ``refs/stash`` never
show it. /workspace-cleanup, /workspace-delete, the 30-day sweep and the orphan
sweep all promise to keep a folder with stashed work (#1045, #1080).

#1181: the stash outlives the submodule's checkout. After ``git submodule
deinit sub`` or ``git rm sub`` the folder ``sub`` has no ``.git`` any more, but
``.git/modules/sub`` still holds the stash — and the workspace was judged clean
and deleted with it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from c_lord import session_dir as sd
from c_lord.session_dir import SessionDirManager, describe_changes, worktree_status

from .test_workspace_cleanup_reasons import _GIT_ENV, _git, _session_clone


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


# ---- #1181: the stash stays in .git/modules/<name> without a checkout ----


def _stash_in(path: Path) -> None:
    (path / "README.md").write_text("wip\n")
    _git(path, "stash", "-q")


def _module_stash(ws: Path, tmp_path: Path) -> str:
    gd = str(ws / ".git" / "modules" / "sub")
    return _git(
        tmp_path,
        "--git-dir",
        gd,
        "--work-tree",
        str(tmp_path),
        "rev-parse",
        "-q",
        "--verify",
        "refs/stash",
    ).strip()


def _delete(ws: Path) -> sd.CleanupResult:
    return SessionDirManager(base_dir=str(ws.parent), source_repo="/repo").cleanup_for_thread(
        int(ws.name)
    )


def test_deinit_submodule_with_stash_is_kept(tmp_path: Path) -> None:
    """``git submodule deinit`` removes the checkout; the stash stays in .git/modules/sub."""
    ws = _with_submodule(tmp_path)
    _stash_in(ws / "sub")
    _git(ws, "submodule", "deinit", "-q", "sub")
    assert (ws / ".git" / "modules" / "sub").is_dir()
    assert _module_stash(ws, tmp_path)  # still there
    r = _delete(ws)
    assert ws.exists(), f"removed={r.removed} reason={r.reason!r}"
    assert "sub: 1 stash entry" in r.reason


def test_rm_submodule_with_stash_is_kept(tmp_path: Path) -> None:
    """``git rm sub`` + commit + push: gitlink and .gitmodules gone, stash stays in modules/."""
    ws = _with_submodule(tmp_path)
    _stash_in(ws / "sub")
    _git(ws, "rm", "-q", "sub")
    _git(ws, "commit", "-qm", "drop sub")
    _git(ws, "push", "-q", "origin", "main")
    assert _module_stash(ws, tmp_path)
    r = _delete(ws)
    assert ws.exists(), f"removed={r.removed} reason={r.reason!r}"


def test_removed_submodule_without_stash_is_clean(tmp_path: Path) -> None:
    """A leftover .git/modules/sub with nothing stashed is no reason to keep the dir."""
    ws = _with_submodule(tmp_path)
    _git(ws, "submodule", "deinit", "-q", "sub")
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)


def test_stash_counted_once_for_a_checked_out_submodule(tmp_path: Path) -> None:
    ws = _with_submodule(tmp_path)
    _stash_in(ws / "sub")
    st = worktree_status(str(ws))
    assert st.submodule_stashes == ("sub: 1 stash entry",)


def test_ls_files_failure_still_finds_submodule_stash(tmp_path: Path, monkeypatch) -> None:
    """When ``ls-files`` fails the stash is still found through .git/modules."""
    ws = _with_submodule(tmp_path)
    _stash_in(ws / "sub")
    real = sd._run

    def fake(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["git", "ls-files"]:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal")
        return real(cmd, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(sd, "_run", fake)
    st = worktree_status(str(ws))
    assert not st.clean, describe_changes(st)


def test_uninitialized_submodule_is_clean(tmp_path: Path) -> None:
    """A fresh clone without --recurse-submodules: no modules dir, nothing to lose."""
    _with_submodule(tmp_path)
    clone = tmp_path / "c2" / "1"
    clone.parent.mkdir()
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "origin.git"), str(clone)], check=True, env=_GIT_ENV
    )
    assert not (clone / ".git" / "modules").exists()
    assert worktree_status(str(clone)).clean


@pytest.mark.parametrize("n", [1, 5])
def test_no_extra_git_calls_for_checked_out_submodules(tmp_path: Path, monkeypatch, n: int) -> None:
    """The cost stays linear and local: 1 + 3 calls per checked-out submodule.

    One ``ls-files`` for the workspace, then per submodule one ``rev-parse``
    (stash), one ``rev-list`` (unpushed commits, #1198) and one ``ls-files`` for
    nested repos (#1227). #1181 kept this to the stash call alone, but #1198
    showed a checked-out submodule's commits can be lost with the parent's
    ``git status`` empty — not deleting wins over the call count.
    """
    ws = _session_clone(tmp_path)
    for i in range(n):
        _git(
            ws,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(tmp_path / "seed"),
            f"s{i}",
        )
    _git(ws, "commit", "-qm", "subs")
    calls: list[list[str]] = []
    real = sd._run

    def spy(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return real(cmd, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(sd, "_run", spy)
    sd._submodule_stashes(str(ws))
    assert len(calls) == 1 + 3 * n  # ls-files + (rev-parse, rev-list, ls-files) per submodule
    assert not any("ls-remote" in c for c in calls)  # no network


def _repo(tmp_path: Path, name: str) -> Path:
    r = tmp_path / name
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f.txt").write_text(name + "\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", name)
    return r


def test_nested_submodule_stash_is_named_once(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)
    allow = ("-c", "protocol.file.allow=always")
    inner = _repo(tmp_path, "inner")
    mid = _repo(tmp_path, "mid")
    _git(mid, *allow, "submodule", "add", "-q", str(inner), "inner")
    _git(mid, "commit", "-qm", "inner")
    _git(ws, *allow, "submodule", "add", "-q", str(mid), "mid")
    _git(ws, *allow, "submodule", "update", "--init", "--recursive", "-q")
    _git(ws, "commit", "-qm", "mid")
    _git(ws, "push", "-q", "origin", "main")
    assert worktree_status(str(ws)).clean
    (ws / "mid" / "inner" / "f.txt").write_text("wip\n")
    _git(ws / "mid" / "inner", "stash", "-q")
    assert worktree_status(str(ws)).submodule_stashes == ("mid/inner: 1 stash entry",)
