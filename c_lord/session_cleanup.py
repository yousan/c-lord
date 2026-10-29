"""What the 30-day sweep tidied, and what it left behind — #554.

Since #818 the sweep keeps the ``sessions`` row as a swept tombstone rather than
deleting it (see ``SWEPT_REASON`` in :mod:`c_lord.database.repository`). The
history below is why the notice exists; "deleted" there means "no longer a
live session".

c-lord deletes every ``sessions`` row that has gone 30 days unused, on every
startup, and until #554 it did so in total silence: one ``Cleaned up 3 old
sessions`` line, no thread ids, nothing in Discord. The row is the only thing
tying a Discord thread to its Claude session, so deleting it is what produces
「セッションが無い」 a month later — with no way for the person reading it to
find out why, or that anything was deleted at all:

    古い C-lord セッションを続けようとしたところセッションが無い、って言われ
    ちゃった。これってどうして無いんだろう？ 消した覚えは無いはず。Discord 上
    にそういう事も書いてないし

The 2026-08-26 decision (yousan) keeps the deletion and adds the notice. This
module is the notice: what survived, and the wording each combination earns.

**Why the notice is not one sentence.** Deleting the row does not delete the
work. ``W3 │ Qiita`` — the thread that prompted this — still has its git clone
on disk, half-written article and images included, dated the day it was last
touched; what it lost was the transcript, and with it Claude's memory of the
conversation. A notice saying 「作業内容は失われました」 would be false there, and
「そのまま続けられます」 false for a thread whose clone really is gone. So
:func:`inspect_survivors` looks at the disk and :func:`notice_for` says what it
found. Anything else is a wall of text people learn to skip.

**Two sweepers, not one.** Claude Code runs its own ``cleanupPeriodDays``
(default 30) over the same transcripts, independently of c-lord. So a thread can
lose its conversation history even where c-lord kept the row, and fixing c-lord
alone cannot bring transcripts back. The notice states that as fact rather than
promising a recovery this side cannot deliver.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .retention import claude_transcript_retention_days
from .session_dir import WorktreeStatus, worktree_status
from .tmux import release_window_number
from .transcript.resolver import derive_project_dir, latest_session_jsonl

if TYPE_CHECKING:
    from collections.abc import Collection

    from .database.repository import SessionRecord

logger = logging.getLogger(__name__)

__all__ = ["Survivors", "inspect_survivors", "notice_for"]


@dataclass(frozen=True)
class Survivors:
    """What is still on disk for a session whose row has just been deleted."""

    #: The git clone c-lord made for this thread — the actual work product.
    session_dir: bool
    #: Claude Code's own JSONL for it — the conversation, i.e. Claude's memory.
    transcript: bool


def inspect_survivors(record: SessionRecord, *, projects_root: Path | None = None) -> Survivors:
    """Look at the disk and report what outlived ``record``.

    Never raises. This runs during startup cleanup over rows nobody is watching;
    an odd ``working_dir`` (unreadable, embedded NUL, a path from another host)
    must degrade to "nothing found" rather than take the sweep — or the bot —
    down with it. Reporting less than survived is safe; the notice then simply
    understates, which is the harmless direction.
    """
    working_dir = record.working_dir
    if not working_dir:
        return Survivors(session_dir=False, transcript=False)
    try:
        session_dir = Path(working_dir).is_dir()
    except (OSError, ValueError):
        return Survivors(session_dir=False, transcript=False)
    try:
        project_dir = derive_project_dir(working_dir, projects_root=projects_root)
        transcript = latest_session_jsonl(project_dir) is not None
    except (OSError, ValueError):
        transcript = False
    return Survivors(session_dir=session_dir, transcript=transcript)


#: Opening line. Leads with the fact, because the reader arrived here confused
#: about why their session vanished — not looking for advice yet.
def _headline(days: int) -> str:
    """Opening line, including where the period came from (#575).

    The reader is being told their thread was cleaned up without asking. They
    are owed the reason the number is what it is — and that c-lord did not pick
    it: the period follows Claude Code's own transcript retention
    (``cleanupPeriodDays``), so the folder goes at the same moment the
    conversation Claude was keeping for it does. Saying so is also the only way
    someone can find the knob that changes it.
    """
    return (
        f"🧹 このスレッドは {days} 日以上使われていなかったため、"
        "**ワークスペースを整理しました。**\n"
        f"（{days} 日は Claude Code の会話ログ保持期間 `cleanupPeriodDays` に"
        "合わせています。c-lord 独自の期間ではありません）\n"
    )


#: Closing line. Every branch gets a next step: a notice that only reports a
#: loss leaves the reader exactly as stuck as the silence did.
_START_AGAIN = "・新しく始める → **チャンネルで** `/clord prompt:<やること>`"

#: Offered only when the checkout survived. #538 can reconnect the thread to a
#: session dir that is still on disk, so for those threads 「新しく始める」 alone
#: understates what is possible — and understating it is how work that is sitting
#: right there gets abandoned. Never offered when nothing survived: a reconnect
#: that cannot succeed is the false promise #538 exists to remove.
_RECONNECT = (
    "・**この作業の続きから再開する** → `/clord-reattach`"
    "（ディスクに残っている作業ディレクトリに繋ぎ直します）"
)


def notice_for(record: SessionRecord, survivors: Survivors, *, days: int = 30) -> str:
    """The message to post into ``record``'s thread, given what survived.

    Three shapes, because three things can be true (see the module docstring):

    * **clone + transcript** — everything but the link is intact.
    * **clone only** — the Qiita case: the work is there, Claude's memory is not.
      Said plainly, because "restored" would overpromise and "lost" would
      underpromise, and the reader can see the difference the moment they look.
    * **neither** — nothing to reconnect to; do not describe leftovers that are
      not there.

    The two checkout-surviving branches name ``/clord-reattach`` (#538 AC6): the
    row is gone but the work is not, and reconnecting keeps it. The third does not
    — with nothing on disk there is nothing to reattach to.
    """
    head = _headline(days)
    if survivors.session_dir and survivors.transcript:
        return (
            head + "・作業ディレクトリ（clone した内容）は**残っています**\n"
            "・会話の履歴も**残っています**\n\n"
            "続けるには:\n" + _RECONNECT + "\n" + _START_AGAIN
        )
    if survivors.session_dir:
        return (
            head + "・作業ディレクトリ（clone した内容）は**残っています**"
            " — 書きかけの成果物はディスク上にそのままあります\n"
            "・会話の履歴は**失われています**"
            "（Claude Code 自身も既定 30 日で transcript を整理するため）\n\n"
            "続けるには:\n" + _RECONNECT + "\n" + _START_AGAIN
        )
    return (
        head + "・このワークスペースに紐づくファイルは見つかりませんでした\n\n"
        "続けるには:\n" + _START_AGAIN
    )


# ── #575: the folder, not just the row ───────────────────────────────────────


class DirOutcome(Enum):
    """What the sweep did with a session directory."""

    REMOVED = "removed"
    KEPT_DIRTY = "kept_dirty"
    """Uncommitted work present, or cleanliness could not be established."""
    KEPT_UNSAFE = "kept_unsafe"
    """The path did not look like a session directory at all."""
    KEPT_FOREIGN = "kept_foreign"
    """Not this thread's own session dir — a fixed checkout, or still in use (#902)."""
    ABSENT = "absent"


#: How many changed paths a "keeping" log line names before it abbreviates.
_MAX_LOGGED_CHANGES = 5


def describe_changes(status: WorktreeStatus) -> str:
    """Why a directory is being kept, for the INFO line (#749, #903).

    Names the entries, because "uncommitted work" alone read as 80 dirs of user
    work when 60 of them held nothing but c-lord's own SKILL.md. Ignored files
    show as ``!! name``; commits on no remote branch as a count.
    """
    if not status.is_repo:
        return "not a git repo (cannot tell work from leftovers)"
    reasons: list[str] = []
    if status.unpushed_commits is None:
        reasons.append("unpushed commits unknown (git rev-list failed)")
    elif status.unpushed_commits:
        reason = f"{status.unpushed_commits} unpushed commit(s) on no remote branch"
        if status.remote_unchecked:
            reason += " (ls-remote failed; pushed branches could not be checked)"
        reasons.append(reason)
    if status.user_changes:
        shown = ", ".join(status.user_changes[:_MAX_LOGGED_CHANGES])
        more = len(status.user_changes) - _MAX_LOGGED_CHANGES
        if more > 0:
            shown += f" (+{more} more)"
        reasons.append(f"uncommitted user work: {shown}")
    return "; ".join(reasons)


#: A session dir is always ``<base>/<channel_id>/<thread_id>`` — at least three
#: path components deep. A ``working_dir`` shallower than this is a corrupt row,
#: not a workspace, and deleting it could take out a home directory.
_MIN_PATH_DEPTH = 3


def sweep_days() -> int:
    """How long a workspace survives unused, in days.

    Mirrors Claude Code's transcript retention rather than being a number c-lord
    picked: once Claude has forgotten the conversation, the checkout that existed
    to serve it has nothing left to serve. Keeping the two in step also removes
    the window that produced the 118 GB — rows expiring while directories did
    not. See :mod:`c_lord.retention` for why c-lord reads that setting and never
    writes it.
    """
    return claude_transcript_retention_days()


def _resolved(path: str | Path) -> Path | None:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def foreign_reason(
    record: SessionRecord,
    path: Path,
    *,
    sessions_root: str | Path | None,
    claimed: Collection[str] = (),
) -> str | None:
    """Why *path* is not the swept thread's to delete, or ``None`` if it is (#902).

    c-lord clones exactly one directory per thread, ``<root>/<channel>/<thread>``
    (:class:`c_lord.session_dir.SessionDirManager`). Anything else a row points
    at — a scheduled task's fixed checkout (#687), another thread's clone — was
    made by someone else and may be in use this week: the 30-day sweep deleted
    ``/home/yousan/c-lord-audit`` and its hand-written ``AUDIT.md`` that way.
    Paths are compared resolved, so a relative root (``data/sessions``) and a
    symlinked one agree with what the rows recorded.
    """
    resolved = _resolved(path)
    root = _resolved(sessions_root) if sessions_root else None
    if resolved is None or root is None:
        return "not a session dir (session dir root unknown)"
    if (
        resolved.name != str(record.thread_id)
        or resolved.parent.parent != root
        or not resolved.parent.name.isdigit()
    ):
        return "not a session dir (fixed checkout)"
    if any(_resolved(other) == resolved for other in claimed if other):
        return "still used by another session"
    return None


def remove_clean_session_dir(
    record: SessionRecord,
    *,
    sessions_root: str | Path | None,
    claimed: Collection[str] = (),
) -> DirOutcome:
    """Delete *record*'s working directory when it is safe to. Never raises.

    Safe means: the path is this thread's own session dir under *sessions_root*
    and no live row in *claimed* still points at it (#902 — see
    :func:`foreign_reason`), it exists, and ``git`` reports no uncommitted or
    untracked changes other than the files c-lord wrote there itself (#749 — see
    :func:`c_lord.session_dir.worktree_status`). Anything else is kept.

    Losing a half-finished change to a background sweep nobody asked for is
    unrecoverable, so every uncertainty resolves to *keep* — including "this is
    not a git repository at all", where cleanliness cannot be established, and
    "where session dirs live is unknown" (``sessions_root=None``).
    """
    working_dir = (record.working_dir or "").strip()
    if not working_dir:
        return DirOutcome.ABSENT

    try:
        path = Path(working_dir)
        if len([p for p in path.parts if p not in ("/", "")]) < _MIN_PATH_DEPTH:
            logger.warning(
                "session cleanup: refusing to remove suspiciously shallow path %r (thread=%s)",
                working_dir,
                record.thread_id,
            )
            return DirOutcome.KEPT_UNSAFE
        if not path.is_dir():
            return DirOutcome.ABSENT
    except (OSError, ValueError):
        return DirOutcome.ABSENT

    reason = foreign_reason(record, path, sessions_root=sessions_root, claimed=claimed)
    if reason is not None:
        logger.info("session cleanup: keeping %s — %s (thread=%s)", path, reason, record.thread_id)
        return DirOutcome.KEPT_FOREIGN

    status = worktree_status(str(path))
    if not status.clean:
        logger.info(
            "session cleanup: keeping %s — %s (thread=%s)",
            path,
            describe_changes(status),
            record.thread_id,
        )
        return DirOutcome.KEPT_DIRTY

    try:
        shutil.rmtree(path)
    except OSError as exc:
        logger.warning("session cleanup: failed to remove %s: %s", path, exc)
        return DirOutcome.KEPT_DIRTY
    logger.info("session cleanup: removed %s (thread=%s)", path, record.thread_id)
    return DirOutcome.REMOVED


def retire_swept_workspace(
    record: SessionRecord,
    *,
    sessions_root: str | Path | None,
    claimed: Collection[str] = (),
) -> DirOutcome:
    """Finish the sweep of one swept row: its checkout, then its ``w{N}``. Never raises.

    #595: the 30-day sweep is the automatic delete, and deletion is when a
    thread's window number is freed. A checkout kept for uncommitted work means
    the workspace was not deleted, so the thread keeps its number. A directory
    that was never the thread's own (#902) is left alone, but the thread itself
    is done with, so its number is freed.
    """
    outcome = remove_clean_session_dir(record, sessions_root=sessions_root, claimed=claimed)
    if outcome in (DirOutcome.REMOVED, DirOutcome.ABSENT, DirOutcome.KEPT_FOREIGN):
        try:
            release_window_number(record.thread_id)
        except Exception:  # never let a number cost the sweep
            logger.exception(
                "session cleanup: releasing window number failed (thread=%s)", record.thread_id
            )
    return outcome
