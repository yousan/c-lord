"""Second review of 0e9b09c — undecodable git output, and any failure, keeps.

``_run`` decoded git's output as strict UTF-8. A non-UTF-8 file name or
``remote.x.url`` raised ``UnicodeDecodeError``, which is no ``OSError``: it
escaped ``worktree_status`` and stopped /workspace-delete and the sweeps. Now
undecodable bytes pass through (``surrogateescape``), any failure in the checks
is "unknown" (kept), and ``cleanup_orphaned`` goes on past a workspace that
failed.
"""

from __future__ import annotations

from pathlib import Path

from c_lord import session_dir as sd
from c_lord.session_dir import SessionDirManager, describe_changes, worktree_status

from .test_submodule_unpushed_1198 import _delete, _workspace, git


def test_non_utf8_remote_url_in_a_submodule_does_not_raise(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    with (ws / ".git" / "modules" / "sub" / "config").open("ab") as f:
        f.write(b'[remote "x"]\n\turl = /nowhere/caf\xe9.git\n')
    st = worktree_status(str(ws))
    assert st.clean, describe_changes(st)
    assert _delete(ws).removed


def test_any_failure_in_the_checks_is_unknown(tmp_path: Path, monkeypatch) -> None:
    ws, _ = _workspace(tmp_path)

    def boom(*_a: object, **_k: object) -> int:
        raise ValueError("unexpected")

    monkeypatch.setattr(sd, "_stash_entries", boom)
    st = worktree_status(str(ws))
    assert not st.clean
    assert "status unknown" in describe_changes(st)
    assert not _delete(ws).removed


def test_cleanup_orphaned_goes_on_past_a_failing_workspace(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "sessions"
    for n in ("1", "2"):
        (base / n).mkdir(parents=True)
        git(base / n, "init", "-q")
    real = sd._get_commit

    def flaky(path: str) -> str:
        if path.endswith("/1"):
            raise RuntimeError("unexpected")
        return real(path)

    monkeypatch.setattr(sd, "_get_commit", flaky)
    results = SessionDirManager(base_dir=str(base), source_repo="/x").cleanup_orphaned(set())
    by_id = {r.thread_id: r for r in results}
    assert by_id[2].removed and not (base / "2").exists()
    assert not by_id[1].removed and (base / "1").exists()


def test_reason_never_carries_surrogates(tmp_path: Path) -> None:
    """Third review: a non-UTF-8 name reached Discord and the logs as a lone surrogate."""
    import os

    ws, _ = _workspace(tmp_path)
    with open(os.path.join(os.fsencode(ws), b"caf\xe9.env"), "wb") as f:
        f.write(b"x")
    res = _delete(ws)
    assert not res.removed
    res.reason.encode("utf-8")  # no lone surrogate
    assert "caf\\xe9.env" in res.reason, res.reason


def test_unreadable_session_dir_is_reported_and_the_scan_goes_on(tmp_path: Path) -> None:
    import os

    import pytest

    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    base = tmp_path / "sessions"
    for n in ("1", "2"):
        (base / n).mkdir(parents=True)
        git(base / n, "init", "-q")
    (base / "1").chmod(0o600)
    try:
        results = SessionDirManager(base_dir=str(base), source_repo="/x").cleanup_orphaned(set())
        by_id = {r.thread_id: r for r in results}
        assert 1 in by_id and not by_id[1].removed and "unknown" in by_id[1].reason, by_id
        assert by_id[2].removed
    finally:
        (base / "1").chmod(0o755)
