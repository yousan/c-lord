"""Structural rules of the package — checked on the source, not by running it.

These fail when a change is correct line by line but puts code in the wrong
place: a Cog that drives the Claude runner by itself, or a module that branches
on a delivery switch that no longer exists.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

COGS_DIR = Path(__file__).parent.parent / "c_lord" / "cogs"
RUN_HELPER = COGS_DIR / "_run_helper.py"


def _direct_runner_runs(source: str) -> list[int]:
    """Line numbers where *source* calls ``.run(...)`` on something named a runner.

    Read from the syntax tree, so a mention in a docstring or a comment is not a
    call, and ``subprocess.run`` / ``asyncio.run`` are not runners.
    """
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "run":
            continue
        owner = node.func.value
        owner_name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
        if owner_name.lstrip("_").endswith("runner"):
            lines.append(node.lineno)
    return lines


class TestOnlyRunHelperDrivesTheRunner:
    """One place drives a turn: ``cogs/_run_helper.py``.

    Iterating ``runner.run()`` is only the visible half of a turn. The helper
    also keeps the in-flight ledger, the status lamp, the ``enter`` / ``exit``
    log pair and the event processing around it — a Cog that loops over the
    runner itself gets none of that, and its turns look hung or go unreported.
    Cogs call ``run_claude_with_config()`` (or the older
    ``run_claude_in_thread()``) instead.
    """

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("async def f(runner):\n    async for e in runner.run('p'):\n        pass\n", [2]),
            ("async def f(c):\n    async for e in c.runner.run('p'):\n        pass\n", [2]),
            ("def f(self):\n    return self._runner.run('p')\n", [2]),
            ('def f():\n    """async for e in runner.run(p): ..."""\n', []),
            ("import subprocess\nsubprocess.run(['true'])\n", []),
        ],
        ids=["bare", "through-config", "private-attribute", "docstring-only", "subprocess"],
    )
    def test_the_scan_finds_calls_and_only_calls(self, source: str, expected: list[int]) -> None:
        assert _direct_runner_runs(source) == expected

    def test_no_cog_calls_runner_run_itself(self) -> None:
        violations = [
            f"  {path.relative_to(COGS_DIR.parent.parent)}:{line}"
            for path in sorted(COGS_DIR.glob("*.py"))
            if path != RUN_HELPER
            for line in _direct_runner_runs(path.read_text())
        ]

        if violations:
            pytest.fail(
                "A Cog drives the Claude runner by itself; go through "
                "_run_helper.run_claude_with_config() instead:\n" + "\n".join(violations)
            )

    def test_run_helper_is_where_the_runner_is_driven(self) -> None:
        """The exemption above names a real file — and that file is where the call is."""
        assert RUN_HELPER.is_file()
        assert _direct_runner_runs(RUN_HELPER.read_text())

    def test_run_claude_with_config_takes_one_run_config(self) -> None:
        from c_lord.cogs._run_helper import run_claude_with_config

        assert inspect.iscoroutinefunction(run_claude_with_config)
        assert list(inspect.signature(run_claude_with_config).parameters) == ["config"]

    def test_run_claude_in_thread_keeps_its_positional_order(self) -> None:
        """The pre-RunConfig entry point is still there for consumers that call it."""
        from c_lord.cogs._run_helper import run_claude_in_thread

        assert inspect.iscoroutinefunction(run_claude_in_thread)
        leading = list(inspect.signature(run_claude_in_thread).parameters)[:5]
        assert leading == ["thread", "runner", "repo", "prompt", "session_id"]


class TestSingleDeliveryPath:
    """Issue #712: the JSONL transcript mirror is the ONLY delivery path.

    The skill-push bridge (#53) was removed, not merely defaulted off, because a
    legacy opt-in kept the "Claude forgot to post and the turn vanished" failure
    mode (#491) one env line away — and its gate also kept the REST API control
    plane from starting in the default configuration (#543).

    So no module may branch on the removed switches again. ``legacy_env.py`` is
    the single exemption: warning about a var requires naming it.
    """

    _FORBIDDEN = ("CLORD_BRIDGE_MODE", "USE_SKILL_REPLY", "skills_enabled", "bridge_mode_jsonl")
    _EXEMPT = {"legacy_env.py"}

    def test_no_bridge_mode_branching_in_package(self) -> None:
        pkg_dir = Path(__file__).parent.parent / "c_lord"
        violations = []
        for py_file in sorted(pkg_dir.rglob("*.py")):
            if py_file.name in self._EXEMPT:
                continue
            for line_no, line in enumerate(py_file.read_text().splitlines(), start=1):
                for token in self._FORBIDDEN:
                    if token in line:
                        rel = py_file.relative_to(pkg_dir.parent)
                        violations.append(f"  {rel}:{line_no}: {line.strip()}")

        if violations:
            pytest.fail(
                "Removed delivery-path switches are referenced again (#712).\n"
                "jsonl is the only bridge; do not reintroduce a mode gate:\n"
                + "\n".join(violations)
            )
