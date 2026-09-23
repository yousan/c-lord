"""Tests for the build hook that bakes the commit date into the wheel (#756).

hatch-vcs only records a date for *dirty* builds, so a clean
``uv tool install git+…`` used to install a c-lord that reported ``v1.4.197``
with no date — and the stale-build check could never fire for exactly the
instances that fall behind. ``hatch_build.py`` writes ``c_lord/_build_info.py``
so the installed package knows its commit date without asking the network.

hatchling itself is only present in the build environment, so the hook's base
class is stubbed here.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def hook_module(monkeypatch: pytest.MonkeyPatch):
    iface = types.ModuleType("hatchling.builders.hooks.plugin.interface")

    class BuildHookInterface:  # minimal stand-in for hatchling's base class
        def __init__(self, root: str) -> None:
            self.root = root

    iface.BuildHookInterface = BuildHookInterface  # type: ignore[attr-defined]
    for name in (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "hatchling.builders.hooks.plugin.interface", iface)

    spec = importlib.util.spec_from_file_location("hatch_build", _ROOT / "hatch_build.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_repo(path: Path, when: str) -> None:
    env = {
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=path, check=True, env=env)


def test_bakes_the_commit_date(hook_module, tmp_path: Path) -> None:
    _git_repo(tmp_path, "2026-09-15T12:00:00+09:00")
    (tmp_path / "c_lord").mkdir()
    build_data: dict = {"artifacts": []}

    hook_module.CustomBuildHook(str(tmp_path)).initialize("standard", build_data)

    ns: dict = {}
    exec((tmp_path / "c_lord" / "_build_info.py").read_text(), ns)
    assert ns["COMMIT_DATE"] == "20260915"
    assert "/c_lord/_build_info.py" in build_data["artifacts"]


def test_without_git_keeps_an_existing_file(hook_module, tmp_path: Path) -> None:
    """Building a wheel from the sdist: no ``.git``, but the sdist carries the file."""
    (tmp_path / "c_lord").mkdir()
    (tmp_path / "c_lord" / "_build_info.py").write_text('COMMIT_DATE = "20260901"\n')
    build_data: dict = {"artifacts": []}

    hook_module.CustomBuildHook(str(tmp_path)).initialize("standard", build_data)

    assert '"20260901"' in (tmp_path / "c_lord" / "_build_info.py").read_text()
    assert "/c_lord/_build_info.py" in build_data["artifacts"]


def test_without_git_or_file_never_fails_the_build(hook_module, tmp_path: Path) -> None:
    """A missing date must never be able to break ``pip install`` for anyone."""
    (tmp_path / "c_lord").mkdir()
    build_data: dict = {"artifacts": []}

    hook_module.CustomBuildHook(str(tmp_path)).initialize("standard", build_data)

    assert not (tmp_path / "c_lord" / "_build_info.py").exists()
    assert build_data["artifacts"] == []
