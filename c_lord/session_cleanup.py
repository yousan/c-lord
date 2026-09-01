"""What the 30-day sweep deleted, and what it left behind — #554.

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
from .session_dir import _is_clean
from .transcript.resolver import derive_project_dir, latest_session_jsonl

if TYPE_CHECKING:
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
    ABSENT = "absent"


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


def remove_clean_session_dir(record: SessionRecord) -> DirOutcome:
    """Delete *record*'s working directory when it is safe to. Never raises.

    Safe means: the path looks like a session directory, it exists, and ``git``
    reports no uncommitted or untracked changes. Anything else is kept.

    Losing a half-finished change to a background sweep nobody asked for is
    unrecoverable, so every uncertainty resolves to *keep* — including "this is
    not a git repository at all", where cleanliness cannot be established.
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

    if not _is_clean(str(path)):
        logger.info(
            "session cleanup: keeping %s — uncommitted work or not a git repo (thread=%s)",
            path,
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
