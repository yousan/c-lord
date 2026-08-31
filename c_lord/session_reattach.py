"""Reattach a Discord thread to the Claude session it already has — #538 AC6–AC8.

Until now c-lord could tell you a thread was unrecoverable but could not recover
one. The ``sessions`` row is the only link between a Discord thread and its
Claude session, #554 deletes it after 30 days of disuse, and nothing put it back:
``/resync`` reconnects the mirror to a tmux pane, ``/reopen-workspace`` clears a
``/close-workspace``, and neither writes a row. The repair was editing SQLite by
hand.

What makes recovery worth building is that losing the row destroys almost
nothing. ``W3 │ Qiita`` was measured after its row went: the git clone was intact
with the half-written article and its images, the Discord thread still held 206
messages / 34,206 characters, and the user's own prompts were in
``~/.claude/history.jsonl``. **Only Claude's memory was gone.**

So recovery is graded by what actually survived rather than offered as one button
that either works or lies — see :class:`Recovery`. The grading matters because
the two middle outcomes feel identical from Discord and are not: one resumes the
conversation, the other resumes only the work.

**AC7 — this reattaches, it never adopts.** :func:`plan_recovery` returns
:attr:`Recovery.NONE` unless a session dir for this thread is already on disk. It
does not clone, does not create directories, and has no path that turns a thread
c-lord never touched into a session. Without that, "recovery" would simply be
#551's takeover wearing a friendlier label — which is why a transcript with no
checkout is also NONE: writing a row pointing at a directory that does not exist
is creating a session, not restoring one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .transcript.resolver import derive_project_dir, latest_session_jsonl

logger = logging.getLogger(__name__)

__all__ = [
    "HISTORY_FILENAME",
    "Plan",
    "Recovery",
    "plan_recovery",
    "reattach_notice",
    "recoverable_notice",
    "render_history",
]


class Recovery(Enum):
    """How much of a thread can be brought back, given what is still on disk."""

    #: Transcript *and* checkout survived — ``--continue`` (#270) resumes the
    #: real conversation. Nothing is lost.
    FULL = "full"
    #: The checkout survived, the transcript did not (the Qiita case, and the
    #: common one: Claude Code expires transcripts on its own 30-day default).
    #: The work continues; the conversation is rebuilt from the Discord thread.
    WORKDIR = "workdir"
    #: Nothing to reattach to.
    NONE = "none"


@dataclass(frozen=True)
class Plan:
    """What recovering this thread would do."""

    kind: Recovery
    #: The existing checkout to reattach to — never a path to be created.
    working_dir: str | None = None
    #: Claude's own session id, read from the transcript filename. Only the
    #: transcript still holds it once the row is gone, and it is what
    #: ``--resume`` needs.
    session_id: str | None = None


def plan_recovery(
    *,
    session_dir_base: str | None,
    thread_id: int,
    projects_root: Path | None = None,
) -> Plan:
    """Grade what can be recovered for ``thread_id``.

    ``session_dir_base`` is a :class:`~c_lord.session_dir.SessionDirManager`'s
    base (it already includes the channel id); ``None`` means no binding resolves
    for the channel, which is not an error — it is simply no evidence, and
    therefore no recovery.

    Never raises: this runs behind a button someone clicked on a notice, and an
    odd path must degrade to "cannot recover" rather than a traceback.
    """
    if not session_dir_base:
        return Plan(Recovery.NONE)
    try:
        working = Path(session_dir_base) / str(thread_id)
        if not working.is_dir():
            # No checkout ⇒ nothing to reattach to, whatever else is around.
            return Plan(Recovery.NONE)
    except (OSError, ValueError):
        logger.debug("plan_recovery: unusable base %r", session_dir_base)
        return Plan(Recovery.NONE)

    working_dir = str(working)
    try:
        jsonl = latest_session_jsonl(derive_project_dir(working_dir, projects_root=projects_root))
    except (OSError, ValueError):
        jsonl = None
    if jsonl is not None:
        return Plan(Recovery.FULL, working_dir=working_dir, session_id=jsonl.stem)
    return Plan(Recovery.WORKDIR, working_dir=working_dir)


#: Where the Discord thread is written for a WORKDIR recovery. Inside the
#: checkout (so Claude can simply read it) and under ``.claude/`` so it does not
#: land in the user's git status as an untracked file at the repo root.
HISTORY_FILENAME = ".claude/clord-thread-history.md"

_START_AGAIN = "・新しく始める → **チャンネルで** `/clord prompt:<やること>`"

_NOTICES = {
    Recovery.FULL: (
        "🔗 **このスレッドのワークスペースに再接続しました。**\n"
        "・作業ディレクトリも**会話の履歴も残っていました**\n"
        "・このままメッセージを送れば、**前の会話の続きから**再開します。"
    ),
    Recovery.WORKDIR: (
        "🔗 **このスレッドの作業ディレクトリに再接続しました。**\n"
        "・書きかけの成果物は**そのまま残っています**\n"
        "・**会話の履歴は失われていました**（Claude Code 側の transcript 整理による）ので、"
        f"このスレッドの過去ログを `{HISTORY_FILENAME}` に書き出しました。"
        "次のメッセージで Claude がそれを読み、経緯を引き継ぎます。"
    ),
    Recovery.NONE: (
        "⚠️ **このスレッドには再接続できるものが残っていませんでした。**\n"
        "・作業ディレクトリが見つかりません（削除済みか、別のホストで動いていたセッションです）\n\n"
        "続けるには:\n" + _START_AGAIN
    ),
}


_RECOVERABLE = {
    Recovery.FULL: (
        "⚠️ このスレッドの c-lord 側の記録が見つかりません"
        "（30 日以上使われていないと自動で整理されます）。\n"
        "**いま送ったメッセージは Claude に届いていません。**\n\n"
        "ただし、**作業ディレクトリも会話の履歴もディスクに残っています**。\n"
        "下のボタンで**再接続**すれば、前の会話の続きから再開できます。"
    ),
    Recovery.WORKDIR: (
        "⚠️ このスレッドの c-lord 側の記録が見つかりません"
        "（30 日以上使われていないと自動で整理されます）。\n"
        "**いま送ったメッセージは Claude に届いていません。**\n\n"
        "ただし、**作業ディレクトリは残っています**（書きかけの成果物もそのままです）。\n"
        "会話の履歴は失われていますが、下のボタンで**再接続**すれば、"
        "このスレッドの過去ログを引き継いで続きから作業できます。"
    ),
}


def recoverable_notice(plan: Plan) -> str:
    """The notice shown when a swept thread *can* be reconnected — #538 AC6.

    Leads with the same fact the old notice did — the message did not reach
    Claude — because that is still what the reader needs first. What follows is
    the part that was missing: the work is not gone, and here is the way back.
    Never called for :attr:`Recovery.NONE`; that case keeps the original wording,
    which is the honest one when nothing survived.
    """
    return _RECOVERABLE[plan.kind]


def reattach_notice(plan: Plan) -> str:
    """What to tell the user after acting on ``plan``.

    Three wordings, because the three outcomes differ in exactly the way the
    reader cares about. Saying 「復元しました」 for a WORKDIR recovery would be
    #538 again — a promise the next message cannot keep.
    """
    return _NOTICES[plan.kind]


def render_history(messages: list[tuple[str, str, str]]) -> str:
    """Render ``(author, timestamp, text)`` triples as the hand-off document.

    For a WORKDIR recovery the transcript is gone but the Discord thread is not —
    206 messages of it, in the measured case. Claude reads this instead of
    remembering, which is why it opens by saying what it is: dropped in cold, an
    undated wall of dialogue is just noise, and Claude would have no reason to
    treat it as the history of the repo it is standing in.

    Written to a file rather than pushed into the prompt because the real ones are
    long — 34,206 characters for ``W3 │ Qiita`` — and a prompt that size would
    crowd out the actual instruction.
    """
    head = (
        "# このスレッドのこれまでの経緯（c-lord による書き出し）\n\n"
        "このセッションは中断され、Claude 側の会話履歴（transcript）は失われました。\n"
        "作業ディレクトリはそのまま残っています。以下は同じ作業を行っていた Discord\n"
        "スレッドの過去ログです。**続きを頼まれたら、まずこれを読んで経緯を把握して\n"
        "ください。**\n"
    )
    if not messages:
        return head + "\n（過去ログを取得できませんでした）\n"
    body = "\n".join(
        f"### {author} — {timestamp}\n\n{text}\n" for author, timestamp, text in messages
    )
    return head + "\n---\n\n" + body
