"""Git clone based session directory management for Claude Code sessions.

Each Claude Code session gets its own cloned copy of the source repository,
isolated in a directory named by thread ID.  This replaces the old git worktree
approach with a simpler, more robust model:

  - ``git clone --local`` for local repos (fast, hardlink-based)
  - ``git clone --depth=1 --single-branch`` for remote repos
  - Each clone is a fully independent repository

Cleanup is triggered at three points:
  1. Session end — remove the session dir if it is clean (see _run_helper.py)
  2. Bot startup — remove all orphaned clean session directories
  3. Manual — via /session-dirs and /session-cleanup Discord commands

Safety invariant: a directory with uncommitted changes is NEVER auto-removed.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .coauthor import install_coauthor_hook
from .git_mirrors import ensure_mirror, mirrors_root_for

logger = logging.getLogger(__name__)

_THREAD_DIR_RE = re.compile(r"^\d+$")


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return the result (never raises on non-zero exit)."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _is_clean(path: str) -> bool:
    """Return True if the directory has no uncommitted changes."""
    result = _run(["git", "status", "--porcelain"], cwd=path)
    if result.returncode != 0:
        return False
    return result.stdout.strip() == ""


def _get_commit(path: str) -> str:
    """Return the short commit hash, or empty string on error."""
    result = _run(["git", "rev-parse", "--short", "HEAD"], cwd=path)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_local_repo(source: str) -> bool:
    """Return True if source looks like a local filesystem path."""
    return source.startswith("/") or source.startswith(".")


@dataclass(frozen=True)
class SessionDirInfo:
    """Snapshot of a single session directory."""

    path: str
    thread_id: int
    source_repo: str
    commit: str
    is_clean: bool


@dataclass(frozen=True)
class CleanupResult:
    """Result of a single cleanup attempt."""

    path: str
    thread_id: int | None
    removed: bool
    reason: str


class SessionDirManager:
    """Manages clone-based session directories for Claude Code sessions.

    Each session gets a directory named ``{base_dir}/{thread_id}`` containing
    a git clone of ``source_repo``.

    Args:
        base_dir: Parent directory for all session dirs.
        source_repo: Git repository URL or local path to clone from.
    """

    def __init__(
        self,
        base_dir: str,
        source_repo: str,
    ) -> None:
        self._base_dir = base_dir
        self._source_repo = source_repo

    @property
    def base_dir(self) -> str:
        return self._base_dir

    @property
    def source_repo(self) -> str:
        return self._source_repo

    def _mirror_for_clone(self) -> Path | None:
        """共有オブジェクトの置き場。用意できなければ ``None`` (#643).

        **ここで何が起きてもクローンを止めない。** 同じリポジトリを240回
        ダウンロードして持つのをやめるための最適化であって、正しさには
        関わらない。だから広い ``except`` を置いてある — このモジュールの
        ためにスレッドが立たなくなるのは、割に合わない交換。
        """
        try:
            return ensure_mirror(mirrors_root_for(self._base_dir), self._source_repo)
        except Exception:
            logger.warning("git mirror unavailable, cloning without it", exc_info=True)
            return None

    def create_session_dir(self, thread_id: int, coauthor: Any | None = None) -> str:
        """Create (or return existing) session directory for a thread.

        Idempotent: if the directory already exists, returns its path
        without re-cloning. The skill bundle (#52) is (re)injected on every
        call when the flag is enabled — this keeps SKILL.md in sync with the
        current ``CLORD_API_URL`` / ``CLORD_API_SECRET`` even if the operator
        changes them between sessions.

        Args:
            thread_id: Discord thread the session belongs to.
            coauthor: Discord user who triggered this turn (#518). Recorded
                as a ``Co-authored-by`` trailer on commits Claude makes in
                this checkout. None for runs with no human behind them
                (scheduler), which then get Claude's trailer only.

        Returns:
            Absolute path to the session directory.
        """
        target = str(Path(self._base_dir) / str(thread_id))
        already_existed = Path(target).is_dir()

        if not already_existed:
            Path(self._base_dir).mkdir(parents=True, exist_ok=True)

            args = ["git", "clone"]
            if _is_local_repo(self._source_repo):
                # ``--local`` は既に hardlink でオブジェクトを共有しているので
                # ミラーを挟む意味が無い (実測: ``/home/yousan/c-lord`` の
                # 300クローンで合計 0.5 GB)。
                args.append("--local")
            else:
                args.extend(["--depth=1", "--single-branch"])
                mirror = self._mirror_for_clone()
                if mirror is not None:
                    # ``-if-able``: ミラーが消えていても info を1行出して
                    # 通常のクローンに落ちる。ミラーは高速化と節約のための
                    # ものであって、動作の前提ではない (#643)。
                    args.extend(["--reference-if-able", str(mirror)])

            # `--` so a flag-shaped source_repo can never be read as a git
            # option (`--upload-pack=<cmd>` executes it). Repo strings reach
            # here from user input via `/clord repo:` (#514); channel_repo
            # .validate_repo_url() is the other half of this guard.
            args.extend(["--", self._source_repo, target])

            result = _run(args)
            if result.returncode != 0:
                logger.error(
                    "git clone failed for thread %d: %s",
                    thread_id,
                    result.stderr.strip(),
                )
                raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

            logger.info("Created session dir for thread %d: %s", thread_id, target)
        else:
            logger.info("Session dir already exists: %s", target)

        # Issue #52 Phase 1: (re)inject discord-reply skill so Claude can push
        # final answers via REST API instead of relying on capture-pane
        # scraping. Gated by USE_SKILL_REPLY env so old path stays default.
        # Runs on every call to keep api_url / api_secret in sync.
        from .skills.injector import (
            inject_read_skill,
            inject_skills,
            remove_injected_skills,
            skills_enabled,
        )

        if skills_enabled():
            try:
                inject_skills(target, thread_id=thread_id)
            except OSError as exc:
                # Don't fail session creation on a skill write error.
                logger.warning("Failed to inject skills for thread %d: %s", thread_id, exc)
        else:
            # jsonl bridge mode etc.: scrub any stale REST-API output skill left
            # by a prior skill-mode session so Claude isn't pointed at a dead
            # REST API.
            try:
                remove_injected_skills(target)
            except OSError as exc:
                logger.warning("Failed to remove stale skills for thread %d: %s", thread_id, exc)

        # Issue #259: discord-read is bridge-independent (it curls the Discord
        # REST API directly, not c-lord's API), so inject it in every mode —
        # including jsonl, where the output skills above are scrubbed. This is
        # what lets Claude read other channels regardless of cwd or #71 state.
        try:
            inject_read_skill(target)
        except OSError as exc:
            logger.warning("Failed to inject discord-read for thread %d: %s", thread_id, exc)

        # Issue #518: (re)install the prepare-commit-msg hook so commits made
        # in this checkout record who asked for them. Refreshed every turn —
        # the trailer must name the user who triggered *this* turn, not the
        # one who happened to create the thread.
        install_coauthor_hook(target, user=coauthor)

        return target

    def find_session_dirs(self) -> list[SessionDirInfo]:
        """Return all session directories under base_dir.

        Scans for directories whose name is purely numeric (thread IDs)
        and contain a ``.git`` directory or file.
        """
        results: list[SessionDirInfo] = []
        base = Path(self._base_dir)

        try:
            entries = list(base.iterdir())
        except OSError as exc:
            logger.error("Cannot scan base_dir %s: %s", self._base_dir, exc)
            return results

        for entry in entries:
            if not entry.is_dir():
                continue
            if not _THREAD_DIR_RE.match(entry.name):
                continue
            if not (entry / ".git").exists():
                continue

            path = str(entry)
            thread_id = int(entry.name)
            commit = _get_commit(path)
            clean = _is_clean(path)

            results.append(
                SessionDirInfo(
                    path=path,
                    thread_id=thread_id,
                    source_repo=self._source_repo,
                    commit=commit,
                    is_clean=clean,
                )
            )

        return results

    def cleanup_for_thread(self, thread_id: int) -> CleanupResult:
        """Remove the session directory for ``thread_id`` if it is clean.

        If the directory does not exist this is a no-op (returns removed=False).
        """
        path = str(Path(self._base_dir) / str(thread_id))
        if not Path(path).is_dir():
            return CleanupResult(
                path=path,
                thread_id=thread_id,
                removed=False,
                reason="session directory does not exist",
            )

        return self._try_remove(path, thread_id)

    def cleanup_orphaned(self, active_thread_ids: set[int]) -> list[CleanupResult]:
        """Remove clean session directories whose sessions are no longer active.

        Args:
            active_thread_ids: Thread IDs that are currently running.
                               Directories for these sessions are skipped.
        """
        results: list[CleanupResult] = []
        for info in self.find_session_dirs():
            if info.thread_id in active_thread_ids:
                results.append(
                    CleanupResult(
                        path=info.path,
                        thread_id=info.thread_id,
                        removed=False,
                        reason="session is still active",
                    )
                )
                continue

            result = self._try_remove(info.path, info.thread_id)
            results.append(result)

        return results

    def _try_remove(self, path: str, thread_id: int | None) -> CleanupResult:
        """Check cleanliness and remove the directory if safe."""
        if not _is_clean(path):
            logger.warning(
                "Skipping session dir removal (dirty): %s (thread_id=%s)",
                path,
                thread_id,
            )
            return CleanupResult(
                path=path,
                thread_id=thread_id,
                removed=False,
                reason="session directory has uncommitted changes — skipped to prevent data loss",
            )

        try:
            shutil.rmtree(path)
            logger.info("Removed session dir: %s (thread_id=%s)", path, thread_id)
            return CleanupResult(path=path, thread_id=thread_id, removed=True, reason="clean")
        except OSError as exc:
            logger.warning("Failed to remove session dir %s: %s", path, exc)
            return CleanupResult(
                path=path,
                thread_id=thread_id,
                removed=False,
                reason=f"removal failed: {exc}",
            )
