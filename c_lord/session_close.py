"""Shared helpers for the 終了 (closed) session lifecycle — Issue #512.

``/close-workspace`` (#271) kills the tmux window but keeps the session dir,
transcript, and DB row so the conversation can be resumed later. Before #512 that
"later" was implicit and invisible:

* the thread name kept its ``W<N> │`` prefix, naming a tmux window that no longer
  existed — so a closed thread was indistinguishable from a live one in the
  sidebar, and
* the next message silently woke Claude up through the ``--continue``
  crash-recovery path (#270), announcing "前回のセッションが落ちていたので…" —
  wording aimed at a *crash*, which reads as nonsense to someone who closed the
  session on purpose.

#512 makes 終了 an explicit, persisted state (``sessions.closed_at``) with two
user-visible consequences, both implemented here so the close command
(``SessionManageCog``) and the message path (``ClaudeChatCog``) cannot drift:

1. :func:`apply_closed_name` / :func:`apply_open_name` — the ``[終了]`` marker on
   the Discord thread name.
2. :func:`closed_notice_embed` — the "this thread is closed, here is how to
   resume" notice shown instead of running the message.

Everything here is best-effort: a failed rename must never break the close, and a
failed notice must never swallow the user's message silently.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from .discord_ui.embeds import COLOR_INFO
from .thread_name import CLOSED_MARK, build_name, parse_topic_from_name, thread_lamp_enabled
from .utils.logger import log_ctx

if TYPE_CHECKING:
    from .database.repository import SessionRecord, SessionRepository

logger = logging.getLogger(__name__)

#: Timeout for the one-off rename/archive PATCH.
#:
#: Deliberately generous. Discord rate-limits thread renames to ~2 per 10 minutes
#: per thread and answers the third with a 429; discord.py handles that by
#: sleeping for the advertised ``retry_after`` and retrying. A short ``wait_for``
#: cancels that sleep, which loses the ``[終了]`` marker — and the marker is the
#: whole point of the close. Observed backoffs are under a minute, and the
#: command has already deferred its response, so waiting is the right trade.
_RENAME_TIMEOUT_SECONDS = 60.0

#: Title of the notice posted when someone writes into a closed thread.
CLOSED_NOTICE_TITLE = f"⏹️ このスレッドは終了しています ({CLOSED_MARK})"

_FALLBACK_TOPIC = "新しいスレッド"


def is_closed(record: SessionRecord | None) -> bool:
    """True when ``record`` marks a session the user closed on purpose (#512).

    The single definition of "終了", so the close command, the message path, and
    both naming paths cannot drift apart on what counts as closed.

    ``closed_at`` is a SQLite ``TEXT`` column: a timestamp string when closed,
    ``NULL`` otherwise. Anything that is not a non-empty string is treated as
    *not closed* — a deliberately fail-open reading, because the cost of getting
    this wrong is asymmetric. A false negative runs a message in a closed thread
    (mildly surprising); a false positive would hold every message in a healthy
    thread and look like the bot has stopped answering.
    """
    closed_at = getattr(record, "closed_at", None)
    return isinstance(closed_at, str) and bool(closed_at.strip())


def closed_notice_embed() -> discord.Embed:
    """The notice shown when a message lands in a closed thread (#512).

    States three things, in the order the user needs them: the message was *not*
    run (otherwise they would wait for a reply that never comes), the history is
    still there (so "終了" doesn't read as "deleted"), and exactly how to resume.
    """
    return discord.Embed(
        title=CLOSED_NOTICE_TITLE,
        description=(
            "送信されたメッセージは実行していません。\n\n"
            "会話の履歴とワークスペースは残っているので、いつでも再開できます。\n"
            "下の **▶️ 再開する** を押すと、いま送ったメッセージから続きを実行します"
            "（`/reopen-workspace` でも再開できます）。"
        ),
        color=COLOR_INFO,
    )


def _name_parts(
    record: SessionRecord | None, thread: discord.Thread
) -> tuple[str, str | None, str | None]:
    """Resolve ``(topic, issue_ref, origin_issue_ref)`` for rebuilding ``thread``'s name.

    Falls back to parsing the current Discord name when the row has no topic yet
    (e.g. a session closed before its first naming pass), so the rename never
    invents a placeholder over a name the user can already read.

    The origin (#593) rides along because stopping a workspace rebuilds the whole
    name: without it the ``[停止]`` rename would erase the number the thread was
    opened for, which is precisely when a stopped thread most needs to stay
    findable in the archived list.
    """
    topic = record.topic if record else None
    if not topic:
        name = thread.name if isinstance(thread.name, str) else ""
        topic = parse_topic_from_name(name)
    issue_ref = record.issue_ref if record else None
    origin_issue_ref = record.origin_issue_ref if record else None
    return topic or _FALLBACK_TOPIC, issue_ref, origin_issue_ref


async def _edit(thread: discord.Thread, *, archived: bool, name: str | None = None) -> bool:
    """Apply ``archived`` (and ``name`` when given) to ``thread``. True when it landed.

    Swallows every failure: a rename is cosmetic, and a 403 (no *Manage Threads*)
    or a rate-limit must not take down the close/reopen it decorates.

    ``name`` is passed as a separate keyword rather than splatted from a dict so
    the call keeps ``discord.Thread.edit``'s precise parameter types.
    """
    label = f"archived={archived}" + (f" name={name!r}" if name is not None else "")
    try:
        coro = (
            thread.edit(archived=archived)
            if name is None
            else thread.edit(name=name, archived=archived)
        )
        await asyncio.wait_for(coro, timeout=_RENAME_TIMEOUT_SECONDS)
    except (  # noqa: UP041 — asyncio.TimeoutError != builtins.TimeoutError on Python 3.10
        discord.HTTPException,
        TimeoutError,
        asyncio.TimeoutError,
    ) as exc:
        logger.warning(
            "%s close/reopen thread.edit(%s) failed: %s",
            log_ctx(thread_id=thread.id),
            label,
            exc,
        )
        return False
    logger.info("%s thread.edit(%s) applied (#512)", log_ctx(thread_id=thread.id), label)
    return True


async def _build_and_apply(
    repo: SessionRepository, thread: discord.Thread, *, closed: bool, archived: bool
) -> str:
    """Rebuild ``thread``'s name with/without ``[終了]`` and apply it with ``archived``.

    The name change and the archive flag go out as **one** ``PATCH``. Two separate
    edits would spend two of the thread's ~2-per-10-minutes rename allowance and
    could interleave badly — an archive landing first makes the following rename
    fail outright ("Thread is archived", code 50083).

    Never raises: the name is decoration, and the close (or reopen) it decorates
    must complete even if naming hits something unexpected. If the combined edit
    fails, the archive flag is retried on its own — losing the marker is a
    cosmetic regression, losing the archive/unarchive is a functional one.
    """
    try:
        record = await _safe_get(repo, thread.id)
        topic, issue_ref, origin_issue_ref = _name_parts(record, thread)
        new_name = build_name(
            topic,
            # A closed session has no live pane, and a reopened one has no window
            # until its next turn spawns one — "dead" is the honest state for both.
            "dead",
            None,
            lamp=thread_lamp_enabled(),
            issue_ref=issue_ref,
            origin_issue_ref=origin_issue_ref,
            closed=closed,
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "%s could not build the close/reopen name", log_ctx(thread_id=thread.id), exc_info=True
        )
        new_name = ""

    current = thread.name if isinstance(thread.name, str) else ""
    rename_to = new_name if (new_name and new_name != current) else None

    if not await _edit(thread, archived=archived, name=rename_to) and rename_to is not None:
        # The rename half is what failed (no Manage Threads, or a rename
        # rate-limit we could not wait out). Keep the archive flag, drop the name.
        await _edit(thread, archived=archived)
    return new_name


async def apply_closed_name(repo: SessionRepository, thread: discord.Thread) -> str:
    """Rename ``thread`` to ``[終了] …`` **and archive it**. Returns the aimed-for name.

    The ``W<N> │`` prefix is dropped because the tmux window it names is exactly
    what ``/close-workspace`` just killed — keeping it would point at a window
    that no longer exists. Archiving (#271, declutter the sidebar) rides along in
    the same PATCH; see :func:`_build_and_apply` for why they are not two calls.
    """
    return await _build_and_apply(repo, thread, closed=True, archived=True)


async def apply_open_name(repo: SessionRepository, thread: discord.Thread) -> str:
    """Rename ``thread`` back to its open form and un-archive it. Returns the name.

    Un-archiving is not cosmetic here: Discord refuses to edit an archived thread
    (code 50083), so the same PATCH has to clear the flag for the rename to be
    accepted at all — and a reopened session belongs back in the sidebar anyway.

    ``W<N>`` is *not* restored here: the window is recreated only when the next
    turn actually runs, and the regular naming pass
    (``ClaudeChatCog._apply_thread_naming``) puts the number back then.
    """
    return await _build_and_apply(repo, thread, closed=False, archived=False)


async def _safe_get(repo: SessionRepository, thread_id: int) -> SessionRecord | None:
    """``repo.get`` that never raises — naming must not break the caller."""
    try:
        return await repo.get(thread_id)
    except Exception:  # pragma: no cover - defensive
        logger.debug("session_close: session lookup failed for %d", thread_id, exc_info=True)
        return None


def reopen_rename_notice(old_name: str, new_name: str) -> str:
    """One line recording what the thread was called before it was reopened.

    A stopped thread keeps its window number (``[停止] W28 │ …``, #607), but the
    number is **not** restored on reopen: while it was stopped another workspace
    may well have taken 28, and pointing two threads at one window is the #427
    class of bug. So the reopen is exactly the moment the ``W28`` handle
    disappears from the name.

    Writing the old name into the thread keeps it findable afterwards — the whole
    reason the number was worth keeping in the first place.

    Returns ``""`` when there is nothing worth saying: the name did not change,
    or the rename did not go through.
    """
    if not old_name or not new_name or old_name == new_name:
        return ""
    return (
        f"🏷️ スレッド名: `{old_name}`\n"
        f"　　　　　　→ `{new_name}`\n"
        "（次に投稿すると、新しいウィンドウ番号が付きます）"
    )
