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

    def create_session_dir(self, thread_id: int) -> str:
        """Create (or return existing) session directory for a thread.

        Idempotent: if the directory already exists, returns its path
        without re-cloning.

        Returns:
            Absolute path to the session directory.
        """
        target = str(Path(self._base_dir) / str(thread_id))
        if Path(target).is_dir():
            logger.info("Session dir already exists: %s", target)
            return target

        Path(self._base_dir).mkdir(parents=True, exist_ok=True)

        args = ["git", "clone"]
        if _is_local_repo(self._source_repo):
            args.append("--local")
        else:
            args.extend(["--depth=1", "--single-branch"])

        args.extend([self._source_repo, target])

        result = _run(args)
        if result.returncode != 0:
            logger.error(
                "git clone failed for thread %d: %s",
                thread_id,
                result.stderr.strip(),
            )
            raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

        logger.info("Created session dir for thread %d: %s", thread_id, target)

        # Issue #52 Phase 1: inject discord-reply skill so Claude can push
        # final answers via REST API instead of relying on capture-pane
        # scraping. Gated by USE_SKILL_REPLY env so old path stays default.
        from .skills.injector import inject_skills, skills_enabled

        if skills_enabled():
            try:
                inject_skills(target, thread_id=thread_id)
            except OSError as exc:
                # Don't fail session creation on a skill write error.
                logger.warning("Failed to inject skills for thread %d: %s", thread_id, exc)

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
