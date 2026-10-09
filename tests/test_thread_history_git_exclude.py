"""The reattach hand-off file stays out of commits and out of the sweep's way (#882).

On a WORKDIR reattach (#538 / #700 / #862) c-lord writes the whole Discord
thread into ``.claude/clord-thread-history.md``. It is c-lord's file, like the
injected SKILL.md (#779 / #749), but it got neither treatment: it showed up
untracked — one ``git add -A`` from putting the conversation into the user's
repository — and the orphan sweep counted it as the user's unfinished work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord

from c_lord.discord_ui.authorization import Authorizer
from c_lord.session_dir import SessionDirManager, worktree_status
from c_lord.session_reattach import HISTORY_FILENAME, Recovery

THREAD_ID = 1530372563858096321


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    return path


def _ignored(repo: Path) -> bool:
    return _git(repo, "check-ignore", "-q", HISTORY_FILENAME, check=False).returncode == 0


def _cog(tmp_path: Path):
    from c_lord.cogs.claude_chat import ClaudeChatCog

    bot = MagicMock()
    bot.channel_id = 999
    bot.settings_repo = None
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    cog = ClaudeChatCog(
        bot=bot, repo=repo, runner=MagicMock(), authorizer=Authorizer(allow_anyone=True)
    )
    sdm = MagicMock()
    sdm.base_dir = str(tmp_path / "sessions" / "9999")
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    cog._projects_root = tmp_path / "projects"
    (tmp_path / "projects").mkdir()
    cog._collect_thread_history = AsyncMock(  # type: ignore[method-assign]
        return_value=[("yousan", "2026-10-09 02:52", "社外秘の話")]
    )
    return cog


async def test_exported_history_is_git_ignored(tmp_path: Path) -> None:
    """AC1: after the export, ``git check-ignore`` succeeds and ``git add -A``
    does not stage the conversation."""
    work = _init_repo(tmp_path / "sessions" / "9999" / str(THREAD_ID))
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.parent_id = 999

    plan = await _cog(tmp_path)._reattach_thread(thread)

    assert plan.kind is Recovery.WORKDIR
    assert (work / HISTORY_FILENAME).is_file()
    assert _ignored(work)
    _git(work, "add", "-A")
    assert "clord-thread-history" not in _git(work, "status", "--porcelain").stdout


def test_worktree_status_counts_history_as_clord_file(tmp_path: Path) -> None:
    """AC2: the sweep does not keep a dir whose only change is the history."""
    repo = _init_repo(tmp_path / "w")
    (repo / ".claude").mkdir()
    (repo / HISTORY_FILENAME).write_text("x\n", encoding="utf-8")

    status = worktree_status(str(repo))

    assert status.user_changes == ()
    assert status.clord_files == (f"?? {HISTORY_FILENAME}",)
    assert status.clean


def test_next_turn_excludes_a_history_written_before_the_fix(tmp_path: Path) -> None:
    """AC4: an existing workspace with an un-excluded history gets the line on
    its next turn (create_session_dir runs every turn)."""
    base = tmp_path / "sessions"
    work = _init_repo(base / "555")
    (work / ".claude").mkdir()
    (work / HISTORY_FILENAME).write_text("x\n", encoding="utf-8")
    assert not _ignored(work)

    SessionDirManager(base_dir=str(base), source_repo="/repo").create_session_dir(555)

    assert _ignored(work)


def test_no_history_no_exclude_line(tmp_path: Path) -> None:
    """A workspace that never reattached gets no line it does not need."""
    base = tmp_path / "sessions"
    work = _init_repo(base / "556")

    SessionDirManager(base_dir=str(base), source_repo="/repo").create_session_dir(556)

    exclude = (work / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert HISTORY_FILENAME not in exclude
