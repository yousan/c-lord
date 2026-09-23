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
Files c-lord writes into the dir itself (the injected ``discord-read`` skill)
are not the user's changes and do not count (#749).
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
from .skills.injector import LEGACY_SKILL_NAMES, READ_SKILL_NAME

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


#: Files c-lord itself writes into every session dir (#749). They show up in
#: ``git status`` — the clone's ``.gitignore`` has never heard of them — and
#: until #749 that alone made the dir "dirty": the sweeps refused to delete 60
#: orphans (9.6 GB) whose only change was the injected SKILL.md, and told their
#: threads 「書きかけの成果物が残っています」.
#:
#: Exact files, not ``.claude/``: that directory is the user's too (settings,
#: their own skills), and anything else in it stays work.
_CLORD_FILES: frozenset[str] = frozenset({f".claude/skills/{READ_SKILL_NAME}/SKILL.md"})

#: Whole directories: c-lord ``rmtree``s these on every turn
#: (``remove_legacy_skills``, #712), so nothing placed there can outlive the
#: next turn anyway.
_CLORD_DIRS: tuple[str, ...] = tuple(f".claude/skills/{name}/" for name in LEGACY_SKILL_NAMES)

#: Claude Code's own sub-agent worktrees. Not c-lord's — so never ignored
#: outright: each one is opened and must itself be clean (see
#: :func:`worktree_status`).
_WORKTREE_ENTRY_RE = re.compile(r"^\.claude/worktrees/(?!\.\.?/)[^/]+/$")

#: A worktree inside a worktree inside ... is followed this far and no further;
#: past it the entry counts as work (i.e. the dir is kept).
_MAX_WORKTREE_DEPTH = 2


@dataclass(frozen=True)
class WorktreeStatus:
    """What ``git status`` found, split into the user's changes and c-lord's.

    Entries are porcelain lines (``"?? path"``, ``" M path"``) so a log line can
    show exactly why a directory was kept.
    """

    #: ``False`` when git could not answer — not a repo, or git failed.
    #: Cleanliness then cannot be established, so the dir is never clean.
    is_repo: bool
    #: Changes that count as work. One is enough to keep the directory.
    user_changes: tuple[str, ...] = ()
    #: Changes c-lord made itself (or clean Claude Code worktrees) — not work.
    clord_files: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.is_repo and not self.user_changes


def _is_clord_change(xy: str, rel: str) -> bool:
    """Is this porcelain entry c-lord's own write rather than the user's?

    Untracked, or a tracked copy c-lord overwrote/removed in the working tree
    only (index still equals HEAD — c-lord's own repo tracks the injected
    SKILL.md since it slipped into a commit). A *staged* change is someone's
    decision and always counts.
    """
    owned = rel in _CLORD_FILES or rel.startswith(_CLORD_DIRS)
    if not owned:
        return False
    return xy == "??" or (xy[0] == " " and xy[1] in "MDT")


def _is_clean_worktree(path: str, xy: str, rel: str, depth: int) -> bool:
    """Is this entry a Claude Code worktree with nothing uncommitted inside?

    The parent's ``git status`` shows a nested checkout as one line and never
    looks in, so the worktree is opened and judged by the same rules.
    """
    if xy != "??" or not _WORKTREE_ENTRY_RE.match(rel) or depth >= _MAX_WORKTREE_DEPTH:
        return False
    return worktree_status(str(Path(path) / rel), _depth=depth + 1).clean


def worktree_status(path: str, *, _depth: int = 0) -> WorktreeStatus:
    """Classify ``git status`` of *path* into the user's changes and c-lord's (#749).

    Safety invariant (module docstring): anything that *might* be the user's
    uncommitted work lands in ``user_changes``. Only files c-lord writes itself
    are set aside, and a Claude Code worktree only when it is itself clean.
    """
    # -uall: a wholly untracked ``.claude/`` must be listed file by file, or the
    # injected SKILL.md and a user's own file would hide behind one line.
    # -z: paths come back unquoted, so spaces and non-ASCII names parse as-is.
    result = _run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=path)
    if result.returncode != 0:
        return WorktreeStatus(is_repo=False)

    user: list[str] = []
    clord: list[str] = []
    fields = iter(result.stdout.split("\0"))
    for entry in fields:
        if not entry.strip():
            continue
        xy, rel = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            next(fields, None)  # the source path of a rename/copy
        line = f"{xy} {rel}"
        if _is_clord_change(xy, rel) or _is_clean_worktree(path, xy, rel, _depth):
            clord.append(line)
        else:
            user.append(line)
    return WorktreeStatus(is_repo=True, user_changes=tuple(user), clord_files=tuple(clord))


def _is_clean(path: str) -> bool:
    """Return True if the directory has no uncommitted changes of the user's.

    Files c-lord wrote itself do not count (#749) — see :func:`worktree_status`.
    """
    return worktree_status(path).clean


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
        without re-cloning. The ``discord-read`` skill (#259) is (re)injected on
        every call so its baked-in ``.env`` path stays current, and any skill
        left behind by the retired skill-push path is scrubbed (#712).

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

        from .skills.injector import inject_read_skill, remove_legacy_skills

        # #712: scrub the retired output skills (discord-reply /
        # discord-prompt-choice) if an older c-lord left them here. They tell
        # Claude to POST its answer to the REST API, which is listening again —
        # so a leftover would get the answer delivered twice.
        try:
            remove_legacy_skills(target)
        except OSError as exc:
            logger.warning("Failed to remove legacy skills for thread %d: %s", thread_id, exc)

        # Issue #259: discord-read lets Claude read other Discord channels by
        # curl-ing Discord's own API — nothing to do with how its answers get
        # delivered, so every session gets it, regardless of cwd.
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
