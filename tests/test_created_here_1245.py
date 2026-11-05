"""#1245, third review of ef8f0fc — commits made here are verified, whatever ref holds them.

ef8f0fc trusted every tracking ref whose reflog had no ``update by push``. But a
commit made here can sit under a *fetched* ref too: pushed by URL and fetched
back (a1), pushed via a second remote and fetched (a2), pushed and then the
reflogs expired (a3), pushed by a tool that writes no reflog (a8), or
fast-forwarded upstream into ``release`` and fetched (b1). Each time the branch
was then deleted upstream and /workspace-delete deleted the only copy.

Now the question is whether a commit *created here* (``commit``, ``merge``,
``rebase``, ``cherry-pick``, ``revert`` in HEAD's or a branch's reflog) is held
by a tracking ref: those refs are asked. With no usable HEAD reflog, every
tracking ref is asked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from c_lord import session_dir as sd

from .test_stale_tracking_ref_1245 import _count_ls_remote
from .test_submodule_unpushed_1198 import _commit, _kept, _workspace, git


@pytest.fixture(autouse=True)
def _fresh_ls_remote_cache():
    sd._ls_remote_cache.clear()
    yield
    sd._ls_remote_cache.clear()


def _work_commit(ws: Path) -> str:
    git(ws / "sub", "checkout", "-q", "-b", "work")
    return _commit(ws / "sub", "w.txt")


def _drop_local_and_upstream(ws: Path, subr: Path, *branches: str) -> None:
    git(ws / "sub", "checkout", "-q", "--detach", "origin/main")
    git(ws / "sub", "branch", "-q", "-D", "work")
    for b in branches:
        git(subr, "update-ref", "-d", f"refs/heads/{b}")
    git(subr, "gc", "-q", "--prune=now")


def test_a1_pushed_by_url_then_fetched_is_kept(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    _work_commit(ws)
    git(ws / "sub", "push", "-q", str(subr), "work")  # no tracking ref
    git(ws / "sub", "fetch", "-q", "origin")  # origin/work, a fetch
    _drop_local_and_upstream(ws, subr, "work")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_a2_pushed_via_second_remote_then_fetched_is_kept(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    _work_commit(ws)
    git(ws / "sub", "remote", "add", "mine", str(subr))
    git(ws / "sub", "push", "-q", "mine", "work")
    git(ws / "sub", "fetch", "-q", "origin")
    _drop_local_and_upstream(ws, subr, "work")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_a3_reflogs_expired_after_the_push_is_kept(tmp_path: Path) -> None:
    ws, subr = _workspace(tmp_path)
    _work_commit(ws)
    git(ws / "sub", "push", "-q", "origin", "work")
    _drop_local_and_upstream(ws, subr, "work")
    git(ws / "sub", "reflog", "expire", "--expire=now", "--all")
    assert not _kept(ws).removed


def test_a8_push_that_wrote_no_reflog_is_kept(tmp_path: Path) -> None:
    """go-git / dulwich, or logging off at the time: the tracking ref has no reflog."""
    ws, subr = _workspace(tmp_path)
    _work_commit(ws)
    git(ws / "sub", "-c", "core.logAllRefUpdates=false", "push", "-q", "origin", "work")
    log = ws / ".git" / "modules" / "sub" / "logs" / "refs" / "remotes" / "origin" / "work"
    if log.exists():
        log.unlink()
    _drop_local_and_upstream(ws, subr, "work")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_b1_fast_forwarded_upstream_into_release_then_both_deleted_is_kept(
    tmp_path: Path,
) -> None:
    ws, subr = _workspace(tmp_path)
    sha = _work_commit(ws)
    git(ws / "sub", "push", "-q", "origin", "work")
    git(subr, "update-ref", "refs/heads/release", sha)  # ff-merged upstream
    git(ws / "sub", "fetch", "-q", "origin")  # origin/release, a fetch
    _drop_local_and_upstream(ws, subr, "work", "release")
    res = _kept(ws)
    assert "sub: 1 unpushed commit(s)" in res.reason, res.reason


def test_untouched_submodule_makes_no_network_call(tmp_path: Path, monkeypatch) -> None:
    """Control: nothing created here — 0 ls-remote, deleted."""
    ws, _ = _workspace(tmp_path)
    _commit(tmp_path / "subremote-seed", "later.txt")
    git(tmp_path / "subremote-seed", "push", "-q", "origin", "HEAD:main")
    calls = _count_ls_remote(monkeypatch)
    from .test_submodule_unpushed_1198 import _delete

    assert _delete(ws).removed
    assert calls == []
