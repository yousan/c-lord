"""Review of 3eaa7da — the sweep's git calls run nothing the workspace planted.

The workspace's git config and hooks are Claude's to write, and the sweep ran
git there with the bot's environment: a ``reference-transaction`` hook, a
``core.fsmonitor`` command or a ``remote.origin.uploadpack`` ran on every
sweep, with ``DISCORD_BOT_TOKEN`` in their environment. And a probe ref left
behind by a killed sweep (``refs/clord-probe/*``) counted as local work forever.

Now inspecting calls run with fsmonitor and hooks off and without the
secrets; the remote is asked by URL from outside the workspace, and the probe
fetches into a throwaway repo — the workspace gets no ref at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from c_lord import session_dir as sd
from c_lord.tmux import SENSITIVE_ENV_KEYS

from .test_created_here_probe_1245 import _net, _pr_flow, _upstream_moves
from .test_submodule_unpushed_1198 import _delete, _workspace, git


@pytest.fixture(autouse=True)
def _fresh_ls_remote_cache():
    sd._ls_remote_cache.clear()
    yield
    sd._ls_remote_cache.clear()


def _marker_script(tmp: Path, name: str, tail: str = "") -> tuple[Path, Path]:
    marker = tmp / f"{name}.ran"
    script = tmp / f"{name}.sh"
    script.write_text(f'#!/bin/sh\necho "$DISCORD_BOT_TOKEN" > "{marker}"\n{tail}')
    script.chmod(0o755)
    return script, marker


def test_leftover_probe_ref_does_not_keep_the_workspace(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path)
    _upstream_moves(tmp_path)
    sub = ws / "sub"
    git(sub, "fetch", "-q", "--refmap=", "origin", "main:refs/clord-probe/origin/main")
    assert _delete(ws).removed


def test_a_leftover_probe_lock_does_not_stop_the_probe(tmp_path: Path, monkeypatch) -> None:
    ws, _ = _workspace(tmp_path)
    _pr_flow(tmp_path, ws, ff=False)
    _upstream_moves(tmp_path)
    lock = ws / ".git" / "modules" / "sub" / "refs" / "clord-probe" / "origin" / "main.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    net = _net(monkeypatch)
    assert _delete(ws).removed
    assert net["fetch"] == 1


def test_no_workspace_hook_runs_during_the_probe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-token")
    ws, _ = _workspace(tmp_path)
    _pr_flow(tmp_path, ws, ff=False)
    _upstream_moves(tmp_path)
    hook, marker = _marker_script(tmp_path, "reftx")
    hooks = ws / ".git" / "modules" / "sub" / "hooks"
    (hooks / "reference-transaction").write_text(hook.read_text())
    (hooks / "reference-transaction").chmod(0o755)
    assert _delete(ws).removed
    assert not marker.exists(), marker.read_text()


def test_no_fsmonitor_command_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-token")
    ws, _ = _workspace(tmp_path)
    script, marker = _marker_script(tmp_path, "fsmon")
    git(ws, "config", "core.fsmonitor", str(script))
    sd.worktree_status(str(ws))
    assert not marker.exists(), marker.read_text()


def test_no_workspace_uploadpack_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-token")
    ws, _ = _workspace(tmp_path)
    _pr_flow(tmp_path, ws, ff=False)
    script, marker = _marker_script(tmp_path, "upload", 'exec git-upload-pack "$@"\n')
    git(ws / "sub", "config", "remote.origin.uploadpack", str(script))
    net = _net(monkeypatch)
    assert _delete(ws).removed
    assert net["ls-remote"] == 1
    assert not marker.exists(), marker.read_text()


@pytest.mark.parametrize("key", SENSITIVE_ENV_KEYS)
def test_secrets_are_not_in_the_environment_of_git_calls(monkeypatch, key: str) -> None:
    monkeypatch.setenv(key, "secret-value")
    shown = sd._run(["git", "-c", f"alias.clordenv=!printenv {key}; true", "clordenv"], cwd="/")
    assert "secret-value" not in shown.stdout


def test_probe_fetch_has_the_ls_remote_timeout_and_a_failure_costs_once(
    tmp_path: Path, monkeypatch
) -> None:
    ws, _ = _workspace(tmp_path)
    _pr_flow(tmp_path, ws, ff=False)
    _upstream_moves(tmp_path)
    timeouts: list[float | None] = []
    real = subprocess.Popen

    class Hang(real):  # type: ignore[misc, valid-type]
        def communicate(self, input=None, timeout=None):  # noqa: A002, ANN001, ANN201
            if "fetch" in self.args:
                timeouts.append(timeout)
                raise subprocess.TimeoutExpired(self.args, timeout or 0)
            return super().communicate(input, timeout)

    monkeypatch.setattr(sd.subprocess, "Popen", Hang)
    assert not sd.worktree_status(str(ws)).clean
    _upstream_moves(tmp_path)  # a new head, same branch: the failure still stands
    sd._ls_remote_cache.pop(next(k for k in sd._ls_remote_cache if "\0" not in k))
    assert not sd.worktree_status(str(ws)).clean
    # One fetch, with the ls-remote timeout (the 5 s is _run draining the killed group).
    assert [t for t in timeouts if t != 5] == [sd._LS_REMOTE_TIMEOUT_SECONDS], timeouts


def test_leftover_cleanup_runs_no_workspace_hook(tmp_path: Path, monkeypatch) -> None:
    """Deleting a leftover probe ref is a ref update: hooks stay off for it too."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-token")
    ws, _ = _workspace(tmp_path)
    _pr_flow(tmp_path, ws, ff=False)
    _upstream_moves(tmp_path)
    sub = ws / "sub"
    git(sub, "update-ref", "refs/clord-probe/origin/main", "HEAD")
    hook, marker = _marker_script(tmp_path, "reftx-cleanup")
    hooks = ws / ".git" / "modules" / "sub" / "hooks"
    (hooks / "reference-transaction").write_text(hook.read_text())
    (hooks / "reference-transaction").chmod(0o755)
    assert _delete(ws).removed
    assert not marker.exists(), marker.read_text()
