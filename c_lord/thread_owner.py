"""Is this thread another c-lord's? — #811.

Every c-lord registers the same slash-command names, so a guild with two of them
(say the production bot and a per-user one) shows each command once **per bot**.
Pick the other bot's entry in a thread and the bot that answers is one that has
never seen the thread. It looks in its own ``sessions`` table, finds nothing, and
used to say so as if the thread were broken: 「ワークスペースがありません」,
「メッセージを送っても復元できません（c-lord の記録が見つかりません）」 — about
a thread its owner was working in at that very moment (2026-09-25, twice
``/workspace-start`` and once ``/tmux-screenshot``).

The non-owning bot cannot know what the owner's session looks like, but it does
know who the owner is: Discord keeps ``thread.owner_id`` forever. So when that is
**another bot** and this bot has no row, the true answer is "not mine — ask
<@owner>", and nothing about the workspace should be claimed at all.

Deliberately narrow:

* **a row wins.** If this bot has a ``sessions`` row, the thread is this bot's,
  whoever created it.
* **a human owner is not "another c-lord".** A hand-made thread (#556's Grafana
  thread) keeps the #538 / #551 wording; there is nobody else to point at.
* **never raises, and doubt means "not foreign".** Every caller already has an
  answer for a thread with no row; this only replaces it when the evidence is
  positive, so a DB or REST hiccup falls back to exactly the old behaviour.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from .database.repository import SessionRepository

logger = logging.getLogger(__name__)

__all__ = ["foreign_owner_id", "foreign_owner_notice", "foreign_owner_notice_for"]


async def _is_bot_user(bot: Any, user_id: int) -> bool:
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            return False
    return getattr(user, "bot", False) is True


async def foreign_owner_id(bot: Any, repo: SessionRepository, channel: object) -> int | None:
    """The owning bot's user id when ``channel`` is another bot's thread, else ``None``.

    "Another bot's thread" = a :class:`discord.Thread` whose ``owner_id`` is a bot
    other than ``bot.user``, and for which ``repo`` holds no ``sessions`` row.
    """
    if not isinstance(channel, discord.Thread):
        return None
    owner_id = getattr(channel, "owner_id", None)
    me = getattr(getattr(bot, "user", None), "id", None)
    if not isinstance(owner_id, int) or not isinstance(me, int) or owner_id == me:
        return None
    try:
        if await repo.get(channel.id) is not None:
            return None
        if not await _is_bot_user(bot, owner_id):
            return None
    except Exception:
        logger.warning("thread %s: owner check failed — treating as ours", channel.id)
        return None
    return owner_id


def foreign_owner_notice(owner_id: int) -> str:
    """What a non-owning bot says instead of claiming the workspace is gone."""
    return (
        f"ℹ️ このスレッドは <@{owner_id}> の担当です（このボットの担当ではありません）。\n"
        f"同じ名前のコマンドが候補に複数並んでいたら、<@{owner_id}> の方を選び直してください。"
        "このスレッドの作業には影響ありません。"
    )


async def foreign_owner_notice_for(
    bot: Any, repo: SessionRepository, channel: object
) -> str | None:
    """:func:`foreign_owner_notice` when ``channel`` is another bot's thread, else ``None``."""
    owner_id = await foreign_owner_id(bot, repo, channel)
    if owner_id is None:
        return None
    logger.info(
        "thread %s belongs to bot %s — answering with its name (#811)",
        getattr(channel, "id", None),
        owner_id,
    )
    return foreign_owner_notice(owner_id)
