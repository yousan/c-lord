"""``/workspace-cleanup`` says why it kept a directory (#1016).

#903 counted unpushed commits and ignored files (``.env``) as work. The 30-day
and orphan sweeps say so (``describe_changes()``), but the manual
``/workspace-cleanup`` listed such a directory under "⚠️ Dirty — not removed"
with one fixed reason, "session directory has uncommitted changes" — and
``git status`` shows nothing, so the reader could not tell what to do.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from c_lord.session_dir import SessionDirManager

from .test_session_manage import _capture_responder, _make_cog

CHANNEL = 1505747831447883806
THREAD = 1559120173389062204

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
}


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(path), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _session_clone(tmp_path: Path) -> Path:
    """A clone whose ``main`` is on origin, as ``create_session_dir`` leaves it."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("hello\n")
    (seed / ".gitignore").write_text(".env\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", str(origin), "main")
    ws = tmp_path / "sessions" / str(CHANNEL) / str(THREAD)
    ws.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(ws)], check=True, env=_GIT_ENV)
    return ws


async def _cleanup_embed(ws: Path):
    """Run the real ``/workspace-cleanup`` path over the directory holding *ws*."""
    cog = _make_cog()
    cog.bot.session_registry.list_active = MagicMock(return_value=[])
    cog._get_all_bindings = AsyncMock(return_value=[{"channel_id": CHANNEL}])
    sdm = SessionDirManager(base_dir=str(ws.parent), source_repo="/repo")
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
    respond, ack, sent = _capture_responder()
    await cog._workspace_cleanup_impl(dry_run=False, respond=respond, ack=ack)
    return sent[-1]["embed"]


def _dirty_field(embed) -> str:
    fields = [f for f in embed.fields if f.name.startswith("⚠️ Dirty")]
    assert len(fields) == 1, [f.name for f in embed.fields]
    return fields[0].value


async def test_unpushed_commits_are_named_with_their_count(tmp_path: Path, caplog) -> None:
    """AC1/AC3. RED: "uncommitted changes" for a clean ``git status``."""
    ws = _session_clone(tmp_path)
    _git(ws, "checkout", "-q", "-b", "feature/wip")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "wip 1")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "wip 2")

    with caplog.at_level(logging.INFO, logger="c_lord.session_dir"):
        embed = await _cleanup_embed(ws)

    assert ws.exists()
    value = _dirty_field(embed)
    assert "2 unpushed commit(s)" in value, value
    assert "uncommitted changes" not in value
    kept = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and str(ws) in r.getMessage() and "unpushed" in r.getMessage()
    ]
    assert len(kept) == 1, [r.getMessage() for r in caplog.records]


async def test_an_ignored_env_file_is_named(tmp_path: Path) -> None:
    """AC2: ``.env`` alone keeps the directory — and the display says ``!! .env``."""
    ws = _session_clone(tmp_path)
    (ws / ".env").write_text("TOKEN=x\n")

    embed = await _cleanup_embed(ws)

    assert ws.exists()
    assert "!! .env" in _dirty_field(embed)


async def test_a_clean_clone_is_still_removed(tmp_path: Path) -> None:
    ws = _session_clone(tmp_path)

    embed = await _cleanup_embed(ws)

    assert not ws.exists()
    assert not [f for f in embed.fields if f.name.startswith("⚠️ Dirty")]
