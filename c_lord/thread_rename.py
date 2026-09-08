"""``/thread-rename`` — re-summarise one thread's name, when asked (#705).

c-lord does **not** summarise thread names on its own any more (see
:func:`~c_lord.thread_name.topic_auto_enabled`).  The sidebar is where a user
recognises their own threads, so a name that changes without them asking costs
them the thread.  This module is the other half of that trade: when the name has
gone stale, the user asks for a new one and gets it.

What it does, in order:

1. reads the thread's recent conversation (:func:`collect_recent_text`),
2. asks **sonnet** for a ≤20-char Japanese topic
   (:func:`~c_lord.topic.summarize_thread`),
3. swaps *only* the topic body into the existing name
   (:func:`~c_lord.thread_name.replace_topic_in_name`), so the ``W<N> │``
   prefix, the ``#<origin> … →#<current>`` numbers and a ``[停止]`` marker all
   survive,
4. persists the topic so the 60s sidebar sync (#120) repaints the same name
   rather than undoing it a minute later.

Every failure path returns a line for the user instead of raising: a rename is
decoration, and a command must answer even when the model or Discord does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import discord

from . import topic as topic_module
from .thread_name import replace_topic_in_name
from .utils.logger import log_ctx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .database.repository import SessionRepository

logger = logging.getLogger(__name__)

#: How many messages back the summary looks.  Enough to see what a thread turned
#: into, few enough that one command is one cheap history read.
HISTORY_LIMIT = 40
#: Cap on the text handed to the model.  The newest messages are kept — a stale
#: name is usually stale because the *recent* work moved on.
MAX_CONVERSATION_CHARS = 2000
#: Seconds to wait on the Discord rename before giving up (matches the naming pass).
RENAME_TIMEOUT_SECONDS = 5.0

_MANAGE_THREADS_HINT = (
    "❌ スレッド名を変更できませんでした（bot に **Manage Threads** 権限がありません）。\n"
    "サーバー設定 → ロール で c-lord に「スレッドの管理」を付けてから、もう一度実行してください。"
)


def _safe(name: str) -> str:
    """Neutralise ``@everyone`` / ``@here`` / role pings before echoing a name.

    The topic is model output derived from the thread's own conversation, and a
    thread name is quoted back into a Discord **message** here. Discord parses
    mentions inside backticks too, so code-fencing is not enough: without this a
    conversation that steers the summary to ``@everyone`` would make c-lord ping
    the channel. Thread names themselves never ping, so only the echo needs it.
    """
    return discord.utils.escape_mentions(name or "")


async def collect_recent_text(
    thread: discord.Thread,
    *,
    limit: int = HISTORY_LIMIT,
    max_chars: int = MAX_CONVERSATION_CHARS,
) -> str:
    """Return the thread's recent conversation as ``author: text`` lines.

    Oldest-first within the window, newest-kept when the window is too long for
    ``max_chars``.  Empty messages (embed-only tool cards, attachments) and
    ``!command`` invocations are skipped: neither says anything about what the
    thread is *about*, and the ``!thread-rename`` that triggered this call would
    otherwise be the most recent thing the model reads.

    Returns ``""`` when the history cannot be read — the caller turns that into
    a message rather than a traceback.
    """
    lines: list[str] = []
    try:
        async for message in thread.history(limit=limit):
            text = (message.content or "").strip()
            if not text or text.startswith("!"):
                continue
            author = getattr(getattr(message, "author", None), "display_name", "?")
            lines.append(f"{author}: {text}")
    except (discord.HTTPException, discord.ClientException) as exc:
        logger.warning(
            "%s could not read thread history for rename: %s",
            log_ctx(thread_id=getattr(thread, "id", None)),
            exc,
        )
        return ""

    # ``history`` is newest-first; render oldest-first, but drop from the *old*
    # end when trimming so the newest turns always make it into the prompt.
    lines.reverse()
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[-max_chars:]
    return out


async def rename_thread_topic(
    thread: discord.Thread,
    repo: SessionRepository | Any | None,
) -> str:
    """Re-summarise ``thread``'s topic and apply it.  Returns the line to reply with.

    Runs regardless of ``auto_topic_locked`` (#95): that flag stops c-lord from
    renaming a thread **on its own**, and this is the user asking.  The new topic
    is persisted with source ``"command"`` so a later manual rename still wins
    and the sidebar sync rebuilds the same name.
    """
    ctx = log_ctx(thread_id=thread.id)
    conversation = await collect_recent_text(thread)
    if not conversation:
        return "❌ このスレッドの会話を読めませんでした。名前は変えていません。"

    new_topic = await topic_module.summarize_thread(conversation)
    if not new_topic:
        return (
            f"❌ 要約できませんでした（`{topic_module.RENAME_MODEL}` から使える答えが"
            "返りませんでした）。名前は変えていません。"
        )

    current = thread.name if isinstance(thread.name, str) else ""
    new_name = replace_topic_in_name(current, new_topic)
    if new_name == current:
        return f"ℹ️ 要約は同じでした（`{_safe(current)}`）。名前は変えていません。"

    try:
        await asyncio.wait_for(thread.edit(name=new_name), timeout=RENAME_TIMEOUT_SECONDS)
    except discord.Forbidden:
        logger.warning("%s /thread-rename forbidden (no Manage Threads)", ctx)
        return _MANAGE_THREADS_HINT
    except (  # noqa: UP041 — asyncio.TimeoutError != builtins.TimeoutError on Python 3.10
        discord.HTTPException,
        TimeoutError,
        asyncio.TimeoutError,
    ) as exc:
        logger.warning("%s /thread-rename edit failed: %r → %r: %s", ctx, current, new_name, exc)
        return (
            "❌ スレッド名の変更に失敗しました"
            f"（{type(exc).__name__}）。Discord のリネーム制限（10分に約2回）に"
            "かかっている場合は、少し待ってからもう一度実行してください。"
        )

    # Persist last: a stored topic the Discord name does not carry would be
    # repainted onto the thread by the 60s sync, turning a failed rename into a
    # delayed one the user never sees coming.
    if repo is not None:
        with contextlib.suppress(Exception):
            await repo.set_topic(thread.id, new_topic, source="command")

    logger.info("%s /thread-rename applied: %r → %r (#705)", ctx, current, new_name)
    return f"✅ スレッド名を変更しました: `{_safe(new_name)}`"
