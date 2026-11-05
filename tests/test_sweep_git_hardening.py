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


# ---- W57 round 12: the neutral directory may sit inside someone's repo ----


def _enclosing_repo_tmp(tmp: Path, monkeypatch) -> Path:  # noqa: ANN001
    """TMPDIR inside a git repo (``git init /tmp`` happens)."""
    import tempfile

    root = tmp / "evilroot"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    t = root / "tmp"
    t.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(t))
    return root


def test_ls_remote_ignores_a_repo_enclosing_tmpdir_ssh_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    ws, subr = _workspace(tmp_path)
    _pr_flow(tmp_path, ws, ff=False)
    git(ws / ".git" / "modules" / "sub", "--git-dir=.", "config", "remote.origin.url",
        f"ssh://fakehost{subr}")  # fmt: skip
    root = _enclosing_repo_tmp(tmp_path, monkeypatch)
    log = tmp_path / "ran.log"
    s = tmp_path / "evil-ssh.sh"
    s.write_text(f'#!/bin/bash\necho "evil-ssh $*" >> "{log}"\n[ "$1" = -G ] && exit 0\n'
                 'eval "${@: -1}"\n')  # fmt: skip
    s.chmod(0o755)
    git(root, "config", "core.sshCommand", str(s))
    _delete(ws)
    assert not log.exists(), log.read_text()


def test_ls_remote_ignores_a_repo_enclosing_tmpdir_insteadof(tmp_path: Path, monkeypatch) -> None:
    """A decoy ``insteadOf`` there would point the ls-remote at a repo still holding the branch."""
    ws, subr = _workspace(tmp_path)
    sub = ws / "sub"
    git(sub, "checkout", "-q", "-b", "work")
    (sub / "w.txt").write_text("only copy\n")
    git(sub, "add", "w.txt")
    git(sub, "commit", "-qm", "work")
    git(sub, "push", "-q", "origin", "work")
    decoy = tmp_path / "decoy.git"
    git(tmp_path, "clone", "-q", "--bare", str(subr), str(decoy))
    git(subr, "update-ref", "-d", "refs/heads/work")
    git(subr, "gc", "-q", "--prune=now")
    git(sub, "checkout", "-q", "main")
    git(sub, "branch", "-q", "-D", "work")
    assert not _delete(ws).removed, "control"
    sd._ls_remote_cache.clear()
    root = _enclosing_repo_tmp(tmp_path, monkeypatch)
    git(root, "config", f"url.{decoy}.insteadOf", str(subr))
    assert not _delete(ws).removed


# ---- W57 round 13: wherever TMPDIR sits (a ':' split GIT_CEILING_DIRECTORIES) ----


def _evil_ssh_repo(at: Path, script: Path) -> None:
    at.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(at)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(at), "config", "core.sshCommand", str(script)], check=True)


def _tmpdir_layout(tmp: Path, case: str, script: Path) -> Path:
    if case == "tmpdir-is-repo":
        _evil_ssh_repo(tmp / "R", script)
        return tmp / "R"
    if case == "grandparent-is-repo":
        _evil_ssh_repo(tmp / "R", script)
        (tmp / "R" / "a" / "tmp").mkdir(parents=True)
        return tmp / "R" / "a" / "tmp"
    if case == "symlink-into-repo":
        _evil_ssh_repo(tmp / "R", script)
        (tmp / "R" / "tmp").mkdir()
        link = tmp / "outside" / "tmplink"
        link.parent.mkdir()
        link.symlink_to(tmp / "R" / "tmp")
        return link
    if case == "colon-in-tmpdir-repo":
        _evil_ssh_repo(tmp / "a:b", script)
        return tmp / "a:b"
    _evil_ssh_repo(tmp / "a:b", script)  # colon-above-tmpdir
    (tmp / "a:b" / "tmp").mkdir()
    return tmp / "a:b" / "tmp"


@pytest.mark.parametrize("call", ["ls-remote", "probe"])
@pytest.mark.parametrize(
    "case",
    [
        "tmpdir-is-repo",
        "grandparent-is-repo",
        "symlink-into-repo",
        "colon-in-tmpdir-repo",
        "colon-above-tmpdir",
    ],
)
def test_enclosing_repo_config_not_used(tmp_path: Path, monkeypatch, case: str, call: str) -> None:
    import tempfile

    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    log = tmp_path / "ran.log"
    script = tmp_path / "evil-ssh.sh"
    script.write_text(f'#!/bin/sh\necho "evil-ssh $*" >> "{log}"\nexit 1\n')
    script.chmod(0o755)
    monkeypatch.setattr(tempfile, "tempdir", str(_tmpdir_layout(tmp_path, case, script)))
    if call == "ls-remote":
        sd._ask_ls_remote("ssh://fakehost/x.git")
    else:
        ws = tmp_path / "probe-ws"
        git(tmp_path, "init", "-q", str(ws))
        git(ws, "commit", "-q", "--allow-empty", "-m", "x")
        old = git(ws, "rev-parse", "HEAD").strip()
        sd._probe_contains(str(ws), ["git"], ws / ".git", "ssh://fakehost/x.git", ("main",), [old])
    assert not log.exists(), log.read_text()


def test_bot_env_git_dir_is_not_used(tmp_path: Path, monkeypatch) -> None:
    """A ``GIT_DIR`` in the bot's environment does not pick the repo either."""
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    log = tmp_path / "ran.log"
    script = tmp_path / "evil-ssh.sh"
    script.write_text(f'#!/bin/sh\necho "evil-ssh $*" >> "{log}"\nexit 1\n')
    script.chmod(0o755)
    _evil_ssh_repo(tmp_path / "R", script)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "R" / ".git"))
    sd._ask_ls_remote("ssh://fakehost/x.git")
    assert not log.exists(), log.read_text()
