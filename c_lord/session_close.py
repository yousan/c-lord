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

#: Timeout for the one-off rename. Matches the naming path in ClaudeChatCog —
#: long enough for a normal PATCH, short enough not to stall the close.
_RENAME_TIMEOUT_SECONDS = 5.0

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


def _name_parts(record: SessionRecord | None, thread: discord.Thread) -> tuple[str, str | None]:
    """Resolve ``(topic, issue_ref)`` for rebuilding ``thread``'s name.

    Falls back to parsing the current Discord name when the row has no topic yet
    (e.g. a session closed before its first naming pass), so the rename never
    invents a placeholder over a name the user can already read.
    """
    topic = record.topic if record else None
    if not topic:
        name = thread.name if isinstance(thread.name, str) else ""
        topic = parse_topic_from_name(name)
    issue_ref = record.issue_ref if record else None
    return topic or _FALLBACK_TOPIC, issue_ref


async def _rename(thread: discord.Thread, new_name: str) -> bool:
    """Rename ``thread``, swallowing every failure. True when the name changed.

    A rename is cosmetic; a 403 (no *Manage Threads*) or a rate-limit must not
    take down the close/reopen it decorates.
    """
    current = thread.name if isinstance(thread.name, str) else ""
    if current == new_name:
        return False
    try:
        await asyncio.wait_for(thread.edit(name=new_name), timeout=_RENAME_TIMEOUT_SECONDS)
    except (  # noqa: UP041 — asyncio.TimeoutError != builtins.TimeoutError on Python 3.10
        discord.HTTPException,
        TimeoutError,
        asyncio.TimeoutError,
    ) as exc:
        logger.warning(
            "%s close/reopen rename failed: %r → %r: %s",
            log_ctx(thread_id=thread.id),
            current,
            new_name,
            exc,
        )
        return False
    logger.info("%s renamed → %r (#512)", log_ctx(thread_id=thread.id), new_name)
    return True


async def _build_and_rename(
    repo: SessionRepository, thread: discord.Thread, *, closed: bool
) -> str:
    """Rebuild ``thread``'s name with/without the ``[終了]`` marker and apply it.

    Never raises: the name is decoration, and the close (or reopen) it decorates
    must complete even if naming hits something unexpected.
    """
    try:
        record = await _safe_get(repo, thread.id)
        topic, issue_ref = _name_parts(record, thread)
        new_name = build_name(
            topic,
            # A closed session has no live pane, and a reopened one has no window
            # until its next turn spawns one — "dead" is the honest state for both.
            "dead",
            None,
            lamp=thread_lamp_enabled(),
            issue_ref=issue_ref,
            closed=closed,
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "%s could not build the close/reopen name", log_ctx(thread_id=thread.id), exc_info=True
        )
        return ""
    await _rename(thread, new_name)
    return new_name


async def apply_closed_name(repo: SessionRepository, thread: discord.Thread) -> str:
    """Rename ``thread`` to its ``[終了] …`` form. Returns the name it aimed for.

    The ``W<N> │`` prefix is dropped because the tmux window it names is exactly
    what ``/close-workspace`` just killed — keeping it would point at a window
    that no longer exists.
    """
    return await _build_and_rename(repo, thread, closed=True)


async def apply_open_name(repo: SessionRepository, thread: discord.Thread) -> str:
    """Rename ``thread`` back to its open form (drops ``[終了]``). Returns the name.

    ``W<N>`` is *not* restored here: the window is recreated only when the next
    turn actually runs, and the regular naming pass
    (``ClaudeChatCog._apply_thread_naming``) puts the number back then.
    """
    return await _build_and_rename(repo, thread, closed=False)


async def _safe_get(repo: SessionRepository, thread_id: int) -> SessionRecord | None:
    """``repo.get`` that never raises — naming must not break the caller."""
    try:
        return await repo.get(thread_id)
    except Exception:  # pragma: no cover - defensive
        logger.debug("session_close: session lookup failed for %d", thread_id, exc_info=True)
        return None
