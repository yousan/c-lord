"""Guidance that names `/clord-init` options must name real ones (#513).

Error messages told users to run `/clord-init repo:<URL> branch:<branch>`, but
the command has never had a `branch` option — the user types it, Discord offers
nothing, and they are stuck. The option list is read from the command itself,
so a future rename or removal fails here instead of drifting in the text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from c_lord.cogs.channel_repo import ChannelRepoCog

ROOT = Path(__file__).resolve().parent.parent

# `/clord-init repo:<URL> branch:<branch>` → ("clord-init", "repo:<URL> branch:<branch>")
_MENTION_RE = re.compile(r"/(clord-init|clord-thread-init)((?: [a-z_]+:[^\s`]*)+)")
_OPTION_RE = re.compile(r" ([a-z_]+):")  # the name before a value like repo:https://…


def _guidance_files() -> list[Path]:
    files = list((ROOT / "c_lord").rglob("*.py"))
    files += list((ROOT / "docs").rglob("*.md"))
    files.append(ROOT / "README.md")
    return files


def _real_options() -> dict[str, set[str]]:
    return {
        cmd.name: {p.name for p in cmd.parameters}
        for cmd in (ChannelRepoCog.clord_init, ChannelRepoCog.clord_thread_init)
    }


def test_no_branch_option_in_clord_init_guidance() -> None:
    """AC2: `grep -rn 'branch:<branch>' c_lord/ docs/` is empty."""
    hits = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in _guidance_files()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "branch:<branch>" in line
    ]
    assert hits == []


@pytest.mark.parametrize("path", _guidance_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_clord_init_mentions_only_real_options(path: Path) -> None:
    real = _real_options()
    bogus = [
        f"{n}: /{cmd}{opts}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for cmd, opts in _MENTION_RE.findall(line)
        if not set(_OPTION_RE.findall(opts)) <= real[cmd]
    ]
    assert bogus == []
