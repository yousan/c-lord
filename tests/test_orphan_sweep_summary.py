"""#1250 — the orphan sweep summary calls unpushed commits "uncommitted user work".

The summary line exists (#749) so an operator can tell *why* directories are kept
and decide whether one may be deleted by hand. In production (2026-11-05, 13 sweeps,
every one alike) it said ``kept 10 (10 with uncommitted user work, ...)`` while the
per-directory lines said 8 of the 10 had a clean worktree and were kept only for
commits that were never pushed:

    orphan sweep: keeping <sessions>/<ch>/<thr> — 2 unpushed commit(s) on no remote branch
    orphan sweep: removed 0 directory(ies) (0.0 GB; 0 held only c-lord's own files),
        kept 10 (10 with uncommitted user work, 0 not a git repo, 0 unsafe or failed to remove)

Nothing was lost — the keep decision was right — but the reason was misreported.
Each kept directory is counted under one reason: uncommitted files win when both
are present, the rest (unpushed commits, stashes) get their own count.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from c_lord import orphan_dirs
from c_lord.orphan_dirs import OrphanSweepResult, sweep_orphan_dirs
from c_lord.session_dir import WorktreeStatus

DAY = 86400.0


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(path),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )


def _age(path: Path, days: float) -> None:
    when = time.time() - days * DAY
    for p in (path, path / ".git"):
        os.utime(p, (when, when))


def _pushed_clone(ws: Path) -> Path:
    ws.mkdir(parents=True)
    (ws / "README.md").write_text("hello\n")
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "init")
    _git(ws, "update-ref", "refs/remotes/origin/main", "HEAD")
    return ws


def _local_commits(ws: Path, n: int) -> None:
    for i in range(n):
        (ws / f"f{i}.txt").write_text("x\n")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-qm", f"local {i}")


@pytest.mark.asyncio
async def test_summary_does_not_call_unpushed_commits_uncommitted_work(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ws = _pushed_clone(tmp_path / "1440498354797674496" / "1505951360342818887")
    # two local commits never pushed — worktree stays clean
    _local_commits(ws, 2)
    _age(ws, 40)

    with caplog.at_level(logging.INFO, logger="c_lord.orphan_dirs"):
        result = await sweep_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)

    assert result.kept == 1 and ws.exists()
    keep_line = [r.getMessage() for r in caplog.records if "keeping" in r.getMessage()]
    assert keep_line and "unpushed commit" in keep_line[0]
    assert "uncommitted" not in keep_line[0]  # the per-dir line is right
    summary = [r.getMessage() for r in caplog.records if "orphan sweep: removed" in r.getMessage()]
    assert summary
    # The summary must not count a clean worktree with unpushed commits as
    # "uncommitted user work".
    assert "1 with uncommitted user work" not in summary[0], summary[0]
    assert "0 with uncommitted user work" in summary[0], summary[0]
    assert "1 with unpushed commits or stashes only" in summary[0], summary[0]
    assert (result.kept_uncommitted, result.kept_unpushed) == (0, 1)


@pytest.mark.asyncio
async def test_summary_counts_each_kept_dir_under_one_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    unpushed = _pushed_clone(tmp_path / "111" / "1")
    _local_commits(unpushed, 1)
    stashed = _pushed_clone(tmp_path / "111" / "2")
    (stashed / "README.md").write_text("changed\n")
    _git(stashed, "stash", "-q")
    dirty = _pushed_clone(tmp_path / "111" / "3")
    (dirty / "draft.md").write_text("x\n")
    both = _pushed_clone(tmp_path / "111" / "4")
    _local_commits(both, 1)
    (both / "draft.md").write_text("x\n")
    for ws in (unpushed, stashed, dirty, both):
        _age(ws, 40)

    with caplog.at_level(logging.INFO, logger="c_lord.orphan_dirs"):
        result = await sweep_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)

    assert (result.removed, result.kept) == (0, 4)
    # Uncommitted files win when a directory has both; the counts add up to kept.
    assert (result.kept_uncommitted, result.kept_unpushed) == (2, 2)
    assert result.kept_user_work == 4  # both kinds are the user's work
    (summary,) = [r.getMessage() for r in caplog.records if "directory(ies)" in r.getMessage()]
    assert (
        "kept 4 (2 with uncommitted user work, 2 with unpushed commits or stashes only, "
        "0 not a git repo, 0 could not be checked, 0 unsafe or failed to remove)"
    ) in summary, summary


# #1286: a directory git could not answer for — unpushed commits unknown, or git
# timed out — was counted as "unpushed commits or stashes only" / "not a git
# repo" in the summary while its own line said "unknown". The operator reads the
# summary to decide what to delete by hand; it now gets its own count.


def _single_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "111" / "222"
    ws.mkdir(parents=True)
    (ws / "f").write_text("x")
    when = time.time() - 40 * DAY
    os.utime(ws, (when, when))
    return ws


async def _keep_and_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: WorktreeStatus,
) -> tuple[str, str, OrphanSweepResult]:
    ws = _single_ws(tmp_path)
    monkeypatch.setattr(orphan_dirs, "worktree_status", lambda *a, **k: status)
    with caplog.at_level(logging.INFO, logger="c_lord.orphan_dirs"):
        r = await orphan_dirs.sweep_orphan_dirs(tmp_path, known_dirs=set(), min_idle_days=30)
    assert r.kept == 1 and ws.exists()
    msgs = [x.getMessage() for x in caplog.records]
    keep = next(m for m in msgs if "keeping" in m)
    summ = next(m for m in msgs if "orphan sweep: removed" in m)
    return keep, summ, r


@pytest.mark.asyncio
async def test_unknown_unpushed_is_not_called_unpushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    keep, summ, r = await _keep_and_summary(
        tmp_path, monkeypatch, caplog, WorktreeStatus(is_repo=True, unpushed_commits=None)
    )
    assert "unknown" in keep
    assert not re.search(r"\b1 with unpushed commits or stashes only", summ), (keep, summ)
    assert "1 could not be checked" in summ, summ
    assert (r.kept_unpushed, r.kept_unknown) == (0, 1)


@pytest.mark.asyncio
async def test_git_timeout_is_not_called_not_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    keep, summ, r = await _keep_and_summary(
        tmp_path, monkeypatch, caplog, WorktreeStatus(is_repo=False, unknown="git timed out")
    )
    assert "status unknown" in keep
    assert "1 not a git repo" not in summ, (keep, summ)
    assert "1 could not be checked" in summ, summ
    # the counts still add up to ``kept``: nothing lands in "unsafe or failed"
    assert "0 unsafe or failed to remove" in summ, summ
    assert (r.kept_not_repo, r.kept_unknown) == (0, 1)
