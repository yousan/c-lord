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
are not the user's changes and do not count (#749). Commits on no remote branch
and files git ignores are work too (#903) — ``git status`` shows neither.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote

from .attachments import ATTACHMENT_SUBDIR
from .coauthor import install_coauthor_hook
from .git_exclude import add_git_exclude
from .git_mirrors import ensure_mirror, mirrors_root_for
from .session_reattach import HISTORY_FILENAME
from .skills.injector import LEGACY_SKILL_NAMES, READ_SKILL_NAME
from .tmux import SENSITIVE_ENV_KEYS

logger = logging.getLogger(__name__)

_THREAD_DIR_RE = re.compile(r"^\d+$")
#: A full object name, as a detached ``HEAD`` file holds it (#1198).
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


#: How long one inspecting git call may take before it counts as failed (#1198).
_GIT_TIMEOUT_SECONDS = 60.0

#: Exit status of a call that timed out (as ``timeout(1)`` reports it).
_TIMEOUT_RETURNCODE = 124

#: Variables that point git at a repository other than the one a call names.
_REPO_LOCATING_ENV: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CONFIG",
)

#: ``-c`` for every inspecting git call (review of 3eaa7da): nothing the workspace
#: planted runs — no ``core.fsmonitor`` command (``git status``), no hook
#: (``reference-transaction`` on a ref update), no ``ext::`` remote.
_INSPECT_CONFIG: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
)


def _run(
    args: list[str],
    cwd: str | None = None,
    *,
    stdin: str | None = None,
    timeout: float | None = _GIT_TIMEOUT_SECONDS,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return the result (never raises on non-zero exit).

    #1198: a call that takes longer than *timeout* comes back as a failure
    (exit 124) — callers read a failure as "unknown", which keeps a workspace,
    instead of hanging the sweep. The call runs in its own process group and
    the whole group is killed: ``git status`` runs ``git status`` in each
    submodule and clean filters, which outlived a kill of ``git`` alone.
    ``GIT_OPTIONAL_LOCKS=0``: ``git status`` otherwise holds ``index.lock``
    while it runs, and a killed one left it behind — the next ``git commit`` in
    the kept workspace failed. ``GIT_NO_LAZY_FETCH=1``: in a partial clone a
    missing object would otherwise be fetched over the network mid-inspection
    (ignored by git before 2.44). ``git clone`` passes ``timeout=None`` and
    keeps the caller's process group.
    """
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0", **(extra_env or {})}
    # Review of 3eaa7da: the workspace's config and hooks are Claude's to write,
    # and this runs with the bot's environment. The secrets never reach a child
    # (#353's list), and an inspecting git call (a timeout is set — ``git
    # clone`` passes none) runs no fsmonitor command, no hook and no ``ext::``
    # transport. ``-c`` reaches the git processes it starts (submodules) too.
    for key in SENSITIVE_ENV_KEYS:
        env.pop(key, None)
    if timeout is not None and args[:1] == ["git"]:
        args = ["git", *_INSPECT_CONFIG, *args[1:]]
        # W57 round 14: a repo-locating variable in the bot's environment
        # (``GIT_COMMON_DIR`` …) made git read — or inspect — another repo:
        # its ``core.sshCommand`` ran, and a ``GIT_DIR`` at a clean repo would
        # have read every workspace as clean. Each call locates its repo by
        # ``cwd`` / ``--git-dir``, or by the ``GIT_DIR`` *extra_env* sets.
        for key in _REPO_LOCATING_ENV:
            env.pop(key, None)
        env.update(extra_env or {})
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # A non-UTF-8 file name or URL must not raise UnicodeDecodeError
        # (second review of 0e9b09c); bytes round-trip back into git.
        errors="surrogateescape",
        cwd=cwd,
        env=env,
        start_new_session=timeout is not None,
    )
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a child left the group and holds a pipe
            proc.kill()
            proc.wait()
        return subprocess.CompletedProcess(
            args, _TIMEOUT_RETURNCODE, "", f"timed out after {timeout}s"
        )
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


#: Files c-lord itself writes into every session dir (#749). They show up in
#: ``git status`` — the clone's ``.gitignore`` has never heard of them — and
#: until #749 that alone made the dir "dirty": the sweeps refused to delete 60
#: orphans (9.6 GB) whose only change was the injected SKILL.md, and told their
#: threads 「書きかけの成果物が残っています」.
#:
#: Exact files, not ``.claude/``: that directory is the user's too (settings,
#: their own skills), and anything else in it stays work.
#:
#: #882: the Discord thread c-lord exports on a WORKDIR reattach is c-lord's
#: too — counting it as work kept dirs whose only change was that export.
_CLORD_FILES: frozenset[str] = frozenset(
    {f".claude/skills/{READ_SKILL_NAME}/SKILL.md", HISTORY_FILENAME}
)

#: Whole directories: c-lord ``rmtree``s these on every turn
#: (``remove_legacy_skills``, #712), so nothing placed there can outlive the
#: next turn anyway.
_CLORD_DIRS: tuple[str, ...] = tuple(f".claude/skills/{name}/" for name in LEGACY_SKILL_NAMES)

#: Discord attachments c-lord downloads into the dir (#528). The originals stay
#: in the thread, so they are c-lord's copy, not work.
_CLORD_OWNED_DIRS: tuple[str, ...] = (*_CLORD_DIRS, f"{ATTACHMENT_SUBDIR}/")

#: Ignored paths a tool rebuilds, which therefore are not work (#903). Every
#: *other* ignored file is: a ``.env``, a local runbook hidden by
#: ``info/exclude`` (#902's ``AUDIT.md``), notes in an ignored ``notes/``.
#: The list is what keeps the sweep useful — counting ``node_modules/`` or
#: ``.venv/`` as work would strand every checkout again (#749's 118 GB).
#: Anything not named here resolves to *keep*.
#:
#: A path is regenerable when any of its directories is in
#: :data:`_REGENERABLE_DIRS`, its file name is in :data:`_REGENERABLE_FILES` or
#: ends with :data:`_REGENERABLE_SUFFIXES`, or it lies under one of
#: :data:`_REGENERABLE_SUBPATHS` (framework runtime dirs, at any depth).
_REGENERABLE_DIRS: frozenset[str] = frozenset(
    {
        # package managers / virtualenvs
        "node_modules", "bower_components", ".venv", "venv", "vendor", ".eggs",
        # caches
        "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".nox",
        ".cache", ".gradle", ".dart_tool", ".parcel-cache", ".turbo", ".terraform",
        # build output
        "build", "dist", "target", ".build", ".next", ".nuxt", ".svelte-kit",
        ".angular", ".expo", ".godot", "build_web", "coverage", "htmlcov",
    }
)  # fmt: skip
_REGENERABLE_FILES: frozenset[str] = frozenset(
    {
        ".DS_Store",
        ".coverage",
        ".phpunit.result.cache",
        # written by the build backend on install (hatch-vcs, #722)
        "_version.py",
        "_build_info.py",
    }
)
_REGENERABLE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".egg-info")
_REGENERABLE_SUBPATHS: tuple[str, ...] = (
    "storage/framework/",
    "storage/logs/",
    "bootstrap/cache/",
)

#: An ignored directory that is not regenerable is opened and its files judged
#: one by one (git reports it as a single line). Past this many files it counts
#: as work without looking further.
_MAX_IGNORED_DIR_FILES = 2000

#: Claude Code's own sub-agent worktrees. Not c-lord's — so never ignored
#: outright: each one is opened and must itself be clean (see
#: :func:`worktree_status`).
_WORKTREE_ENTRY_RE = re.compile(r"^\.claude/worktrees/(?!\.\.?/)[^/]+/$")

#: A worktree inside a worktree inside ... is followed this far and no further;
#: past it the entry counts as work (i.e. the dir is kept).
_MAX_WORKTREE_DEPTH = 2


def exclude_thread_history(work_dir: str | Path) -> None:
    """Keep the exported Discord thread out of the user's commits (#882).

    Same mechanism as the injected skill (#779) and uploads (#528):
    ``.git/info/exclude``, never the user's ``.gitignore``.
    """
    add_git_exclude(work_dir, f"/{HISTORY_FILENAME}", "Discord thread history for reattach (#882)")


@dataclass(frozen=True)
class WorktreeStatus:
    """What ``git status`` found, split into the user's changes and c-lord's.

    Entries are porcelain lines (``"?? path"``, ``" M path"``) so a log line can
    show exactly why a directory was kept.
    """

    #: ``False`` when git could not answer — not a repo, or git failed.
    #: Cleanliness then cannot be established, so the dir is never clean.
    is_repo: bool
    #: Why ``git status`` gave no answer when it is known (``"git timed out"``,
    #: #1198) — told apart from "not a git repo" in the reason.
    unknown: str = ""
    #: Changes that count as work. One is enough to keep the directory.
    user_changes: tuple[str, ...] = ()
    #: Changes c-lord made itself (or clean Claude Code worktrees) — not work.
    clord_files: tuple[str, ...] = ()
    #: Commits reachable from HEAD, a local branch or the stash but from no
    #: remote branch (#903); ``None`` when git could not tell. Any is work.
    unpushed_commits: int | None = 0
    #: ``git ls-remote`` failed or timed out, so pushed branches without a
    #: tracking ref could not be told apart from unpushed ones (#903).
    remote_unchecked: bool = False
    #: Entries in ``git stash list`` (#1045). Any is work; counted apart from
    #: unpushed commits so the reason says "stash", not "unpushed commit".
    stash_entries: int = 0
    #: Stashes inside submodules, as ``"<path>: N stash entries"`` (#1121). They
    #: live in ``<ws>/.git/modules/<name>`` — deleting the dir deletes them —
    #: and the parent's status and ``refs/stash`` never show them. Commits in a
    #: submodule on no remote-tracking ref (tags never count) are reported here too, as
    #: ``"<path>: N unpushed commit(s)"`` (#1198) — checked out or not, the
    #: parent's status need not show them.
    submodule_stashes: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return (
            self.is_repo
            and not self.user_changes
            and self.unpushed_commits == 0
            and self.stash_entries == 0
            and not self.submodule_stashes
        )


def _is_clord_change(xy: str, rel: str) -> bool:
    """Is this porcelain entry c-lord's own write rather than the user's?

    Untracked, or a tracked copy c-lord overwrote/removed in the working tree
    only (index still equals HEAD — c-lord's own repo tracks the injected
    SKILL.md since it slipped into a commit). A *staged* change is someone's
    decision and always counts.
    """
    if not _is_clord_owned(rel):
        return False
    return xy in ("??", "!!") or (xy[0] == " " and xy[1] in "MDT")


def _is_clord_owned(rel: str) -> bool:
    return rel in _CLORD_FILES or rel.startswith(_CLORD_OWNED_DIRS)


def _is_regenerable(rel: str) -> bool:
    """Is this ignored path something a tool rebuilds? See :data:`_REGENERABLE_DIRS`."""
    parts = rel.rstrip("/").split("/")
    name = parts[-1]
    if name in _REGENERABLE_FILES or name.endswith(_REGENERABLE_SUFFIXES):
        return True
    if any(part in _REGENERABLE_DIRS for part in parts):
        return True
    padded = f"/{rel.rstrip('/')}/"
    return any(f"/{sub}" in padded for sub in _REGENERABLE_SUBPATHS)


def _ignored_dir_holds_work(path: str, rel: str) -> bool:
    """Does the wholly ignored directory *rel* hold anything but c-lord's and tools' files?

    ``git status`` reports a matched directory as one ``!! dir/`` line, so a
    repository that ignores ``.claude/`` would hide a user's own skill behind
    the injected one. Walks without following symlinks; too many files, or an
    unreadable tree, counts as work.
    """
    root = Path(path)
    seen = 0
    try:
        for current, dirs, files in os.walk(root / rel):
            base = Path(current).relative_to(root).as_posix()
            dirs[:] = [d for d in dirs if not _is_regenerable(f"{base}/{d}/")]
            for name in files:
                seen += 1
                if seen > _MAX_IGNORED_DIR_FILES:
                    return True
                file_rel = f"{base}/{name}"
                if not (_is_clord_owned(file_rel) or _is_regenerable(file_rel)):
                    return True
    except OSError:
        return True
    return False


#: ``git ls-remote`` per remote URL is cached this long (#903). Every session
#: dir of a channel shares one origin, so a sweep asks the network once, not
#: once per candidate. Short, because a stale "this head exists" would delete.
_LS_REMOTE_TTL_SECONDS = 60.0
#: A remote that does not answer within this is "could not check" — keep.
_LS_REMOTE_TIMEOUT_SECONDS = 20.0
#: Keyed by :func:`_ls_remote_key`. A failure is cached too, as ``None``: an
#: offline host waits out the timeout once per URL per TTL, not once per
#: workspace.
_ls_remote_cache: dict[str, tuple[float, dict[str, str] | None]] = {}
_ls_remote_lock = threading.Lock()

#: ``scheme://…`` or scp-like ``[user@]host:path`` — the same remote from any repo.
_NETWORK_URL_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://|[^/:]+:)")


def _ls_remote_key(cwd: str, url: str) -> str:
    """The cache key: a network URL as is, a local path resolved against *cwd*.

    ``../sub.git`` in two repos is two remotes; one cached answer for both
    would vouch for the wrong one's branches. A ``file://`` URL is absolute.
    """
    if _NETWORK_URL_RE.match(url):
        return url
    return os.path.normpath(os.path.join(os.path.realpath(cwd), url))


def _ls_remote_refs(
    cwd: str, name: str, url: str, pre: list[str], *, tags: bool = False
) -> dict[str, str] | None:
    """``refs/heads/<branch>`` → object name on remote *name*, cached by *url* (#903).

    With *tags*, ``refs/tags/<tag>`` (and git's peeled ``^{}`` lines) instead,
    cached apart (#1246).

    ``None`` when the remote could not be asked (failed, timed out) — the
    caller then keeps; that answer is cached for the TTL too. *pre* is the
    ``git`` prefix (``--git-dir=…`` for a module git dir whose checkout is
    gone, #1245).
    """
    key = _ls_remote_key(cwd, url)
    if tags:
        key = f"tags\0{key}"
    with _ls_remote_lock:
        cached = _ls_remote_cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < _LS_REMOTE_TTL_SECONDS:
        return cached[1]
    heads = _ask_ls_remote(_remote_target(cwd, url), tags=tags)
    with _ls_remote_lock:
        _ls_remote_cache[key] = (time.monotonic(), heads)
    return heads


def _remote_target(cwd: str, url: str) -> str:
    """What to hand git for *url*: a local path resolved against *cwd*, else the URL."""
    local = _local_remote_path(cwd, url)
    return local if local is not None else url


def _ask_ls_remote(target: str, *, tags: bool = False) -> dict[str, str] | None:
    """``git ls-remote`` of *target*'s branches (or, with *tags*, its tags), from outside.

    Review of 3eaa7da: by URL, from a neutral directory, so none of the
    workspace's config applies — no ``remote.<name>.uploadpack``, no
    ``core.sshCommand``, no credential helper or ``insteadOf`` of its own. The
    bot's global config and environment (its ssh keys, its helpers) still do.
    """
    # Through _run (#1198): on a timeout the transport child (ssh,
    # remote-https, upload-pack) is killed with git, not left running.
    # ``--symref HEAD``: the default branch, probed first when a branch is
    # gone (W57 round 12). Branches only — no tags, no ``refs/pull/*``.
    try:
        with tempfile.TemporaryDirectory(prefix="clord-ls-remote-") as neutral:
            result = _run(
                ["git", "ls-remote", "--tags", "--", target]
                if tags  # #1246: tags only, through the same neutral dir
                else ["git", "ls-remote", "--symref", "--", target, "HEAD", "refs/heads/*"],
                cwd=neutral,
                timeout=_LS_REMOTE_TIMEOUT_SECONDS,
                extra_env={"GIT_TERMINAL_PROMPT": "0", **_neutral_env(neutral)},
            )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    heads: dict[str, str] = {}
    for line in result.stdout.splitlines():
        value, _, ref = line.partition("\t")
        value, ref = value.strip(), ref.strip()
        if tags:
            if ref.startswith("refs/tags/") and value:
                heads[ref] = value
        elif ref == "HEAD" and value.startswith("ref: refs/heads/"):
            heads[_DEFAULT_BRANCH] = value[len("ref: ") :]
        elif ref.startswith("refs/heads/") and value:
            heads[ref] = value
    return heads


#: Key of :func:`_ask_ls_remote`'s answer naming the default branch (``refs/heads/…``).
_DEFAULT_BRANCH = "HEAD"


def _head_shas(heads: dict[str, str]) -> set[str]:
    """The branch heads' object names in an :func:`_ask_ls_remote` answer."""
    return {sha for ref, sha in heads.items() if ref.startswith("refs/heads/")}


def _neutral_env(directory: str) -> dict[str, str]:
    """Run git with the empty git dir *directory* as its repository (W57 rounds 12–13).

    ``TMPDIR`` may sit inside a git repository (``git init /tmp``): git would
    then read that repo's ``core.sshCommand`` or ``insteadOf`` for the sweep.
    ``GIT_DIR`` names the repository outright — no discovery at all, whatever
    the path holds (a ``:`` split ``GIT_CEILING_DIRECTORIES``), and a
    ``GIT_DIR`` in the bot's own environment is overridden too.
    """
    _make_empty_git_dir(Path(directory))
    return {"GIT_DIR": directory}


def _make_empty_git_dir(directory: Path) -> None:
    """A minimal git dir by hand — ``git init`` itself would look around first."""
    (directory / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (directory / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    head = directory / "HEAD"
    if not head.exists():
        # Not ``main`` (W57 round 14): the bot's own ``includeIf "onbranch:main"``
        # would match this throwaway dir. ``gitdir:/**`` still matches any git
        # dir — the bot's own global config, unavoidable.
        head.write_text(f"ref: refs/heads/clord-neutral-{os.urandom(6).hex()}\n", encoding="utf-8")


def _remote_head_shas(path: str, skip: frozenset[str] = frozenset()) -> frozenset[str] | None:
    """Every branch head on every remote of *path*, by asking the remotes (#903).

    A session dir is a single-branch clone: pushing a work branch leaves no
    ``origin/<branch>`` behind, so ``--remotes`` alone calls it unpushed.
    ``None`` when any remote could not be asked — the caller then keeps.
    """
    remotes = _run(["git", "remote"], cwd=path)
    if remotes.returncode != 0:
        return None
    shas: set[str] = set()
    for name in remotes.stdout.split():
        if name in skip:
            continue  # inside the workspace: deleted with it, never vouches
        url = _run(["git", "remote", "get-url", "--", name], cwd=path)
        if url.returncode != 0:
            return None
        heads = _ls_remote_refs(path, name, url.stdout.strip(), ["git"])
        if heads is None:
            return None
        shas |= _head_shas(heads)
    return frozenset(shas)


def _start_refs(path: str, trusted: frozenset[str]) -> tuple[list[str], list[str]] | None:
    """HEAD and every local ref but tags and notes, by name, and the tracking
    refs that vouch. ``None``: git failed.

    A tracking ref vouches only when it is of a remote in *trusted* (the longest
    configured name it starts with — ``a/b`` before ``a``). Every other one is
    local work like a branch: a remote that is no copy (third review of ef8f0fc,
    #1265) and a ref of no configured remote at all (review of f870f5b: ``git
    fetch <neighbour> 'refs/heads/*:refs/remotes/b/*'``).

    Not only branches (#1198): a commit held only by ``refs/original`` or
    another local ref was deleted with the workspace. Left out:

    - ``refs/remotes/*`` — what the count subtracts;
    - ``refs/stash`` — counted on its own (:func:`_stash_entries`, #1045), a
      stash commit is no "unpushed commit" a user could push;
    - ``refs/tags/*`` — a fetched tag cannot be told from a hand-made one, and
      counting them kept every workspace that fetched tags (a ``--depth=1``
      clone after ``git fetch --tags``, a tag on a deleted branch: the
      ``ls-remote`` heads cannot vouch for those). A commit held only by a
      local tag is therefore not counted — a known limit (spec);
    - ``refs/notes/*`` — the same: notes fetched from (or pushed to) the
      remote are on no remote branch, so counting them kept such workspaces
      forever, and notes annotate commits rather than hold work.

    Names, not object names: ``rev-list`` peels a ref to an annotated tag
    fully (one level of ``%(*objectname)`` did not). A failed (or timed-out)
    call is unknown, never "no refs".
    """
    refs = _run(["git", "for-each-ref", "--format=%(refname)"], cwd=path)
    if refs.returncode != 0:
        return None
    tips: list[str] = []
    vouching: list[str] = []
    for ref in refs.stdout.split():
        if ref.startswith("refs/remotes/"):
            (vouching if _tracking_remote(ref, trusted) else tips).append(ref)
        elif not ref.startswith(("refs/tags/", "refs/notes/", _PROBE_NS)) and ref != "refs/stash":
            tips.append(ref)
    # Only when present: an unborn HEAD would fail rev-list.
    found = _run(["git", "rev-parse", "-q", "--verify", "HEAD^{commit}"], cwd=path)
    if found.returncode not in (0, 1):
        return None
    if found.returncode == 0:
        tips.append("HEAD")
    return tips, vouching


def _tracking_remote(ref: str, names: frozenset[str]) -> str | None:
    """The longest of *names* that ``refs/remotes/<name>/`` *ref* starts with."""
    return next(
        (n for n in sorted(names, key=len, reverse=True) if ref.startswith(f"refs/remotes/{n}/")),
        None,
    )


def _stash_entries(path: str, *, git_dir: bool = False) -> int | None:
    """How many entries ``git stash list`` has (#1045). ``None``: git failed.

    A ``refs/stash`` that resolves is at least one entry, whatever its reflog
    says (#1080): ``rev-list --walk-reflogs`` counts reflog lines, and a stash
    whose reflog was expired — or that was placed with ``update-ref`` — counted
    0, so the dir read as clean and was removed with the stash's only copy.

    With *git_dir*, *path* is a git directory whose checkout may be gone
    (#1181). Its ``core.worktree`` then names a missing folder and every git
    command dies on it, so the git dir itself stands in as the work tree; and
    a failure other than "no such ref" is unknown, not "no stash" — in either
    mode since #1198, as a timed-out call must never read as "no stash".
    """
    pre = ["git", f"--git-dir={path}", f"--work-tree={path}"] if git_dir else ["git"]
    found = _run([*pre, "rev-parse", "-q", "--verify", "refs/stash"], cwd=path)
    if found.returncode not in (0, 1):
        return None
    if found.returncode != 0 or not found.stdout.strip():
        return 0  # no stash at all
    result = _run([*pre, "rev-list", "--walk-reflogs", "--count", "refs/stash"], cwd=path)
    if result.returncode != 0:
        return None
    try:
        return max(int(result.stdout.strip() or "0"), 1)
    except ValueError:
        return None


#: How deep submodules of submodules are searched for stashes (#1121).
_MAX_SUBMODULE_DEPTH = 3


def _stash_note(name: str, n: int | None) -> str | None:
    """``"<name>: N stash entries"``, or ``None`` with no stash. Unknown counts as one."""
    n = 1 if n is None else n
    if not n:
        return None
    return f"{name}: {n} stash {'entry' if n == 1 else 'entries'}"


def _linked_git_dir(checkout: Path) -> Path | None:
    """The git dir a submodule's ``.git`` *file* points at (``gitdir: …``), resolved."""
    try:
        line = (checkout / ".git").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None  # a directory (old-style submodule) or unreadable
    if not line.startswith("gitdir:"):
        return None
    return (checkout / line[len("gitdir:") :].strip()).resolve()


@dataclass(frozen=True)
class _Repo:
    """A repo inside the workspace whose commits all read as pushed (#1245).

    Kept for the network check (:func:`_stale_tracking_note`): "pushed" was
    judged from local remote-tracking refs only, and a branch deleted on the
    remote leaves its tracking ref behind.
    """

    name: str
    path: str
    #: *path* is a git dir whose checkout may be gone (see :func:`_stash_entries`).
    git_dir: bool
    own_git_dir: Path
    #: Commits only tags reach, not yet counted (#1246): judged by
    #: :func:`_tag_only_note` — over the network, so only when nothing else
    #: keeps the workspace (#903).
    tag_only: frozenset[str] = frozenset()


def _kind(p: Path) -> str | None:
    """``"dir"``, ``"file"``, ``"missing"`` or ``"other"``; ``None`` when unknown (#1242).

    ``Path.is_dir()`` reads a permission error as ``False`` and, on Python
    3.13, ``Path.is_file()`` raises it — the first read an unreadable
    ``.git/modules`` as "no submodules", the second stopped the sweep.
    """
    try:
        mode = p.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError:
        return None
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def _named(prefix: str, what: str) -> str:
    name = prefix.rstrip("/")
    return f"{name}: {what}" if name else what


#: Why a module dir could not be looked into (#1242).
_MODULES_UNREADABLE = "modules unknown (unreadable)"


def _submodule_stashes(
    path: str, *, prefix: str = "", depth: int = 0, pushed: list[_Repo] | None = None
) -> list[str]:
    """Work that lives only inside submodules: stashes and unpushed commits.

    ``"<path>: N stash entries"`` (#1121, #1181) and ``"<path>: N unpushed
    commit(s)"`` (#1198). Checked-out submodules are named by their path. Then
    every git dir under ``.git/modules`` that no checkout claimed is asked too
    (#1181): after ``git submodule deinit`` or ``git rm`` the checkout is gone
    but the stash and the commits are still there — and deleting the workspace
    deletes them. Those are named by their module name.

    Every ``160000`` gitlink in an index is asked, ``.gitmodules`` or not
    (#1207), at every depth (#1227): a nested repo that was only ``git add``-ed
    has no ``.gitmodules`` entry, and requiring one let its stash be deleted
    with the workspace.

    Cost, all local (no network): one ``git ls-files`` for the workspace, then
    per checked-out submodule one ``rev-parse`` (stash), one ``rev-list``
    (unpushed) and one ``ls-files`` of its own — at the deepest level too,
    where a checked-out repo below is reported as too deep (unknown, kept).
    Each unclaimed module git dir costs one ``rev-parse`` and one ``rev-list``.
    Tags never count as pushed (:func:`_local_only_commits`). Linked
    worktrees' HEADs are read from files, not asked. An embedded repo's own
    ``.git/modules`` is walked the same way (a ``stat`` when it has none).
    #1181 had kept checked-out submodules to the stash call alone; #1198 showed
    their commits are lost without the parent's ``git status`` showing anything
    (HEAD moved back to the recorded commit, or the commit recorded and pushed
    in the parent), and not deleting wins over the call count.

    Unknown is work too, as for the parent: a stash or count git cannot read is
    reported, never read as "none" — and neither is a ``.git/modules`` that
    cannot be listed or stat'ed (#1242).

    Every repo whose count came back 0 is appended to *pushed*, for the
    network check the caller may make (#1245).
    """
    pushed = [] if pushed is None else pushed
    found: list[str] = []
    seen: set[Path] = set()
    found += _checked_out_submodule_stashes(
        path, prefix=prefix, depth=depth, seen=seen, pushed=pushed
    )
    if depth == 0:
        modules = Path(path) / ".git" / "modules"
        kind = _kind(modules)
        if kind is None:
            found.append(_named(prefix, _MODULES_UNREADABLE))
        elif kind == "dir":
            found += _module_dir_stashes(modules, prefix="", depth=0, seen=seen, pushed=pushed)
    return found


def _checked_out_submodule_stashes(
    path: str, *, prefix: str, depth: int, seen: set[Path], pushed: list[_Repo] | None = None
) -> list[str]:
    pushed = [] if pushed is None else pushed
    listed = _run(["git", "ls-files", "--stage", "-z"], cwd=path)
    if listed.returncode != 0:
        if depth == 0:
            # .git/modules is still walked (#1181); checked-out ones are unknown.
            return ["submodules unknown (git ls-files failed)"]
        # #1227: nested repos in this submodule could not be listed — keep.
        return [f"{prefix.rstrip('/')}: nested repos unknown (git ls-files failed)"]
    found: list[str] = []
    for entry in listed.stdout.split("\0"):
        meta, _, rel = entry.partition("\t")
        if not meta.startswith("160000 ") or not rel:
            continue
        sub = Path(path) / rel
        dot = _kind(sub / ".git")
        if dot is None:  # #1242: cannot tell whether it is checked out
            found.append(f"{prefix}{rel}: {_MODULES_UNREADABLE}")
            continue
        if dot == "missing":
            continue  # not checked out — its git dir, if any, is walked below (#1181)
        if depth >= _MAX_SUBMODULE_DEPTH:
            # #1198: past the limit a nested repo is unknown, never "nothing".
            return [f"{prefix.rstrip('/')}: nested repos too deep to check"]
        found += _repo_notes(sub, f"{prefix}{rel}", depth=depth, seen=seen, pushed=pushed)
    return found


def _repo_notes(
    sub: Path, name: str, *, depth: int, seen: set[Path], pushed: list[_Repo]
) -> list[str]:
    """Stash, unpushed commits and nested repos of the checked-out repo *sub*."""
    found: list[str] = []
    linked = _linked_git_dir(sub)
    if linked is not None:
        seen.add(linked)
    note = _stash_note(name, _stash_entries(str(sub)))
    if note:
        found.append(note)
    # #1198: the parent's status says nothing about commits that are on a
    # branch HEAD left, or that the parent recorded and pushed.
    own_git_dir = linked if linked is not None else sub / ".git"  # .git exists here
    count, tag_only = _local_only_commits(str(sub), own_git_dir)
    note = _unpushed_note(name, count)
    if note:
        found.append(note)
    elif count == 0:
        pushed.append(_Repo(name, str(sub), False, own_git_dir, tag_only))
    found += _checked_out_submodule_stashes(
        str(sub), prefix=f"{name}/", depth=depth + 1, seen=seen, pushed=pushed
    )
    # #1198: an embedded repo (``.git`` a directory) keeps its own
    # submodules' git dirs in ``.git/modules``; deinit'd ones are only there.
    # After the line above, so its checked-out submodules are in *seen*.
    if linked is None:
        own_modules = sub / ".git" / "modules"
        kind = _kind(own_modules)
        if kind is None:
            found.append(f"{name}: {_MODULES_UNREADABLE}")
        elif kind == "dir":
            found += _module_dir_stashes(
                own_modules, prefix=f"{name}/", depth=depth + 1, seen=seen, pushed=pushed
            )
    return found


def _module_dir_stashes(
    modules: Path,
    *,
    prefix: str,
    depth: int,
    seen: set[Path],
    pushed: list[_Repo] | None = None,
) -> list[str]:
    """Stashes and unpushed commits in the git dirs under a ``modules`` dir
    that no checkout claimed (#1181, #1198).

    A module name may hold ``/`` (``libs/foo`` → ``modules/libs/foo``), so a
    dir that is no git dir is descended into; a git dir's own ``modules`` holds
    its nested submodules.

    #1242: a dir that cannot be listed, or an entry that cannot be stat'ed, is
    unknown — reported, never skipped as "no git dir here" and never raised.
    """
    pushed = [] if pushed is None else pushed
    if depth >= _MAX_SUBMODULE_DEPTH * 4:
        # #1198: past the bound whatever is there is unknown, never "nothing".
        try:
            anything = any(True for _ in modules.iterdir())
        except OSError:
            anything = True
        return [f"{prefix.rstrip('/')}: modules too deep to check"] if anything else []
    found: list[str] = []
    try:
        children = sorted(modules.iterdir())
    except OSError:
        return [_named(prefix, _MODULES_UNREADABLE)]
    for child in children:
        name = f"{prefix}{child.name}"
        kind = _kind(child)
        head = _kind(child / "HEAD") if kind == "dir" else "missing"
        objects = _kind(child / "objects") if kind == "dir" else "missing"
        nested = _kind(child / "modules") if kind == "dir" else "missing"
        if None in (kind, head, objects, nested):
            found.append(f"{name}: {_MODULES_UNREADABLE}")
            continue
        if kind != "dir":
            continue
        if not (head == "file" and objects == "dir"):
            found += _module_dir_stashes(
                child, prefix=f"{name}/", depth=depth + 1, seen=seen, pushed=pushed
            )
            continue
        if child.resolve() not in seen:
            note = _stash_note(name, _stash_entries(str(child), git_dir=True))
            if note:
                found.append(note)
            count, tag_only = _local_only_commits(str(child), child, git_dir=True)
            note = _unpushed_note(name, count)
            if note:
                found.append(note)
            elif count == 0:
                pushed.append(_Repo(name, str(child), True, child, tag_only))
        if nested == "dir":
            found += _module_dir_stashes(
                child / "modules", prefix=f"{name}/", depth=depth + 1, seen=seen, pushed=pushed
            )
    return found


#: Why a submodule's commits could not be counted (#1198).
_WORKTREE_UNREADABLE = "worktree HEAD unreadable"


def _worktree_tips(git_dir: Path) -> list[str] | None:
    """Commits the linked worktrees of *git_dir* hold: detached HEADs and per-worktree refs.

    ``git worktree add --detach`` keeps its HEAD in ``worktrees/<name>/HEAD``,
    and ``refs/worktree/*`` / ``refs/bisect/*`` made there in
    ``worktrees/<name>/refs/`` — a ``rev-list --all`` run elsewhere does not
    see those refs (git 2.43), and the commits may be on nothing else (#1198).
    Plain file reads, no git call. A symbolic ``ref: …`` is skipped: its target
    is a shared ref (``--all``) or one of the files read here.

    ``None`` when anything cannot be read or is no object name — unknown is
    work, never "nothing to lose". No ``worktrees/`` at all is ``[]``.
    """
    try:
        listed = sorted((git_dir / "worktrees").iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        return None
    # Through _kind: ``Path.is_dir()`` raised PermissionError on a 000 entry
    # (Python 3.10-3.12, and for ``refs`` on 3.13) — or read it as "no refs".
    kinds = [_kind(p) for p in listed]
    if None in kinds:
        return None
    entries = [p for p, kind in zip(listed, kinds, strict=True) if kind == "dir"]
    tips: list[str] = []
    for entry in entries:
        files = [entry / "HEAD"]
        refs = entry / "refs"
        refs_kind = _kind(refs)
        if refs_kind is None:
            return None
        if refs_kind == "dir" and not _ref_files(refs, files, set()):
            return None
        for f in files:
            try:
                line = f.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return None
            if line.startswith("ref: "):
                continue
            if not _SHA_RE.match(line.lower()):
                return None
            tips.append(line.lower())
    return sorted(set(tips))


def _ref_files(directory: Path, out: list[Path], visited: set[str]) -> bool:
    """Every file under *directory*, following directory symlinks as git does.

    ``os.walk`` does not follow them, and a ref behind a symlinked directory
    was not counted (#1198). A directory already visited (by real path) is
    skipped, so a symlink loop ends. ``False`` when anything cannot be listed
    or is neither a file nor a directory (a dangling link) — unknown.
    """
    real = os.path.realpath(directory)
    if real in visited:
        return True
    visited.add(real)
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return False
    for entry in entries:
        try:
            if entry.is_dir():  # follows symlinks
                if not _ref_files(Path(entry.path), out, visited):
                    return False
            elif entry.is_file():
                out.append(Path(entry.path))
            else:
                return False
        except OSError:
            return False
    return True


def _own_git_dir(checkout: Path) -> Path | None:
    """The git dir of a checkout: its ``.git`` directory, or where a ``.git`` file points."""
    dot = checkout / ".git"
    return dot if dot.is_dir() else _linked_git_dir(checkout)


def _local_only_commits(
    path: str, own_git_dir: Path, *, git_dir: bool = False
) -> tuple[int | str, frozenset[str]]:
    """Commits in a submodule that exist on no remote (#1198).

    Every local ref counts (``--all``: branches, tags, notes, ``refs/original``,
    ``refs/keep/*``, HEAD and the linked worktrees' HEADs), plus what the
    linked worktrees hold on their own (:func:`_worktree_tips`), ``--not
    --remotes``. A local tag is never proof of a push — it lives in the same
    git dir that deleting removes; a fetched tag on a pushed commit is
    reachable from the remotes and so adds nothing. Commits only tags reach
    are returned apart, uncounted (#1246): the caller asks the remote about
    them (:func:`_tag_only_note`) only when nothing else keeps the workspace,
    and keeps them unless a tag upstream vouches. The stash is left out (``--exclude``): it is
    counted on its own, as ``N stash entries``. A repo with no remote counts
    every commit — there are no remote refs to subtract.

    Broader than the workspace's own count (:func:`_unpushed_commits`, HEAD
    and branches, then ``ls-remote``): no network here, so every ref but the
    tags is assumed to be work.

    One git call, a second (the tag-only commits) when the count is not 0. No
    ``--ignore-missing``: it also silenced a branch whose tip object was gone,
    and the commits below it were deleted. So a ref or worktree HEAD naming a
    missing object fails the call — unknown, kept (an accepted over-keep).
    ``--all`` skips an unborn HEAD by itself. Unknown comes back as the reason
    (a ``str``) — the caller keeps the dir then.

    With *git_dir*, *path* is a git dir whose checkout may be gone — as in
    :func:`_stash_entries`, it stands in as its own work tree. *own_git_dir*
    is the git dir holding ``worktrees/``.
    """
    none: frozenset[str] = frozenset()
    extra = _worktree_tips(own_git_dir)
    if extra is None:
        return _WORKTREE_UNREADABLE, none
    pre = _git_pre(path, git_dir)
    # The worktree tips go through stdin: thousands of ref files overflow argv.
    # A ``--not`` on the command line does not reach them (they stay positive).
    result = _run(
        [*pre, "rev-list", "--count", "--exclude=refs/stash", f"--exclude={_PROBE_NS}*", "--all"]
        + ["--not", "--remotes", "--stdin"],
        cwd=path,
        stdin="".join(f"{sha}\n" for sha in extra),
    )
    if result.returncode != 0:
        return "git failed", none
    try:
        n = int(result.stdout.strip() or "0")
    except ValueError:
        return "git failed", none
    if not n:
        return 0, none
    held = _tag_only_commits(path, pre, extra)
    if held is None:
        return "git failed", none
    # Tag-only commits next to other local-only ones are not asked about: the
    # dir is kept anyway, and the reason names what is surely work (#903).
    return n - len(held), frozenset(held) if n == len(held) else none


def _tag_only_commits(
    path: str, pre: list[str], extra: list[str], vouch: tuple[str, ...] = ()
) -> set[str] | None:
    """Commits reachable from tags and from nothing else :func:`_local_only_commits`
    counts or subtracts — nor from *vouch* (#1246). ``None``: git failed.

    The stash and the probe refs are left out on both sides, so the two
    counts add up. Stdin revs are not flipped by ``--not``: hence the ``^``.
    """
    cmd = [*pre, "rev-list", "--tags", "--not", "--exclude=refs/stash"]
    cmd += [f"--exclude={_PROBE_NS}*", "--exclude=refs/tags/*", "--all", "--remotes", "--stdin"]
    stdin = "".join(f"^{t}\n" for t in (*extra, *vouch))
    result = _run(cmd, cwd=path, stdin=stdin)
    if result.returncode != 0:
        return None
    return set(result.stdout.split())


def _tags_not_checked(repo: _Repo) -> str:
    """``"sub: 2 commit(s) only on tags (not checked upstream)"`` (#1246, W57 round 14)."""
    return f"{repo.name}: {len(repo.tag_only)} commit(s) only on tags (not checked upstream)"


def _tag_only_note(repo: _Repo, ws_root: str, sessions_root: str | None) -> str | None:
    """Are the commits only *repo*'s tags reach on the remote? (#1246)

    #1198 counted them all, so an upstream release tag on a commit on no
    branch (its release branch deleted) kept an untouched workspace forever.
    The remotes are asked (``git ls-remote --tags``, by resolved URL from a
    neutral directory, 20 s, cached 60 s per URL apart from the heads): a
    local tag whose name and object are both upstream vouches for what it
    reaches. Every other tag-only commit is work — the reflogs never decide
    that one came from upstream (review of 4e4bd18: a commit made in a removed
    worktree, by ``commit-tree``, or as a dropped stash has no trace there,
    and ``git tag wip`` on it was its only copy).

    Called only when nothing else keeps the workspace (#903), and the caller
    stops at the first note. No remote vouches when its URL is a path inside
    *ws_root* or anywhere under *sessions_root* — another workspace may be
    deleted in the same sweep, and two that were each other's remote vouched
    for each other's ``wip`` (W57 round 12). With *sessions_root* unknown, no
    local-path remote outside the workspace vouches either. A remote that
    cannot be asked is unknown, kept.
    """
    pre = _git_pre(repo.path, repo.git_dir)
    config = _remote_config(repo.path, pre)
    if config is None:
        return f"{repo.name}: unpushed commits unknown (git failed)"
    no_copy = _no_copy_remotes(repo.path, pre, config, ws_root, sessions_root)
    asked: list[tuple[str, str]] = []
    for name, url in sorted(config.urls.items()):
        if name in no_copy:
            continue  # may be deleted too: no copy
        asked.append((name, url))
    vouch: set[str] = set()
    if asked:
        tags = _run(
            [*pre, "for-each-ref", "--format=%(objectname) %(refname)", "refs/tags/"],
            cwd=repo.path,
        )
        if tags.returncode != 0:
            return f"{repo.name}: unpushed commits unknown (git failed)"
        upstream: list[dict[str, str]] = []
        for name, url in asked:
            got = _ls_remote_refs(repo.path, name, url, pre, tags=True)
            if got is None:
                return f"{repo.name}: unpushed commits unknown (ls-remote failed)"
            upstream.append(got)
        for line in tags.stdout.splitlines():
            sha, _, ref = line.partition(" ")
            if sha and any(got.get(ref) == sha for got in upstream):
                vouch.add(sha)  # same name, same object upstream: a copy exists there
    left = set(repo.tag_only)
    if vouch:
        extra = _worktree_tips(repo.own_git_dir)
        if extra is None:
            return f"{repo.name}: unpushed commits unknown ({_WORKTREE_UNREADABLE})"
        rest = _tag_only_commits(repo.path, pre, extra, tuple(sorted(vouch)))
        if rest is None:
            return f"{repo.name}: unpushed commits unknown (git failed)"
        left &= rest
    return f"{repo.name}: {len(left)} unpushed commit(s)" if left else None


def _unpushed_note(name: str, n: int | str) -> str | None:
    """``"<name>: N unpushed commit(s)"`` (#1198), or ``None`` with none.

    A ``str`` is why the count is unknown — reported, so the dir is kept.
    """
    if isinstance(n, str):
        return f"{name}: unpushed commits unknown ({n})"
    return f"{name}: {n} unpushed commit(s)" if n else None


def _git_pre(path: str, git_dir: bool) -> list[str]:
    """``git`` with the git dir standing in as its own work tree (see :func:`_stash_entries`)."""
    return ["git", f"--git-dir={path}", f"--work-tree={path}"] if git_dir else ["git"]


class _RemoteConfig(NamedTuple):
    """What ``git config`` says about a repo's remotes (:func:`_remote_config`)."""

    #: Remote name → its first ``url`` (what ``ls-remote`` is asked by).
    urls: dict[str, str]
    #: ``core.logAllRefUpdates`` is on.
    logged: bool
    #: Remote name → every ``url`` and ``pushurl`` as configured (#1265).
    every: dict[str, list[str]]
    #: Some ``url.<base>.insteadOf`` / ``pushInsteadOf`` may rewrite them (#1265).
    rewrites: bool


def _remote_config(path: str, pre: list[str]) -> _RemoteConfig | None:
    """Remotes' URLs and push URLs, URL rewrites, and whether ref updates are
    logged, in one ``git config`` call.

    ``core.logAllRefUpdates`` is on unless set to ``false`` (a module git dir is
    non-bare). ``None``: git failed.
    """
    result = _run(
        [*pre, "config", "--get-regexp"]
        + [
            r"^(remote\..*\.(url|pushurl)|core\.logallrefupdates"
            r"|url\..*\.(insteadof|pushinsteadof))$"
        ],
        cwd=path,
    )
    if result.returncode == 1:
        return _RemoteConfig({}, True, {}, False)  # none set: no remote, default logging
    if result.returncode != 0:
        return None
    urls: dict[str, str] = {}
    every: dict[str, list[str]] = {}
    logged = True
    rewrites = False
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "core.logallrefupdates":
            logged = value.strip().lower() not in ("false", "no", "off", "0")
        elif key.startswith("url.") and key.endswith(("insteadof", "pushinsteadof")):
            rewrites = True
        elif key.startswith("remote.") and key.endswith((".url", ".pushurl")) and value:
            name, _, var = key[len("remote.") :].rpartition(".")
            every.setdefault(name, []).append(value)
            if var == "url":
                urls.setdefault(name, value)
    return _RemoteConfig(urls, logged, every, rewrites)


def _no_copy_remotes(
    path: str, pre: list[str], config: _RemoteConfig, ws_root: str, sessions_root: str | None
) -> frozenset[str]:
    """The configured remotes that are no copy of anything (#1265, review of f870f5b).

    A remote is no copy when **any** of its URLs or push URLs is
    (:func:`_no_copy`): a push through a ``pushurl`` into another workspace
    updates ``refs/remotes/<name>/*`` just as a fetch does. URLs are taken as
    git uses them: when an ``insteadOf`` / ``pushInsteadOf`` rule exists, they
    are asked from git (``git remote get-url --all`` and ``--push --all``) —
    ``wsb:x`` looks like a network URL but may name a neighbour workspace. A
    remote whose URLs git cannot tell is no copy either: if in doubt, keep.
    """
    out: set[str] = set()
    for name, configured in config.every.items():
        resolved: list[str] = []
        if config.rewrites:
            for push in ((), ("--push",)):
                got = _run([*pre, "remote", "get-url", *push, "--all", "--", name], cwd=path)
                if got.returncode != 0:
                    resolved = []
                    out.add(name)
                    break
                resolved += got.stdout.splitlines()
        if name in out:
            continue
        if any(_no_copy(path, u, ws_root, sessions_root) for u in [*configured, *resolved]):
            out.add(name)
    return frozenset(out)


def _local_remote_path(cwd: str, url: str) -> str | None:
    """The real path a local remote URL names; ``None`` for a network URL."""
    if url.startswith("file://"):
        rest = url[len("file://") :]
        url = rest[rest.index("/") :] if "/" in rest else rest  # file://host/path
        # git decodes %xx in a file:// URL: ``s%65ssions`` is ``sessions`` (W57 round 16).
        url = unquote(url)
    elif _NETWORK_URL_RE.match(url):
        return None
    return os.path.realpath(os.path.join(os.path.realpath(cwd), url))


def _inside(path: str | None, root: str) -> bool:
    if path is None:
        return False
    real_root = os.path.realpath(root)
    return path == real_root or path.startswith(real_root.rstrip("/") + "/")


def _no_copy(cwd: str, url: str, ws_root: str, sessions_root: str | None) -> bool:
    """Is the remote at *url* no copy at all — it may be deleted with this workspace?

    A local path inside *ws_root* is deleted with it; one under *sessions_root*
    is another workspace, which the same sweep may delete (#1246 tags, #1265
    branches: two workspaces that were each other's remote vouched for each
    other's only copy, and one sweep deleted both). With *sessions_root*
    unknown, no local path outside the workspace is trusted either — keep.
    A network URL (ssh, https, ``host:path``) always may vouch.
    """
    local = _local_remote_path(cwd, url)
    if local is None:
        return False
    return sessions_root is None or _inside(local, ws_root) or _inside(local, sessions_root)


def _pushed_from_here(git_dir: Path, ref: str, logged: bool) -> bool:
    """Was *ref* (``refs/remotes/<r>/<b>``) ever updated by a push from this clone?

    Its reflog says so: ``update by push``. A ref that only ``clone`` /
    ``fetch`` / ``pull`` wrote holds what came *from* the remote — trusted
    without asking. ``git clone`` writes no reflog for the refs it creates, so
    a missing reflog is "from the clone" while ref updates are logged; with
    logging off (or the reflog unreadable) nothing can be told — verify.
    """
    try:
        data = (git_dir / "logs" / ref).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return not logged
    except OSError:
        return True
    return any(line.split(b"\t", 1)[-1].startswith(b"update by push") for line in data.splitlines())


def _writes_a_commit_here(msg: bytes) -> bool:
    """Does a reflog entry with message *msg* write a commit made in this clone? (#1245)

    Only the action — the text before the first ``": "`` — is read; the rest
    is the commit's subject, and a subject saying "fast-forward" or "(start)"
    hid that commit (review of 28f29b3).

    - ``commit…`` (``(amend)``, ``(merge)``, ``(initial)``), ``cherry-pick``,
      ``revert``, ``am``: always.
    - ``rebase…`` / ``pull --rebase…``: each rewritten commit (``pick``,
      ``reword``, ``squash``, ``fixup``, ``edit``), not ``(start)`` (checks out
      upstream) nor ``(finish)`` (writes the last tip — upstream's own tip when
      nothing was rebased; the commits rewritten were logged as picks).
    - ``merge…`` / ``pull…``: unless the whole message ends ``: Fast-forward``.
    """
    action = msg.split(b": ", 1)[0]
    if action.startswith((b"rebase", b"pull --rebase")):
        return b"(start)" not in action and b"(finish)" not in action
    if action.startswith((b"merge", b"pull")):
        return not msg.rstrip().endswith(b": Fast-forward")
    return action.startswith((b"commit", b"cherry-pick", b"revert")) or action == b"am"


def _created_here(git_dir: Path, logged: bool) -> set[str] | None:
    """Commits created in this clone, read from its reflogs. ``None``: cannot tell.

    HEAD's reflog, every ``logs/refs/heads/*`` and each linked worktree's
    ``logs/HEAD``; the new side of each entry whose message says a commit was
    written here (:func:`_writes_a_commit_here`). Every entry of
    ``logs/refs/stash``: a stash commit is made here whatever its message.
    With ref logging off, or HEAD's reflog missing, empty or unreadable
    (expired — ``reflog expire --all``), it is unknown. A commit whose
    entries expired while HEAD's reflog kept later ones reads as not created
    here — a known limit (spec).
    """
    if not logged:
        return None
    files = [git_dir / "logs" / "HEAD"]
    heads = git_dir / "logs" / "refs" / "heads"
    kind = _kind(heads)
    if kind is None or (kind == "dir" and not _ref_files(heads, files, set())):
        return None
    try:
        worktrees = sorted((git_dir / "worktrees").iterdir())
    except (FileNotFoundError, NotADirectoryError):
        worktrees = []
    except OSError:
        return None
    for wt in worktrees:
        log = wt / "logs" / "HEAD"
        wt_kind = _kind(log)
        if wt_kind is None:
            return None
        if wt_kind == "file":
            files.append(log)
    stash = git_dir / "logs" / "refs" / "stash"
    stash_kind = _kind(stash)
    if stash_kind is None:
        return None
    if stash_kind == "file":
        files.append(stash)
    created: set[str] = set()
    for i, f in enumerate(files):
        every = f == stash
        try:
            data = f.read_bytes()
        except OSError:
            return None
        if i == 0 and not data.strip():
            return None  # HEAD's reflog emptied: nothing can be told
        for line in data.splitlines():
            meta, _, msg = line.partition(b"\t")
            parts = meta.split()
            if len(parts) < 2 or not (every or _writes_a_commit_here(msg)):
                continue
            new = parts[1].decode("ascii", "replace").lower()
            if _SHA_RE.match(new) and set(new) != {"0"}:
                created.add(new)
    return created


def _refs_containing(path: str, pre: list[str], commits: set[str]) -> frozenset[str] | None:
    """The tracking refs that reach any of *commits* (one ``cat-file``, one ``for-each-ref``).

    Commits no longer in the object store are dropped first (``--contains``
    fails on them). ``None``: git failed.
    """
    check = _run(
        [*pre, "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        cwd=path,
        stdin="".join(f"{c}\n" for c in sorted(commits)),
    )
    if check.returncode != 0:
        return None
    present = [line.split()[0] for line in check.stdout.splitlines() if line.endswith(" commit")]
    if not present:
        return frozenset()
    contains = [f"--contains={c}" for c in present]
    result = _run(
        [*pre, "for-each-ref", "--format=%(refname)", *contains, "refs/remotes/"], cwd=path
    )
    if result.returncode != 0:
        return None
    return frozenset(result.stdout.split())


#: Where 3eaa7da's probe fetch wrote its throwaway ref. Never counted as work
#: (a sweep killed before its ``finally`` left one, and it kept the workspace
#: forever — review of 3eaa7da); leftovers are removed before a probe.
_PROBE_NS = "refs/clord-probe/"


def _drop_leftover_probes(cwd: str, pre: list[str], git_dir: Path) -> None:
    """Best effort: delete ``refs/clord-probe/*`` and their ``.lock`` files."""
    listed = _run([*pre, "for-each-ref", "--format=%(refname)", _PROBE_NS], cwd=cwd)
    for ref in listed.stdout.split() if listed.returncode == 0 else []:
        _run([*pre, "update-ref", "-d", ref], cwd=cwd)
    for current, _dirs, files in os.walk(git_dir / _PROBE_NS):
        for name in files:
            if name.endswith(".lock"):
                with suppress(OSError):
                    os.unlink(os.path.join(current, name))


def _probe_contains(
    cwd: str,
    pre: list[str],
    git_dir: Path,
    url: str,
    branches: tuple[str, ...],
    olds: list[str],
    *,
    stop_at_miss: bool = False,
    fingerprint: str = "",
) -> set[str] | None:
    """Which of *olds* are on one of the remote's *branches*? (#1245)

    *branches* are branch names, or ``("*",)`` for every head. One bounded
    fetch of those branches only — into a throwaway git dir in a temporary
    directory that borrows the workspace's objects (``objects/info/alternates``),
    so the workspace gets no ref, no object and no config change, and none of
    its hooks or config run (review of 3eaa7da). No tags, no ``FETCH_HEAD``,
    no submodules, no auto gc; through :func:`_run` with the ``ls-remote``
    timeout (20 s). All of *olds* are answered from that one fetch (W57 round
    13: each gone ref fetched every branch again); with *stop_at_miss* it stops
    at the first one not found — one is enough to keep.

    Answers are cached with the ``ls-remote`` answers for the TTL, by (resolved
    URL, branches, *fingerprint* — the remote heads asked about), per commit.
    ``None`` — the fetch failed or timed out — is unknown, and cached per URL:
    a remote that hangs costs one timeout per URL, whichever branches are
    asked next (W57 round 13).
    """
    url_key = _ls_remote_key(cwd, url)
    fail_key = f"probe-failed\0{url_key}"
    key = f"probe\0{url_key}\0{' '.join(branches)}\0{fingerprint}"
    fresh = time.monotonic() - _LS_REMOTE_TTL_SECONDS
    with _ls_remote_lock:
        failed = _ls_remote_cache.get(fail_key)
        cached = _ls_remote_cache.get(key)
    if failed is not None and failed[0] > fresh:
        return None
    known = cached[1] if cached is not None and cached[0] > fresh and cached[1] else {}
    if all(old in known for old in olds) or (
        stop_at_miss and any(known.get(old) == "no" for old in olds)
    ):
        return {old for old in olds if known.get(old) == "yes"}
    _drop_leftover_probes(cwd, pre, git_dir)
    found: set[str] | None = None
    answered: dict[str, str] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="clord-probe-") as tmp:
            neutral = _neutral_env(tmp)
            (Path(tmp) / "objects" / "info" / "alternates").write_text(
                f"{(git_dir / 'objects').resolve()}\n", encoding="utf-8"
            )
            probe = ["git", f"--git-dir={tmp}"]
            fetched = _run(
                [*probe, "-c", "gc.auto=0", "-c", "maintenance.auto=false", "fetch"]
                + ["--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules"]
                + ["--", _remote_target(cwd, url)]
                + [f"+refs/heads/{b}:refs/probe/{b}" for b in branches],
                cwd=tmp,
                timeout=_LS_REMOTE_TIMEOUT_SECONDS,
                extra_env={"GIT_TERMINAL_PROMPT": "0", **neutral},
            )
            if fetched.returncode == 0:
                found = set()
                for old in olds:
                    holding = _run(
                        [*probe, "for-each-ref", "--count=1", "--format=%(refname)"]
                        + [f"--contains={old}", "refs/probe/"],
                        cwd=tmp,
                        extra_env=neutral,
                    )
                    if holding.returncode != 0:
                        found = None
                        break
                    answered[old] = "yes" if holding.stdout.strip() else "no"
                    if holding.stdout.strip():
                        found.add(old)
                    elif stop_at_miss:
                        break
    except OSError:
        found = None
    with _ls_remote_lock:
        if found is None:
            _ls_remote_cache[fail_key] = (time.monotonic(), None)
        else:
            _ls_remote_cache[key] = (time.monotonic(), {**known, **answered})
    return found


def _count_unvouched(path: str, pre: list[str], extra: list[str], vouch: set[str]) -> int | None:
    """Commits from every local ref (stale tracking refs too) but none of *vouch*."""
    # Same tips as _local_only_commits, but ``--all`` now counts the tracking
    # refs that do not vouch too; only those that do are subtracted. Tags are
    # left out (#1246): what only tags reach was judged by _tag_only_note
    # already, and an upstream tag that vouched there must not count here.
    result = _run(
        [*pre, "rev-list", "--count", "--exclude=refs/stash", f"--exclude={_PROBE_NS}*"]
        + ["--exclude=refs/tags/*", "--all", "--stdin"],
        cwd=path,
        stdin="".join(f"{t}\n" for t in extra) + "".join(f"^{v}\n" for v in sorted(vouch)),
    )
    try:
        return int(result.stdout.strip() or "0") if result.returncode == 0 else None
    except ValueError:
        return None


def _on_other_remote_heads(
    repo: _Repo, pre: list[str], url: str, heads: dict[str, str], present: set[str], shas: list[str]
) -> set[str]:
    """Which of *shas* (their branches gone upstream) are on one of the remote's other heads?

    The default branch first (``ls-remote --symref``), then every head — each
    fetched at most once per check, all *shas* answered from it (W57 round 13).
    Heads already here are skipped (they vouch locally). A failed fetch is
    unknown: nothing more is fetched from that remote (its failure is cached
    per URL) and the commits count — the workspace is kept.
    """
    reached: set[str] = set()
    fingerprint = ",".join(sorted(_head_shas(heads)))
    default = heads.get(_DEFAULT_BRANCH, "")
    if default in heads and heads[default] not in present:
        got = _probe_contains(
            repo.path,
            pre,
            repo.own_git_dir,
            url,
            (default[len("refs/heads/") :],),
            shas,
            fingerprint=fingerprint,
        )
        if got is None:
            return reached
        reached |= got
    left = [s for s in shas if s not in reached]
    others = [r for r in heads if r.startswith("refs/heads/") and r != default]
    if left and any(heads[r] not in present for r in others):
        got = _probe_contains(
            repo.path,
            pre,
            repo.own_git_dir,
            url,
            ("*",),
            left,
            stop_at_miss=True,
            fingerprint=fingerprint,
        )
        reached |= got or set()
    return reached


def _stale_tracking_note(repo: _Repo, ws_root: str, sessions_root: str | None = None) -> str | None:
    """Does a remote-tracking ref in *repo* vouch for commits the remote no longer has? (#1245)

    :func:`_local_only_commits` counts a commit as pushed when a local
    ``refs/remotes/*`` reaches it. A branch pushed and then deleted on the
    remote (a PR closed with its branch) leaves that ref behind, and the
    commits — whose only copy is in this git dir — read as pushed.

    Only a tracking ref this clone **pushed to** can vouch for work made here,
    so only those are checked (:func:`_pushed_from_here`); refs that only the
    clone or a fetch wrote hold what came from the remote and are trusted with
    no network call — upstream activity (``main`` moving on, a force-pushed
    ``dependabot/*``, a feature branch deleted) never keeps a workspace with no
    work of its own (second review of 0e9b09c). A pushed-to ref is asked about
    (``git ls-remote --heads``, cached per URL) and vouches when:

    - the branch is still on the remote at the same commit;
    - or it moved, and the remote's commit is here: that commit vouches for
      what it reaches (a fast-forward covers the old commits, a force-push
      does not).

    It moved to a commit not here (fast-forward, force-push, or deleted and
    recreated — indistinguishable without a fetch, and nothing is fetched):
    unknown, kept. Gone from the remote: vouches for nothing. A remote that
    cannot be asked: unknown, kept.

    A remote whose URL is a path inside *ws_root* is deleted with the
    workspace, so none of its refs vouch, pushed or fetched — as if there were
    no remote (second review). So is one under *sessions_root* — another
    workspace, which the same sweep may delete (#1265: two workspaces that were
    each other's remote vouched for each other's only copy) — and, with
    *sessions_root* unknown, any local path (:func:`_no_copy`). Such a remote is
    never asked nor probed — by any of its URLs or push URLs, as git rewrites
    them (:func:`_no_copy_remotes`). A ref of no configured remote never
    vouches (review of f870f5b: ``git fetch <neighbour> 'refs/heads/*:
    refs/remotes/b/*'`` names no remote, and where it came from is unknown).

    Cost: one ``for-each-ref``, one ``config``, reflog file reads; one
    ``ls-remote`` per remote URL (cached) only for pushed-to refs; a
    ``cat-file`` and a ``rev-list`` only when a ref does not vouch.
    """
    pre = _git_pre(repo.path, repo.git_dir)
    refs = _run(
        [*pre, "for-each-ref", "--format=%(objectname)%09%(symref)%09%(refname)", "refs/remotes/"],
        cwd=repo.path,
    )
    if refs.returncode != 0:
        return f"{repo.name}: unpushed commits unknown (git failed)"
    tracking: list[tuple[str, str]] = []
    for line in refs.stdout.splitlines():
        sha, symref, ref = (line.split("\t", 2) + ["", ""])[:3]
        if sha and ref and not symref:  # ``origin/HEAD`` names another tracking ref
            tracking.append((ref, sha))
    if not tracking:
        return None
    config = _remote_config(repo.path, pre)
    if config is None:
        return f"{repo.name}: unpushed commits unknown (git failed)"
    urls, logged = config.urls, config.logged
    names = sorted(config.every, key=len, reverse=True)  # ``a/b`` before ``a``
    inside = {n for n, u in urls.items() if _inside(_local_remote_path(repo.path, u), ws_root)}
    # #1265: a remote in another workspace (or any local one, root unknown) is no copy
    # either — by any of its URLs or push URLs, as git rewrites them.
    no_copy = _no_copy_remotes(repo.path, pre, config, ws_root, sessions_root)
    # Third review of ef8f0fc: a commit made here can reach a *fetched* ref (pushed by
    # URL or via another remote and fetched back, fast-forwarded upstream into
    # another branch), so the push reflog alone does not tell. The refs that
    # hold a commit created here are asked; with no usable HEAD reflog, all.
    created = _created_here(repo.own_git_dir, logged)
    holding = _refs_containing(repo.path, pre, created) if created else frozenset[str]()
    if holding is None:
        return f"{repo.name}: unpushed commits unknown (git failed)"
    vouch: set[str] = set()
    verify: list[tuple[str, str, str]] = []
    no_vouch = False
    elsewhere = False  # a no-copy remote outside the workspace (#1265)
    unconfigured = False
    for ref, sha in tracking:
        remote = next((n for n in names if ref.startswith(f"refs/remotes/{n}/")), None)
        if remote is None or remote not in urls:
            # Review of f870f5b: ``git fetch <path> 'refs/heads/*:refs/remotes/b/*'``
            # leaves refs of no remote — where they came from is unknown: keep.
            no_vouch = unconfigured = True
        elif remote in no_copy:
            no_vouch = True
            elsewhere = elsewhere or remote not in inside
        elif (
            created is not None
            and ref not in holding
            and not _pushed_from_here(repo.own_git_dir, ref, logged)
        ):
            vouch.add(sha)  # holds only what came from the remote (clone / fetch)
        else:
            verify.append((ref, sha, remote))
    if not no_vouch and not verify:
        return None
    heads: dict[str, dict[str, str]] = {}
    moved: list[tuple[str, str, str, str]] = []
    gone_refs: list[tuple[str, str]] = []
    gone = False
    for ref, sha, remote in verify:
        if remote not in heads:
            got = _ls_remote_refs(repo.path, remote, urls[remote], pre)
            if got is None:
                return f"{repo.name}: unpushed commits unknown (ls-remote failed)"
            heads[remote] = got
        now = heads[remote].get(f"refs/heads/{ref[len(f'refs/remotes/{remote}/') :]}")
        if now == sha:
            vouch.add(sha)
        elif now is None:
            gone = True
            gone_refs.append((sha, remote))
        else:
            moved.append((ref, sha, now, remote))
    if not no_vouch and not gone and not moved:
        return None
    # Which of the asked remotes' heads are here — those vouch for what they reach.
    remote_shas = sorted({s for h in heads.values() for s in _head_shas(h)})
    present: set[str] = set()
    if remote_shas:
        check = _run(
            [*pre, "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            cwd=repo.path,
            stdin="".join(f"{s}\n" for s in remote_shas),
        )
        if check.returncode != 0:
            return f"{repo.name}: unpushed commits unknown (git failed)"
        present = {
            line.split()[0] for line in check.stdout.splitlines() if line.endswith(" commit")
        }
    for ref, sha, now, remote in moved:
        if now in present:
            continue  # vouches below for what it reaches
        # Review of 28f29b3 (d5/d6): the normal PR flow — push, merge, pull — then
        # any later push upstream left this unknown forever. For a ref that holds
        # a commit made here, fetch that one branch once and ask git whether the
        # old tip is an ancestor. Never for other refs (untouched submodules
        # stay offline), never with created-here unknown.
        # W57 round 12: a ref verified for its push record alone (``git push
        # origin origin/main:refs/heads/release``) is probed too.
        branch = ref[len(f"refs/remotes/{remote}/") :]
        reached = (
            _probe_contains(
                repo.path, pre, repo.own_git_dir, urls[remote], (branch,), [sha], fingerprint=now
            )
            if created is not None
            else None
        )
        verdict = None if reached is None else sha in reached
        if verdict is None:
            # Fast-forward or force-push (or deleted and recreated) cannot be
            # told without the commit: unknown, kept.
            return (
                f"{repo.name}: unpushed commits unknown (remote branch moved to a commit not here)"
            )
        if verdict:
            vouch.add(sha)  # the old tip is on the live branch
        else:
            gone = True  # rewritten: the old tip vouches for nothing
    # A moved branch's new head vouches for what it reaches, and only that: the
    # old tracking ref's commits count unless they are its ancestors.
    vouch |= present
    extra = _worktree_tips(repo.own_git_dir)
    if extra is None:
        return f"{repo.name}: unpushed commits unknown ({_WORKTREE_UNREADABLE})"
    n = _count_unvouched(repo.path, pre, extra, vouch)
    if n and gone_refs and created is not None:
        # W57 round 12: a PR merged with a merge commit and its branch deleted —
        # GitHub's default — left the commit on the remote's ``main``, which this
        # clone never fetched. Probe the default branch, then every head; only
        # heads not here (those here already vouch above).
        by_remote: dict[str, list[str]] = {}
        for sha, remote in gone_refs:
            by_remote.setdefault(remote, []).append(sha)
        for remote, shas in by_remote.items():
            on_heads = _on_other_remote_heads(repo, pre, urls[remote], heads[remote], present, shas)
            vouch |= on_heads
            if len(on_heads) < len(shas):
                break  # one commit on no remote head already keeps the workspace
        n = _count_unvouched(repo.path, pre, extra, vouch)
    if n is None:
        return f"{repo.name}: unpushed commits unknown (git failed)"
    if not n:
        return None
    if gone or moved:
        return f"{repo.name}: {n} unpushed commit(s) (remote branch deleted or rewritten)"
    if unconfigured:
        return f"{repo.name}: {n} unpushed commit(s) (only on refs of no configured remote)"
    if elsewhere:
        where = "under the sessions dir" if sessions_root else "at a local path"
        return f"{repo.name}: {n} unpushed commit(s) (only on a remote {where})"
    return f"{repo.name}: {n} unpushed commit(s) (only on a remote inside the workspace)"


def _glob_escape(name: str) -> str:
    return re.sub(r"([*?\[\\])", r"\\\1", name)


def _count_not_on(
    path: str,
    tips: list[str],
    also_on: frozenset[str] = frozenset(),
    vouching: list[str] | tuple[str, ...] = (),
) -> int | None:
    """Commits reachable from *tips* but from none of the *vouching* tracking refs nor *also_on*.

    *also_on* are remote heads. A head whose commit is not in this clone is
    skipped: a branch pushed from here is present, and one that moved on since
    no longer vouches for anything — the commits then count, and the dir stays.
    """
    if not tips:
        return 0
    present = ""
    if also_on:
        check = _run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            cwd=path,
            stdin="\n".join(also_on),
        )
        if check.returncode != 0:
            return None
        present = "".join(
            f"^{line.split()[0]}\n"
            for line in check.stdout.splitlines()
            if line.endswith(" commit")
        )
    # Tips and the refs subtracted through stdin (#1198: a worktree with
    # thousands of refs overflowed argv). Only the *vouching* refs are
    # subtracted, by name — not ``--remotes``: a ref of a remote that is no
    # copy, or of no configured remote, subtracts nothing (#1265).
    result = _run(
        ["git", "rev-list", "--count", "--stdin"],
        cwd=path,
        stdin="".join(f"{t}\n" for t in tips) + "".join(f"^{v}\n" for v in vouching) + present,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return None


def _unpushed_commits(
    path: str, *, ask_remote: bool, root: str | None = None, sessions_root: str | None = None
) -> tuple[int | None, bool]:
    """How many commits exist here and on no remote branch (#903). ``None``: unknown.

    Counts from HEAD (a detached commit is on no branch), every local ref but
    tags and notes (branches, ``refs/original`` — #1198) and what the linked
    worktrees hold — their detached HEADs and per-worktree refs, read from
    files (#1198: ``git worktree add --detach`` elsewhere kept its commit
    only in this git dir). The stash is counted
    apart (#1045). A repository with no remote at all counts every commit —
    there is nowhere else they exist. A worktree file that cannot be read is
    unknown (``None``).

    With *ask_remote*, a commit at or below a branch head on a remote counts
    as pushed even without a local tracking ref (``git ls-remote``). Only exact
    heads — no guessing at squash merges. The second value is ``True`` when
    the remote could not be asked; the local count is returned then, so the
    dir is kept.

    A remote that may be deleted too (:func:`_no_copy_remotes`: any URL or
    push URL inside the workspace, under *sessions_root*, or any local path
    when that is unknown — #1265) is treated as no remote: its tracking refs
    are local work, it is not asked. So are the refs of no configured remote.
    """
    own = _own_git_dir(Path(path))
    extra = _worktree_tips(own) if own is not None else []
    if extra is None:
        return None, False
    config = _remote_config(path, ["git"])
    if config is None:
        return None, False
    # Third review of ef8f0fc (b3): a remote whose URL is inside the workspace
    # (``build/backup.git``) is deleted with it — neither its tracking refs
    # nor its ``ls-remote`` heads vouch for anything. #1265: nor one in another
    # workspace, which the same sweep may delete — by any URL or push URL, as
    # git rewrites them — nor a ref of no configured remote.
    no_copy = _no_copy_remotes(path, ["git"], config, root or path, sessions_root)
    trusted = frozenset(config.urls) - no_copy
    start = _start_refs(path, trusted)
    if start is None:
        return None, False
    refs, vouching = start
    tips = sorted(set(refs) | set(extra))
    local = _count_not_on(path, tips, vouching=vouching)
    if not ask_remote or not local:
        return local, False
    heads = _remote_head_shas(path, skip=frozenset(config.every) - trusted)
    if heads is None:
        return local, True
    return _count_not_on(path, tips, heads, vouching=vouching), False


#: How far below an ignored directory a cloned repo is looked for (#1241):
#: ``vendor/lib``, ``vendor/<org>/<pkg>``, ``node_modules/@scope/pkg``.
#: Deeper is not looked at — walking all of ``node_modules`` on every sweep
#: is what the regenerable list exists to avoid (known limit, spec).
_MAX_IGNORED_REPO_DEPTH = 2


def _ignored_repo_notes(path: str, rel: str, pushed: list[_Repo]) -> list[str]:
    """Work in git repos cloned into the ignored directory *rel* (#1241).

    An ignored ``vendor/`` is skipped as something a tool rebuilds — but a
    tool cannot rebuild a commit made in a repo cloned there, and deleting the
    workspace deleted its only copy. Each repo found (a ``.git`` dir or file,
    the ignored dir itself included) is judged like an embedded repo — stash,
    commits on no remote-tracking ref, its own submodules — plus its own
    ``git status`` (its changes show nowhere else). A directory that cannot be
    read is unknown, kept. Symlinks are not followed (deleting the workspace
    does not reach their target either).

    A bare repository there (``build/x.git`` — a push target, a mirror) that
    holds any ref is kept too: it is deleted with the workspace (W57 round 12).

    Stops at the first repo that keeps the workspace (W57 round 12: each slow
    repo cost its own timeout); what was not looked at is said as
    ``"<dir>: …and more not checked"``.
    """
    root = Path(path)
    found: list[str] = []
    stack = [(root / rel.rstrip("/"), 0)]
    while stack:
        if found:
            found.append(f"{rel.rstrip('/')}: …and more not checked")
            break
        here, depth = stack.pop()
        name = here.relative_to(root).as_posix()
        if here.is_symlink():
            continue
        dot = _kind(here / ".git")
        if dot is None:
            found.append(f"{name}: nested repos unknown (unreadable)")
            continue
        if dot != "missing":
            found += _ignored_repo_work(here, name, pushed)
            continue  # its own content is the repo's to judge
        bare = _bare_repo_note(here, name)
        if bare is not None:
            if bare:
                found.append(bare)
            continue  # a bare repo's content is objects, not work files
        if depth >= _MAX_IGNORED_REPO_DEPTH:
            continue
        try:
            with os.scandir(here) as entries:
                subdirs = sorted(e.path for e in entries if e.is_dir(follow_symlinks=False))
        except OSError:
            found.append(f"{name}: nested repos unknown (unreadable)")
            continue
        stack.extend((Path(d), depth + 1) for d in reversed(subdirs))
    return found


#: How many skipped ignored dirs :func:`_not_checked` names before ``(+N more)``.
_MAX_NAMED_UNCHECKED = 3


def _not_checked(dirs: list[str]) -> str:
    """``"…and more not checked: vendor/, build/ (+2 more)"``."""
    shown = ", ".join(dirs[:_MAX_NAMED_UNCHECKED])
    more = len(dirs) - _MAX_NAMED_UNCHECKED
    return f"…and more not checked: {shown}" + (f" (+{more} more)" if more > 0 else "")


def _bare_repo_note(here: Path, name: str) -> str | None:
    """``None``: *here* is no bare repo. ``""``: one with no ref. Else why it is kept."""
    kinds = [_kind(here / "HEAD"), _kind(here / "objects"), _kind(here / "refs")]
    if None in kinds:
        return f"{name}: nested repos unknown (unreadable)"
    if kinds != ["file", "dir", "dir"]:
        return None
    refs = _run(
        ["git", f"--git-dir={here}", "for-each-ref", "--count=1", "--format=%(refname)"],
        cwd=str(here),
    )
    if refs.returncode != 0:
        return f"{name}: bare repository unknown (git failed)"
    return f"{name}: bare repository (holds refs)" if refs.stdout.strip() else ""


def _ignored_repo_work(repo: Path, name: str, pushed: list[_Repo]) -> list[str]:
    found = _repo_notes(repo, name, depth=1, seen=set(), pushed=pushed)
    if found:
        return found  # kept already: its status is not needed (W57 round 12)
    # --ignored (review of 43caee1): the repo's own ignored files (a local
    # ``.env``) are in no commit either. What a tool rebuilds is not work, by
    # the workspace's own rule; any other ignored file or dir is.
    status = _run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=normal", "--ignored=matching"],
        cwd=str(repo),
    )
    if status.returncode != 0:
        found.append(f"{name}: status unknown (git failed)")
        return found
    for entry in status.stdout.split("\0"):
        xy, rel = entry[:2], entry[3:]
        if not entry.strip() or (xy == "!!" and _is_regenerable(rel)):
            continue
        found.append(f"{name}: uncommitted changes")
        break
    return found


def _is_clean_worktree(
    path: str, xy: str, rel: str, depth: int, root: str, sessions_root: str | None
) -> bool:
    """Is this entry a Claude Code worktree with nothing uncommitted inside?

    The parent's ``git status`` shows a nested checkout as one line and never
    looks in, so the worktree is opened and judged by the same rules.
    """
    if xy != "??" or not _WORKTREE_ENTRY_RE.match(rel) or depth >= _MAX_WORKTREE_DEPTH:
        return False
    # *root*: "inside the workspace" is the outer workspace, not the worktree.
    return worktree_status(
        str(Path(path) / rel), sessions_root=sessions_root, _depth=depth + 1, _root=root
    ).clean


def worktree_status(
    path: str,
    *,
    sessions_root: str | None = None,
    _depth: int = 0,
    _root: str | None = None,
) -> WorktreeStatus:
    """Classify ``git status`` of *path* into the user's changes and c-lord's (#749).

    Safety invariant (module docstring): anything that *might* be the user's
    uncommitted work lands in ``user_changes``. Only files c-lord writes itself
    are set aside, and a Claude Code worktree only when it is itself clean.

    Never raises: a file the checks cannot read, or any other failure (an
    undecodable name — second review of 0e9b09c), is unknown — the dir is
    kept, and a sweep goes on to the next one (review of 43caee1: a
    ``PermissionError`` stopped ``cleanup_orphaned`` and failed
    /workspace-delete with an exception).

    *sessions_root* is where every workspace lives (``SessionDirManager``'s
    base dir): a remote there may be deleted in the same sweep, so it vouches
    for no tag (#1246) and no branch (#1265). ``None``: no local-path remote
    outside *path* does either.
    """
    try:
        return _worktree_status(
            path, _depth=_depth, _root=_root or path, sessions_root=sessions_root
        )
    except OSError as exc:
        logger.warning("worktree status of %s unknown: %s", path, exc)
        return WorktreeStatus(is_repo=False, unknown=f"unreadable: {exc.strerror or exc}")
    except Exception as exc:  # noqa: BLE001 — any failure is unknown, never "clean"
        logger.exception("worktree status of %s unknown", path)
        return WorktreeStatus(is_repo=False, unknown=f"check failed: {type(exc).__name__}")


def _worktree_status(
    path: str, *, _depth: int, _root: str, sessions_root: str | None = None
) -> WorktreeStatus:
    # -uall: a wholly untracked ``.claude/`` must be listed file by file, or the
    # injected SKILL.md and a user's own file would hide behind one line.
    # -z: paths come back unquoted, so spaces and non-ASCII names parse as-is.
    # --ignored=matching (#903): ignored files are listed as ``!!`` — they are
    # work unless c-lord or a tool wrote them. ``matching`` does not descend
    # into an ignored directory (``node_modules/`` stays one line).
    result = _run(
        [
            "git",
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        cwd=path,
    )
    if result.returncode == _TIMEOUT_RETURNCODE:
        return WorktreeStatus(is_repo=False, unknown="git timed out")
    if result.returncode != 0:
        return WorktreeStatus(is_repo=False)

    user: list[str] = []
    clord: list[str] = []
    ignored_dirs: list[str] = []
    fields = iter(result.stdout.split("\0"))
    for entry in fields:
        if not entry.strip():
            continue
        xy, rel = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            next(fields, None)  # the source path of a rename/copy
        line = f"{xy} {rel}"
        if _is_clord_change(xy, rel) or _is_clean_worktree(
            path, xy, rel, _depth, _root, sessions_root
        ):
            clord.append(line)
        elif xy == "!!" and (
            _is_regenerable(rel) or (rel.endswith("/") and not _ignored_dir_holds_work(path, rel))
        ):
            if rel.endswith("/"):
                ignored_dirs.append(rel)  # #1241: a repo cloned there is not rebuilt
            continue
        else:
            user.append(line)
    stash = _stash_entries(path)
    # The network is asked only when nothing else keeps the dir (#903).
    unpushed, unchecked = _unpushed_commits(
        path, ask_remote=not user and stash == 0, root=_root, sessions_root=sessions_root
    )
    pushed: list[_Repo] = []
    nested = _submodule_stashes(path, pushed=pushed)
    for i, rel in enumerate(ignored_dirs):
        if user or stash != 0 or unpushed != 0 or nested:
            # Kept already: the rest is not searched (W57 round 12) — and the
            # reason says so, so a dry run never shows a partial list as the
            # whole (W57 round 13).
            nested.append(_not_checked(ignored_dirs[i:]))
            break
        nested += _ignored_repo_notes(path, rel, pushed)
    unasked = pushed
    if not user and stash == 0 and unpushed == 0 and not nested:
        # #1245: a nested repo's "pushed" rests on local tracking refs; ask the
        # remotes only now, as for the workspace, and stop at the first reason.
        # #1246: its tag-only commits likewise — never when something else keeps.
        unasked = []
        for i, repo in enumerate(pushed):
            note = _tag_only_note(repo, _root, sessions_root) if repo.tag_only else None
            note = note or _stale_tracking_note(repo, _root, sessions_root)
            if note:
                nested.append(note)
                unasked = pushed[i + 1 :]
                break
    # Kept already: tag-only commits not asked about are still named, never left
    # out of the reason (W57 round 14 — as 8b43fa8's "…and more not checked").
    nested += [_tags_not_checked(r) for r in unasked if r.tag_only]
    return WorktreeStatus(
        is_repo=True,
        user_changes=tuple(user),
        clord_files=tuple(clord),
        unpushed_commits=unpushed,
        remote_unchecked=unchecked,
        # unknown is work too: never call a dir clean we could not read
        stash_entries=1 if stash is None else stash,
        submodule_stashes=tuple(nested),
    )


#: How many changed paths a "keeping" log line names before it abbreviates.
_MAX_LOGGED_CHANGES = 5


def _printable(text: str) -> str:
    """*text* with undecodable bytes as ``\\xNN`` (third review of ef8f0fc).

    git's output is read with ``surrogateescape``; a lone surrogate must never
    reach Discord or the logs.
    """
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
    except UnicodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")


def describe_changes(status: WorktreeStatus) -> str:
    """Why a directory is being kept — :func:`_describe_changes`, printable."""
    return _printable(_describe_changes(status))


def _describe_changes(status: WorktreeStatus) -> str:
    """Why a directory is being kept, for the INFO line (#749, #903).

    Names the entries, because "uncommitted work" alone read as 80 dirs of user
    work when 60 of them held nothing but c-lord's own SKILL.md. Ignored files
    show as ``!! name``; commits on no remote branch as a count.
    """
    if not status.is_repo:
        if status.unknown:
            return f"status unknown ({status.unknown})"
        return "not a git repo (cannot tell work from leftovers)"
    reasons: list[str] = []
    if status.unpushed_commits is None:
        reasons.append("unpushed commits unknown (git rev-list failed or worktree unreadable)")
    elif status.unpushed_commits:
        reason = f"{status.unpushed_commits} unpushed commit(s) on no remote branch"
        if status.remote_unchecked:
            reason += " (ls-remote failed; pushed branches could not be checked)"
        reasons.append(reason)
    if status.stash_entries:
        n = status.stash_entries
        reasons.append(f"{n} stash {'entry' if n == 1 else 'entries'}")
    reasons.extend(status.submodule_stashes)
    if status.user_changes:
        shown = ", ".join(status.user_changes[:_MAX_LOGGED_CHANGES])
        more = len(status.user_changes) - _MAX_LOGGED_CHANGES
        if more > 0:
            shown += f" (+{more} more)"
        reasons.append(f"uncommitted user work: {shown}")
    return "; ".join(reasons)


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


class GitCloneError(RuntimeError):
    """``git clone`` of a session dir failed (#477).

    Still a ``RuntimeError`` whose text starts ``git clone failed:`` — what this
    used to raise — so existing callers are unaffected. It additionally carries
    the repo and git's own stderr, which is what the thread needs to be told:
    :func:`c_lord.workspace_failure.describe_workspace_failure` turns them into
    a message the user can act on.
    """

    def __init__(self, *, repo: str, stderr: str) -> None:
        self.repo = repo
        self.stderr = stderr
        super().__init__(f"git clone failed: {stderr.strip()}")


@dataclass(frozen=True)
class SessionDirInfo:
    """Snapshot of a single session directory."""

    path: str
    thread_id: int
    source_repo: str
    commit: str
    is_clean: bool
    #: why it is not clean — :func:`describe_changes` of the same snapshot (#1016)
    kept_reason: str = ""
    #: the scan of this dir failed; it is never removed on that scan's word
    failed: bool = False


@dataclass(frozen=True)
class CleanupResult:
    """Result of a single cleanup attempt."""

    path: str
    thread_id: int | None
    removed: bool
    reason: str
    #: kept because it holds work (#903) — *reason* says which (#1016).
    kept_work: bool = False


class SessionDirManager:
    """Manages clone-based session directories for Claude Code sessions.

    Each session gets a directory named ``{base_dir}/{thread_id}`` containing
    a git clone of ``source_repo``.

    Args:
        base_dir: Parent directory for all session dirs.
        source_repo: Git repository URL or local path to clone from.
        sessions_root: Where every workspace lives, all channels' (the
            ``session_dir_base`` the 30-day and orphan sweeps use). A remote
            under it vouches for nothing: it is another workspace, which a
            sweep may delete (#1246, #1265). Default: *base_dir*'s parent when
            *base_dir* is a channel dir (``<root>/<channel_id>``, as
            ``ChannelRepoCog`` builds it — review of f870f5b: a neighbour in
            another channel vouched, and both were deleted), else *base_dir*.
    """

    def __init__(
        self,
        base_dir: str,
        source_repo: str,
        sessions_root: str | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._source_repo = source_repo
        if sessions_root is None:
            base = Path(base_dir)
            sessions_root = str(base.parent) if base.name.isdigit() else base_dir
        self._sessions_root = sessions_root

    @property
    def base_dir(self) -> str:
        return self._base_dir

    @property
    def source_repo(self) -> str:
        return self._source_repo

    @property
    def sessions_root(self) -> str:
        """Where every workspace lives, across channels (#1265)."""
        return self._sessions_root

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

            result = _run(args, timeout=None)  # a big clone may take long
            if result.returncode != 0:
                logger.error(
                    "git clone failed for thread %d: %s",
                    thread_id,
                    result.stderr.strip(),
                )
                raise GitCloneError(repo=self._source_repo, stderr=result.stderr)

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

        # #882: a thread history exported before the fix (or by an older
        # c-lord) was never git-excluded. Catch it up here, every turn, so the
        # conversation cannot ride along on Claude's next ``git add -A``.
        if (Path(target) / HISTORY_FILENAME).is_file():
            exclude_thread_history(target)

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
            if not _THREAD_DIR_RE.match(entry.name):
                continue
            path = str(entry)
            thread_id = int(entry.name)
            try:
                # Inside the try (third review of ef8f0fc): a dir that cannot be
                # stat'ed is reported as unknown, not raised or skipped.
                if not entry.is_dir():
                    continue
                dot = _kind(entry / ".git")
                if dot is None:
                    raise PermissionError(f"cannot stat {entry / '.git'}")
                if dot == "missing":
                    continue
                commit = _get_commit(path)
                status = worktree_status(path, sessions_root=self._sessions_root)
            except Exception as exc:  # noqa: BLE001 — one dir never stops the scan
                logger.exception("Cannot inspect session dir %s", path)
                results.append(
                    SessionDirInfo(
                        path=path,
                        thread_id=thread_id,
                        source_repo=self._source_repo,
                        commit="",
                        is_clean=False,
                        kept_reason=f"status unknown (check failed: {type(exc).__name__})",
                        failed=True,
                    )
                )
                continue
            clean = status.clean

            results.append(
                SessionDirInfo(
                    path=path,
                    thread_id=thread_id,
                    source_repo=self._source_repo,
                    commit=commit,
                    is_clean=clean,
                    kept_reason="" if clean else describe_changes(status),
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

            if info.failed:
                results.append(
                    CleanupResult(
                        path=info.path,
                        thread_id=info.thread_id,
                        removed=False,
                        reason=info.kept_reason,
                        kept_work=True,
                    )
                )
                continue
            # Second review of 0e9b09c: one workspace that fails never stops the sweep.
            try:
                result = self._try_remove(info.path, info.thread_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Cleanup of session dir %s failed", info.path)
                result = CleanupResult(
                    path=info.path,
                    thread_id=info.thread_id,
                    removed=False,
                    reason=f"status unknown (check failed: {type(exc).__name__})",
                    kept_work=True,
                )
            results.append(result)

        return results

    def _try_remove(self, path: str, thread_id: int | None) -> CleanupResult:
        """Check cleanliness and remove the directory if safe."""
        status = worktree_status(path, sessions_root=self._sessions_root)
        if not status.clean:
            # #1016: say what the work is — an unpushed commit or a ``.env``
            # shows nothing in ``git status``, so "uncommitted changes" misled.
            # The same snapshot decides and explains: a second ``git status``
            # could disagree (an ``ls-remote`` that failed, then answered).
            why = describe_changes(status)
            logger.info("Keeping session dir %s (thread_id=%s) — %s (#1016)", path, thread_id, why)
            return CleanupResult(
                path=path, thread_id=thread_id, removed=False, reason=why, kept_work=True
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
                reason=_printable(f"removal failed: {exc}"),
            )
