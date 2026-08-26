"""Is this thread resumable? One answer, shared by the hint and the message path — #538.

c-lord tells the owner of a stopped session **「このスレッドにメッセージを送れば
自動で復元し、続きから再開します」** (``/tmux-screenshot``, ``/resync``). The
receiving side — :meth:`ClaudeChatCog.on_message` — accepted a thread message only
when a ``sessions`` row existed for it, and dropped it otherwise with no reply and
no log line. The two sides never checked the same thing, so a thread whose row was
missing swallowed every message while the bot kept promising it would resume: the
user followed the instructions and got silence (#538).

This module is the single place that answers "what will a plain message in this
thread do?", in the same spirit as :mod:`c_lord.session_close`:

* :func:`classify` — the verdict, from the ``sessions`` row alone.
* :func:`accepts_message` — what ``on_message`` gates on.
* :func:`stopped_hint` / :func:`hint_for_thread` — the wording each verdict earns.

The promise and the acceptance rule are therefore derived from one function; they
cannot drift apart again without deleting this module.

Resumability is deliberately defined as *the row exists* — the exact condition the
message path acts on — not as "a transcript is somewhere on disk". Reconstructing a
lost row from disk is a different feature; promising it here would recreate the very
gap this module closes.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from .session_close import is_closed

if TYPE_CHECKING:
    from .database.repository import SessionRecord, SessionRepository

__all__ = [
    "NOT_A_CLORD_THREAD",
    "NOT_A_CLORD_THREAD_BINDING",
    "UNTRACKED_NOTICE",
    "UNTRACKED_REACTION",
    "ThreadResume",
    "accepts_message",
    "classify",
    "hint_for_thread",
    "is_clord_thread",
    "stopped_hint",
]


class ThreadResume(Enum):
    """What a plain (non-command) message in this thread will actually do."""

    #: A row exists and the session is open — the message runs, and a dead tmux
    #: pane is auto-restored from the on-disk transcript first (``--continue``, #270).
    RESUMES = "resumes"
    #: A row exists but the user closed the session (#512) — the message is held
    #: and a 「▶️ 再開する」 notice is posted instead.
    CLOSED = "closed"
    #: No row — nothing runs. Before #538 this was also completely silent.
    UNTRACKED = "untracked"


def classify(record: SessionRecord | None) -> ThreadResume:
    """The verdict for ``record`` (``None`` = no ``sessions`` row for the thread)."""
    if record is None:
        return ThreadResume.UNTRACKED
    return ThreadResume.CLOSED if is_closed(record) else ThreadResume.RESUMES


def is_clord_thread(verdict: ThreadResume) -> bool:
    """True when this thread currently *has* a session — #551 branch 1.

    Note this is the same rule as :func:`accepts_message`, deliberately: what
    ``/clord`` will continue and what a plain message will continue have to be the
    same set of threads, or one is a way around the other.

    It is **not** the whole of "is this ours" — a thread whose row the 30-day
    sweep took is still c-lord's, and :mod:`c_lord.thread_origin` is what answers
    that. Keeping the two separate is the point: one asks "is there a session to
    continue", the other "was there ever one".
    """
    return verdict is not ThreadResume.UNTRACKED


def accepts_message(verdict: ThreadResume) -> bool:
    """True when ``on_message`` acts on a plain message with this verdict.

    ``CLOSED`` counts as accepted: the message is not run, but it is *answered*
    (held, with a reopen button) rather than dropped — which is the distinction
    that matters to the person who sent it.
    """
    return is_clord_thread(verdict)


#: What to do when a thread has no session record. Shared by the notice posted to
#: the thread and by the hint the slash commands show, so both name the same way out.
#:
#: #551 AC9 rewrote this. #545 said 「このスレッドで新しく始める → `/clord`」, which
#: was true when written and is a refusal now — #551 stops ``/clord`` from turning a
#: thread into a session. Leaving it would have c-lord instructing people to run the
#: command it rejects, which is #538's failure (a promise and a check that disagree)
#: reappearing one layer up. c-lord threads are only ever born in a channel, so that
#: is where it points.
_NEXT_STEPS = (
    "続けるには:\n"
    "・新しく始める → **チャンネルで** `/clord prompt:<やること>`"
    "（新しいスレッドが立ちます。前の会話は引き継ぎません）\n"
    "・別のリポジトリで始める → **チャンネルで** `/clord repo:<URL> prompt:<やること>`"
)

#: Posted to the thread when a message arrives with no session record (#538).
#: Leads with the fact the sender needs first — their message did **not** run —
#: because the failure they hit was waiting for a reply that was never coming.
UNTRACKED_NOTICE = (
    "⚠️ このスレッドには復元できるセッションがありません（c-lord の記録が見つかりません）。\n"
    "**いま送ったメッセージは Claude に届いていません。**\n\n" + _NEXT_STEPS
)

#: Added to every message dropped this way. The notice is posted once per thread
#: per process (it is a wall of text); the reaction is what keeps the 2nd, 3rd, …
#: message from looking silently ignored again.
UNTRACKED_REACTION = "⚠️"

#: ``/clord`` (and ``!clord``) refusing a thread that was never c-lord's — #551.
#: Before this the command took the thread over instead: it cloned a session dir,
#: opened a tmux window and wrote the ``sessions`` row, after which every message
#: in what had been a human conversation went to Claude.
#:
#: Reached only when :mod:`c_lord.thread_origin` finds no trace of c-lord at all.
#: A thread that merely lost its row to the 30-day sweep is offered a reconnect
#: (#538) instead — refusing those would strand them, which is what the first cut
#: of #551 got wrong.
NOT_A_CLORD_THREAD = (
    "⚠️ このスレッドは c-lord のスレッドではないため、ここでセッションを開始できません。\n"
    + _NEXT_STEPS
)

#: ``/clord-thread-init`` refusing the same thread — #551 AC2. Binding a repo to a
#: human thread was step one of the same takeover, so it is refused on the same
#: test; the middle line says what the command *is* for, so the reader is not left
#: thinking it is broken.
NOT_A_CLORD_THREAD_BINDING = (
    "⚠️ このスレッドは c-lord のスレッドではないため、リポジトリを紐づけられません。\n"
    "`/clord-thread-init` は、**すでにある c-lord スレッド**のリポジトリを変えるための"
    "コマンドです。\n" + _NEXT_STEPS
)

_HINTS = {
    ThreadResume.RESUMES: (
        "ℹ️ この作業セッションは現在停止しています（tmux ウィンドウがありません）。\n"
        "**このスレッドにメッセージを送れば自動で復元し、続きから再開します。**"
    ),
    ThreadResume.CLOSED: (
        "ℹ️ このスレッドは終了しています（`[終了]`。tmux ウィンドウもありません）。\n"
        "**メッセージを送ると「▶️ 再開する」ボタンが出ます**"
        "（`/reopen-workspace` でも再開できます）。"
    ),
    ThreadResume.UNTRACKED: (
        "ℹ️ この作業セッションは停止していて、**メッセージを送っても復元できません**"
        "（c-lord の記録が見つかりません）。\n" + _NEXT_STEPS
    ),
}


def stopped_hint(verdict: ThreadResume) -> str:
    """The 「セッションが止まっています」 wording that ``verdict`` earns.

    Used by the commands that hit a missing tmux window (``/tmux-screenshot``,
    ``/resync``). They used to dead-end with a bare "No tmux window found for this
    thread.", which left the user with no next step during the 2026-06-25
    tmux-server-death incident; #464 ②-2 replaced that with the recovery path — a
    plain message auto-resumes the on-disk session via ``--continue`` (#270) and
    announces it (#465). What #464 did not do was *check* that the thread could
    actually be resumed, which is #538. This function supplies the sentence that
    matches what a message would really do.
    """
    return _HINTS[verdict]


async def hint_for_thread(repo: SessionRepository, thread_id: int) -> str:
    """:func:`stopped_hint` for ``thread_id``, looked up through ``repo``.

    Never raises: the hint decorates a command that has already done its work, so
    a DB hiccup must not turn into a failed command. An unreadable row falls back
    to the resumable wording — the pre-#538 behaviour, and the one that sends the
    user to the path most likely to work (a message that *does* have a row simply
    runs).
    """
    try:
        record = await repo.get(thread_id)
    except Exception:
        return _HINTS[ThreadResume.RESUMES]
    return stopped_hint(classify(record))
