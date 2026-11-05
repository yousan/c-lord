"""#1245, review of 28f29b3 — what counts as "created here", and a bounded fetch for the PR flow.

- A commit whose *subject* says "fast-forward" or "(start)" was read as a
  fast-forward / rebase start and not counted as made here (n1, n2): pushed by
  URL, fetched, branch deleted upstream — the only copy was deleted.
- A stash commit is made here too (n6).
- ``rebase (finish)`` writes the upstream tip, not a new commit: counting it
  kept an untouched submodule synced with ``git rebase origin/main`` (n9).
- The normal PR flow (push ``work``, PR merged, branch deleted, ``pull``) then
  any later push to upstream ``main`` left "moved to a commit not here" forever
  (d5, d6). Now that one ref is fetched once into a throwaway ref and the old
  tip checked as an ancestor — no tags, no other ref moves, the probe ref is
  removed afterwards.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from c_lord import session_dir as sd

from .test_submodule_unpushed_1198 import _commit, _delete, _kept, _workspace, git


@pytest.fixture(autouse=True)
def _fresh_ls_remote_cache():
    sd._ls_remote_cache.clear()
    yield
    sd._ls_remote_cache.clear()


def _net(monkeypatch) -> dict[str, int]:  # noqa: ANN001
    counts = {"ls-remote": 0, "fetch": 0}
    real = sd._run

    def spy(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        for k in counts:
            if k in cmd:
                counts[k] += 1
        return real(cmd, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(sd, "_run", spy)
    return counts


def _commit_msg(repo: Path, msg: str) -> str:
    (repo / "w.txt").write_text(msg + "\n")
    git(repo, "add", "w.txt")
    git(repo, "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD").strip()


def _by_url_then_lost(ws: Path, subr: Path) -> None:
    sub = ws / "sub"
    git(sub, "push", "-q", str(subr), "work")
    git(sub, "fetch", "-q", "origin")
    git(sub, "checkout", "-q", "--detach", "origin/main")
    git(sub, "branch", "-q", "-D", "work")
    git(subr, "update-ref", "-d", "refs/heads/work")
    git(subr, "gc", "-q", "--prune=now")


@pytest.mark.parametrize(
    "subject", ["Support fast-forward merges", "(start) the importer", "Note (finish) step"]
)
def test_n1_n2_subject_words_do_not_hide_a_commit_made_here(tmp_path: Path, subject: str) -> None:
    ws, subr = _workspace(tmp_path)
    git(ws / "sub", "checkout", "-q", "-b", "work")
    _commit_msg(ws / "sub", subject)
    _by_url_then_lost(ws, subr)
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_n6_stash_commits_are_made_here(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    (ws / "sub" / "README.md").write_text("wip\n")
    git(ws / "sub", "stash", "-q")
    stash = git(ws / "sub", "rev-parse", "stash@{0}").strip()
    created = sd._created_here(ws / ".git" / "modules" / "sub", True)
    assert created is not None and stash in created


def _record_sub(ws: Path) -> None:
    """The parent records where the submodule now is, and pushes it (not its work)."""
    git(ws, "add", "sub")
    git(ws, "commit", "-qm", "bump sub")
    git(ws, "push", "-q", "origin", "HEAD:main")


def _upstream_moves(tmp_path: Path) -> None:
    seed = tmp_path / "subremote-seed"
    git(seed, "checkout", "-q", "main")
    git(seed, "pull", "-q", "--ff-only", "origin", "main")
    _commit(seed, f"later-{len(list(seed.iterdir()))}.txt")
    git(seed, "push", "-q", "origin", "HEAD:main")


def test_n9_synced_by_rebase_with_no_local_commits_is_deleted_offline(
    tmp_path: Path, monkeypatch
) -> None:
    ws, _ = _workspace(tmp_path)
    _upstream_moves(tmp_path)
    sub = ws / "sub"
    git(sub, "fetch", "-q", "origin")
    git(sub, "rebase", "-q", "origin/main")
    _record_sub(ws)
    _upstream_moves(tmp_path)
    net = _net(monkeypatch)
    assert _delete(ws).removed
    assert net == {"ls-remote": 0, "fetch": 0}, net


def _pr_flow(tmp_path: Path, ws: Path, *, ff: bool) -> str:
    """Commit on work, push, PR merged upstream, branch deleted, checkout main && pull."""
    sub = ws / "sub"
    git(sub, "checkout", "-q", "-b", "work")
    sha = _commit(sub, "feature.txt")
    git(sub, "push", "-q", "origin", "work")
    seed = tmp_path / "subremote-seed"
    git(seed, "fetch", "-q", "origin")
    git(seed, "checkout", "-q", "main")
    git(seed, "pull", "-q", "--ff-only", "origin", "main")
    if ff:
        git(seed, "merge", "-q", "--ff-only", "origin/work")
    else:
        git(seed, "merge", "-q", "--no-ff", "-m", "Merge PR", "origin/work")
    git(seed, "push", "-q", "origin", "HEAD:main", ":work")
    git(sub, "checkout", "-q", "main")
    git(sub, "pull", "-q", "--ff-only")
    git(sub, "branch", "-q", "-D", "work")
    _record_sub(ws)
    return sha


@pytest.mark.parametrize("ff", [False, True], ids=["merge-commit", "fast-forward"])
def test_d5_pr_flow_then_upstream_moves_is_deleted_after_one_fetch(
    tmp_path: Path, monkeypatch, ff: bool
) -> None:
    ws, _ = _workspace(tmp_path)
    _pr_flow(tmp_path, ws, ff=ff)
    _upstream_moves(tmp_path)
    net = _net(monkeypatch)
    res = _delete(ws)
    assert res.removed, res.reason
    assert net == {"ls-remote": 1, "fetch": 1}, net


def test_d5_pr_flow_with_upstream_unchanged_asks_once(tmp_path: Path, monkeypatch) -> None:
    ws, _ = _workspace(tmp_path)
    _pr_flow(tmp_path, ws, ff=False)
    net = _net(monkeypatch)
    assert _delete(ws).removed
    assert net == {"ls-remote": 1, "fetch": 0}, net


def test_d6_pushed_to_main_then_upstream_moves_is_deleted(tmp_path: Path, monkeypatch) -> None:
    """Committed on ``main`` and pushed straight to it; someone pushed on top."""
    ws, _ = _workspace(tmp_path)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "main")
    _commit(sub, "direct.txt")
    git(sub, "push", "-q", "origin", "main")
    _record_sub(ws)
    _upstream_moves(tmp_path)
    net = _net(monkeypatch)
    assert _delete(ws).removed
    assert net == {"ls-remote": 1, "fetch": 1}, net


def test_probe_ref_is_removed_and_no_other_ref_moves(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _pr_flow(tmp_path, ws, ff=False)
    _upstream_moves(tmp_path)
    sub = ws / "sub"
    before = git(sub, "for-each-ref", "--format=%(refname) %(objectname)")
    tags_before = git(sub, "tag")
    st = sd.worktree_status(str(ws))
    assert st.clean, sd.describe_changes(st)
    assert git(sub, "for-each-ref", "--format=%(refname) %(objectname)") == before
    assert git(sub, "tag") == tags_before
    assert "clord-probe" not in git(sub, "for-each-ref")


def test_force_pushed_to_unrelated_history_is_kept(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "-b", "work")
    _commit(sub, "w.txt")
    git(sub, "push", "-q", "origin", "work")
    git(sub, "checkout", "-q", "--detach", "origin/main")
    git(sub, "branch", "-q", "-D", "work")
    seed = tmp_path / "subremote-seed"
    git(seed, "checkout", "-q", "--orphan", "other")
    _commit(seed, "unrelated.txt")
    git(seed, "push", "-q", "-f", "origin", "HEAD:work")
    assert not _kept(ws).removed


def test_a_probe_fetch_that_fails_keeps(tmp_path: Path, monkeypatch) -> None:
    ws, _ = _workspace(tmp_path)
    _pr_flow(tmp_path, ws, ff=False)
    _upstream_moves(tmp_path)
    real = sd._run

    def failing_fetch(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if "fetch" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: unreachable")
        return real(cmd, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(sd, "_run", failing_fetch)
    res = _kept(ws)
    assert "unknown" in res.reason, res.reason
