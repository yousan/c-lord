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
    st = sd.worktree_status(str(ws), sessions_root=str(tmp_path / "sessions"))
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


# ---- W57 round 12: a merged-and-deleted PR branch, a push of an existing commit ----


def _merge_on_remote(tmp_path: Path, subr: Path, branch: str, *, ff: bool) -> None:
    other = tmp_path / f"merger-{branch}"
    git(tmp_path, "clone", "-q", str(subr), str(other))
    mode = "--ff-only" if ff else "--no-ff"
    git(other, "merge", "-q", mode, "-m", "Merge PR", f"origin/{branch}")
    git(other, "push", "-q", "origin", "main")
    git(other, "push", "-q", "origin", "--delete", branch)


@pytest.mark.parametrize("ff", [True, False], ids=["ff-merge", "merge-commit"])
def test_pr_merged_and_branch_deleted_without_a_fetch_is_deleted(
    tmp_path: Path, monkeypatch, ff: bool
) -> None:
    """GitHub's default flow: commit on ``work``, push, PR merged (merge commit or
    fast-forward) and the branch deleted; the workspace never fetched again. The
    commit is on the remote's ``main`` — the default branch is probed."""
    ws, subr = _workspace(tmp_path)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "-b", "work")
    sha = _commit(sub, "w.txt")
    git(sub, "push", "-q", "origin", "work")
    _record_sub(ws)
    _merge_on_remote(tmp_path, subr, "work", ff=ff)
    assert git(subr, "merge-base", "--is-ancestor", sha, "main") == ""
    net = _net(monkeypatch)
    res = _delete(ws)
    assert res.removed, res.reason
    assert net == {"ls-remote": 1, "fetch": 0 if ff else 1}, net


def test_branch_deleted_upstream_with_its_commit_on_no_other_branch_is_still_kept(
    tmp_path: Path, monkeypatch
) -> None:
    """#1245 itself: the probe of the other heads finds the commit nowhere."""
    ws, subr = _workspace(tmp_path)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "-b", "work")
    _commit(sub, "w.txt")
    git(sub, "push", "-q", "origin", "work")
    _record_sub(ws)
    _upstream_moves(tmp_path)  # main moves on: not here, so it is probed
    git(subr, "update-ref", "-d", "refs/heads/work")
    git(subr, "gc", "-q", "--prune=now")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s) (remote branch deleted or rewritten)" in res.reason


def test_pushed_existing_commit_as_new_branch_then_upstream_moves_is_deleted(
    tmp_path: Path,
) -> None:
    """No commit made here: ``git push origin origin/main:refs/heads/release``,
    then someone pushes to ``release``. The push record makes it verified; the
    move to a commit not here is probed."""
    ws, subr = _workspace(tmp_path)
    sub = ws / "sub"
    git(sub, "push", "-q", "origin", "origin/main:refs/heads/release")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "-b", "release", str(subr), str(other))
    _commit(other, "r.txt")
    git(other, "push", "-q", "origin", "release")
    res = _delete(ws)
    assert res.removed, res.reason


# ---- W57 round 13: the all-heads probe once per remote, a failure once per URL ----


def _closed_prs_with_other_branches(tmp_path: Path, n: int) -> Path:
    """*n* pushed branches closed unmerged (deleted upstream); main moved; 5 other branches."""
    ws, subr = _workspace(tmp_path)
    sub = ws / "sub"
    for i in range(n):
        git(sub, "checkout", "-q", "-b", f"w{i}", "origin/main")
        _commit(sub, f"w{i}.txt")
        git(sub, "push", "-q", "origin", f"w{i}")
    git(sub, "checkout", "-q", "--detach", "origin/main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(subr), str(other))
    for b in range(5):
        git(other, "checkout", "-q", "-B", f"b{b}", "origin/main")
        _commit(other, f"b{b}.txt")
        git(other, "push", "-q", "origin", f"b{b}")
    git(other, "checkout", "-q", "main")
    _commit(other, "mm.txt")
    git(other, "push", "-q", "origin", "main")
    for i in range(n):
        git(other, "push", "-q", "origin", "--delete", f"w{i}")
    git(subr, "gc", "-q", "--prune=now")
    return ws


@pytest.mark.parametrize("n", [1, 3])
def test_all_heads_are_fetched_once_per_remote(tmp_path: Path, monkeypatch, n: int) -> None:
    """W57 round 13: each gone ref re-fetched the default branch and then every branch."""
    ws = _closed_prs_with_other_branches(tmp_path, n)
    net = _net(monkeypatch)
    res = _kept(ws)
    assert "unpushed commit(s) (remote branch deleted or rewritten)" in res.reason, res.reason
    assert net == {"ls-remote": 1, "fetch": 2}, net  # default once, all heads once


def test_a_hanging_probe_costs_one_timeout_per_url(tmp_path: Path, monkeypatch) -> None:
    """The default-branch probe hangs: no all-heads probe after it, and the next
    sweep within the TTL does not fetch again."""
    monkeypatch.setattr(sd, "_LS_REMOTE_TIMEOUT_SECONDS", 2.0)
    ws = _closed_prs_with_other_branches(tmp_path, 1)
    real = sd._run
    fetches = {"n": 0}

    def hang(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if "fetch" in cmd:
            fetches["n"] += 1
            cmd = ["git", "-c", "alias.zz=!sleep 30", "zz"]
        return real(cmd, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(sd, "_run", hang)
    assert not _kept(ws).removed
    assert not _kept(ws).removed
    assert fetches["n"] == 1, fetches
