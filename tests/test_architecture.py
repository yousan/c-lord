"""Architecture tests — enforce structural rules that prevent code duplication.

These tests catch violations at CI time, not at code review time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COGS_DIR = Path(__file__).parent.parent / "c_lord" / "cogs"
RUN_HELPER = COGS_DIR / "_run_helper.py"


class TestNoDirectRunnerRunInCogs:
    """Cog files must NOT call runner.run() directly.

    All Claude CLI execution must go through _run_helper.run_claude_with_config()
    (or the backward-compat shim run_claude_in_thread()).
    Direct runner.run() calls bypass the shared rich experience (streaming text,
    tool result embeds, thinking, intermediate text posting) and create
    maintenance burden when the experience is updated.

    If you need Claude CLI execution in a Cog, use:
        from ._run_helper import run_claude_with_config
        from .run_config import RunConfig
        await run_claude_with_config(RunConfig(thread=..., runner=..., prompt=...))

    The ONLY file allowed to call runner.run() directly is _run_helper.py itself.
    """

    # Pattern matches a bare variable named "runner" calling .run() directly.
    # Excludes attribute access patterns like config.runner.run() or self.runner.run()
    # which are allowed (they go through _run_helper orchestration).
    # Only matches: runner.run( at the start of an identifier boundary.
    _RUNNER_RUN_PATTERN = re.compile(r"(?<![.\w])runner\.run\s*\(")

    def _get_cog_files(self) -> list[Path]:
        """Return all .py files in cogs/ except the core execution modules."""
        excluded = {"_run_helper.py", "run_config.py", "__init__.py"}
        return [f for f in COGS_DIR.glob("*.py") if f.name not in excluded]

    def test_no_direct_runner_run_in_cogs(self) -> None:
        """No Cog file should call runner.run() directly."""
        violations = []
        for cog_file in self._get_cog_files():
            content = cog_file.read_text()
            matches = list(self._RUNNER_RUN_PATTERN.finditer(content))
            if matches:
                lines = content.splitlines()
                for match in matches:
                    # Find line number
                    line_no = content[: match.start()].count("\n") + 1
                    violations.append(f"  {cog_file.name}:{line_no}: {lines[line_no - 1].strip()}")

        if violations:
            msg = (
                "Direct runner.run() calls found in Cog files.\n"
                "Use run_claude_with_config() from _run_helper instead:\n" + "\n".join(violations)
            )
            pytest.fail(msg)

    def test_run_helper_exists(self) -> None:
        """_run_helper.py must exist — it's the single source of truth."""
        assert RUN_HELPER.exists(), "_run_helper.py is missing from cogs/"

    def test_run_helper_exports_run_claude_with_config(self) -> None:
        """_run_helper must export the primary run_claude_with_config function."""
        content = RUN_HELPER.read_text()
        assert "async def run_claude_with_config" in content

    def test_run_helper_exports_run_claude_in_thread(self) -> None:
        """_run_helper must also export the backward-compat shim."""
        content = RUN_HELPER.read_text()
        assert "async def run_claude_in_thread" in content


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
