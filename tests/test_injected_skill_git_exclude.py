"""The injected ``discord-read`` SKILL.md stays out of the user's commits (#779).

c-lord writes ``.claude/skills/discord-read/SKILL.md`` into every workspace on
every turn (#259). The clone's ``.gitignore`` knows nothing about it, so it
showed up untracked and one ``git add -A`` put it — with this host's ``.env``
path baked in — into the user's repository. It happened to c-lord itself (#704).

The fix is the one attachments already use (#528): a line in
``.git/info/exclude``, which is local to the clone, rather than an edit to the
user's ``.gitignore``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from c_lord.skills.injector import inject_read_skill

ENTRY = "/.claude/skills/discord-read/SKILL.md"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    return tmp_path


def test_git_add_all_does_not_pick_up_the_skill(repo: Path) -> None:
    """#779 AC1 + AC5."""
    inject_read_skill(repo, env_path="/srv/c-lord/.env")

    _git(repo, "add", "-A")
    status = _git(repo, "status", "--porcelain")

    assert ".claude/skills/discord-read" not in status
    assert "SKILL.md" not in status


def test_exclude_line_is_written_once(repo: Path) -> None:
    """#779 AC1: the second and later injections do not duplicate the line."""
    inject_read_skill(repo)
    inject_read_skill(repo)
    inject_read_skill(repo)

    lines = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines.count(ENTRY) == 1


def test_existing_exclude_content_is_kept(repo: Path) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("# mine\n/secret-notes/")  # no trailing newline

    inject_read_skill(repo)

    lines = exclude.read_text().splitlines()
    assert "# mine" in lines
    assert "/secret-notes/" in lines
    assert ENTRY in lines


def test_user_gitignore_is_untouched(repo: Path) -> None:
    """#779 AC2."""
    before = (repo / ".gitignore").read_bytes()

    inject_read_skill(repo)

    assert (repo / ".gitignore").read_bytes() == before


def test_not_a_git_repository_still_injects(tmp_path: Path) -> None:
    """#779 AC3: best effort — no ``.git`` is not an error."""
    path = inject_read_skill(tmp_path)

    assert Path(path).is_file()
    assert not (tmp_path / ".git").exists()


def test_unwritable_exclude_still_injects(repo: Path) -> None:
    """#779 AC3: a failure to write the exclude never fails the injection."""
    info = repo / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "exclude").unlink(missing_ok=True)
    (info / "exclude").mkdir()  # a directory where the file should be → OSError

    path = inject_read_skill(repo)

    assert Path(path).is_file()


def test_gitfile_worktree_does_not_raise(tmp_path: Path) -> None:
    """``.git`` can be a file (worktree / submodule). Best effort, no exception."""
    (tmp_path / ".git").write_text("gitdir: /nonexistent/elsewhere\n")

    path = inject_read_skill(tmp_path)

    assert Path(path).is_file()
