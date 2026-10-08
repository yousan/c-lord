"""Tell the owner — and only the owner — that someone was refused (#346).

A message from someone the allowlist does not cover used to be dropped at the
top of ``ClaudeChatCog.on_message`` with no reply, no reaction and **no log
line**. Neither side could tell: on 2026-09-26 a guest's request was dropped
that way twice, and the owner only found out by being asked why nothing ran.

What happens now:

* **The refused person sees nothing.** No reply, no reaction, no thread — not
  even the fact that a bot is reading the channel is given away.
* **The application owner gets a DM**: who, where, the first lines, a link to
  the message, and how to allow them. The owner is the same one #713 falls back
  to (:func:`~c_lord.discord_ui.authorization.read_application_owners`), so
  nothing has to be configured (Zero-Config).
* **One DM per person per :data:`DEFAULT_INTERVAL`.** Kept in process memory
  only (a :class:`~c_lord.log_sampler.LogSampler`): a restart starting the count
  over is fine, and a refusal is not worth a table.
* **Bots are logged, never DMed.** Webhooks never get here — owning the webhook
  URL is itself authorization (#507).
* **Every refusal is logged** with ``log_ctx``, DM or not.
* ``CLORD_NOTIFY_DENIED=0`` turns the DM off; the log stays.

Only a message this instance would otherwise have acted on counts as a refusal:
a normal message in a thread this instance owns (:func:`owns_channel`, #596).
The bot sees every message in the guild, and a channel post never runs a turn
for anybody — reporting those would DM the owner about ordinary chat, and every
c-lord instance sharing the guild would send the same DM. A text command
(``!...``) is left to the command, which has its own gate (#781).

The DM never names who *is* allowed — only the setting to change — so a
forwarded or leaked DM does not disclose the allowlist. See
``docs/specs/authorization-default.md``.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import discord

from .command_gate import owns_channel
from .discord_ui.authorization import get_fallback_owner_ids, read_application_owners
from .log_sampler import LogSampler
from .utils.logger import log_ctx

logger = logging.getLogger(__name__)

ENV_VAR = "CLORD_NOTIFY_DENIED"

#: One DM per refused person per day. Enough to learn "someone is knocking";
#: deliberate spam cannot bury the owner's DMs.
DEFAULT_INTERVAL = 24 * 60 * 60.0

#: How much of the refused message the DM quotes.
EXCERPT_LINES = 3
EXCERPT_CHARS = 300

#: Bot messages are only logged, but a chatty bot must not flood INFO either
#: (#678): once per thread per window, with the suppressed count on the next line.
BOT_LOG_WINDOW = 600.0

_FALSE_VALUES = ("0", "false", "no", "off")
_RUNNABLE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


def notify_enabled() -> bool:
    """Whether the owner DM is on (default) — ``CLORD_NOTIFY_DENIED=0`` turns it off."""
    return os.getenv(ENV_VAR, "").strip().lower() not in _FALSE_VALUES


def _excerpt(message: Any) -> str:
    content = (getattr(message, "content", "") or "").strip()
    if not content:
        n = len(getattr(message, "attachments", None) or [])
        return f"> （本文なし — 添付 {n} 件）" if n else "> （本文なし）"
    lines = content.splitlines()
    head = "\n".join(lines[:EXCERPT_LINES])
    cut = len(lines) > EXCERPT_LINES
    if len(head) > EXCERPT_CHARS:
        head = head[:EXCERPT_CHARS]
        cut = True
    quoted = "\n".join(f"> {line}" for line in head.splitlines())
    return quoted + ("\n> …" if cut else "")


def _where(channel: Any) -> str:
    guild = getattr(getattr(channel, "guild", None), "name", None) or "?"
    parent = getattr(channel, "parent", None)
    parent_name = getattr(parent, "name", None) if parent is not None else None
    name = getattr(channel, "name", None) or "?"
    parts = [guild] + ([f"#{parent_name}"] if parent_name else []) + [name]
    return " › ".join(parts)


def build_notice(message: Any) -> str:
    """The DM body: who, where, first lines, link, how to allow."""
    author = message.author
    # The name and the excerpt are the outsider's own text: no markdown from the
    # name, and the excerpt stays inside a quote so it cannot pass for c-lord's.
    display = discord.utils.escape_markdown(
        str(getattr(author, "display_name", None) or getattr(author, "name", "?"))
    )
    return "\n".join(
        [
            "🔒 **許可されていない人の発言を無視しました**"
            "（本人とチャンネルには何も表示していません）",
            f"発言者: <@{author.id}> （{display} / ID `{author.id}`）",
            f"場所: {_where(message.channel)}",
            _excerpt(message),
            f"メッセージ: {message.jump_url}",
            "許可するには: この人に `.env` の `CLORD_ALLOWED_ROLE` で指定したロールを付けてください"
            "（未設定なら `CLORD_ALLOWED_ROLE=<ロール名>` を書いて再起動）。",
            f"-# 同じ人についての通知は {int(DEFAULT_INTERVAL // 3600)} 時間に 1 回まで。"
            f"止めるには `{ENV_VAR}=0`。",
        ]
    )


class DeniedNotifier:
    """Logs every refused message and DMs the owner about refused people."""

    def __init__(
        self,
        *,
        interval: float = DEFAULT_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dm_throttle = LogSampler(interval, clock=clock)
        self._bot_log = LogSampler(BOT_LOG_WINDOW, clock=clock)
        self._owner_ids: set[int] | None = None

    async def report(self, bot: Any, message: Any) -> None:
        """Handle a message ``is_message_authorized`` refused. Never raises."""
        try:
            await self._report(bot, message)
        except Exception:
            logger.exception("denied-message report failed (#346) — message %s", message.id)

    async def _report(self, bot: Any, message: Any) -> None:
        author = message.author
        if bot.user is not None and author.id == bot.user.id:
            return
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            return
        if message.type not in _RUNNABLE_TYPES:
            return
        if not await owns_channel(bot, channel):
            return
        if (await bot.get_context(message)).valid:
            return

        ctx = log_ctx(thread_id=channel.id, channel_id=channel.parent_id or channel.id)
        if author.bot:
            sample = self._bot_log.sample(channel.id)
            if sample.emit:
                logger.info(
                    "%s ignored message %s from bot %s: not authorized "
                    "(not in CLORD_TRUSTED_BOT_IDS) — no DM for bots (#346)%s",
                    ctx,
                    message.id,
                    author.id,
                    sample.suffix,
                )
            return

        if not notify_enabled():
            outcome = f"owner DM off ({ENV_VAR}=0)"
        elif not self._dm_throttle.sample(author.id).emit:
            outcome = "owner already told about this user recently — no DM"
        else:
            outcome = await self._dm_owner(bot, message)
        logger.info(
            "%s ignored message %s from user %s (%s): not authorized — %s (#346)",
            ctx,
            message.id,
            author.id,
            getattr(author, "name", "?"),
            outcome,
        )

    async def _recipients(self, bot: Any) -> set[int]:
        resolved = get_fallback_owner_ids()
        if resolved:
            return resolved
        if self._owner_ids is None:
            # Not cached on failure: the next refusal asks again.
            owner_ids, _ = await read_application_owners(bot)
            self._owner_ids = owner_ids
            return owner_ids
        return self._owner_ids

    async def _dm_owner(self, bot: Any, message: Any) -> str:
        try:
            owner_ids = await self._recipients(bot)
        except Exception:
            logger.warning("could not read the application owner for the DM (#346)", exc_info=True)
            return "owner DM not sent (owner unknown)"
        body = build_notice(message)
        sent: list[int] = []
        for owner_id in sorted(owner_ids):
            try:
                user = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
                await user.send(
                    body,
                    allowed_mentions=discord.AllowedMentions.none(),
                    suppress_embeds=True,  # no previews of links the outsider wrote
                )
                sent.append(owner_id)
            except discord.HTTPException as exc:
                # Forbidden = the owner does not accept DMs. Say so and move on.
                logger.warning(
                    "could not DM owner %s about a refused message (%s: %s) (#346)",
                    owner_id,
                    type(exc).__name__,
                    exc,
                )
        if not sent:
            return "owner DM failed"
        return f"owner DM sent to {', '.join(str(i) for i in sent)}"
