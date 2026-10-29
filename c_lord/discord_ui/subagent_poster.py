"""Discord side of the sub-agent log (#971).

Status lines (start / live / end, with ``subagent-*.md``) are ordinary bot
posts. A sub-agent's *voice* — what it says to whom, and its report — goes
through a webhook so that each post carries the speaker's name, when the bot
may manage webhooks. When it may not, the bot posts the same text with the name
in front: the webhook is used if it is there, never required (README's
permissions are unchanged).

Every webhook used here is registered in :mod:`c_lord.voice_webhooks` *before*
the first post, because c-lord runs webhook messages in its threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from ..transcript.subagents import StatusFile
from ..utils.logger import log_ctx
from ..voice_webhooks import VOICE_WEBHOOK_NAME, register

logger = logging.getLogger(__name__)

#: How long "this channel will not give us a webhook" is believed before asking
#: again — a permission can be granted while the bot runs.
RETRY_SECONDS = 3600.0

_NO_MENTIONS = discord.AllowedMentions.none()
# Discord refuses a webhook username containing these words (HTTP 400).
_RESERVED_RE = re.compile(r"discord|clyde", re.IGNORECASE)

ResolveChannel = Callable[[int], Awaitable[Any]]
SendChunks = Callable[..., Awaitable[Any]]


class _WebhookCache:
    """One voice webhook per parent channel, shared by all of its threads."""

    def __init__(self) -> None:
        self._hooks: dict[int, tuple[discord.Webhook | None, float]] = {}
        self._lock = asyncio.Lock()

    def forget(self, channel_id: int) -> None:
        self._hooks.pop(channel_id, None)

    async def get(self, bot: Any, parent: Any) -> discord.Webhook | None:
        cached = self._hooks.get(parent.id)
        if cached is not None and (cached[0] is not None or time.monotonic() < cached[1]):
            return cached[0]
        async with self._lock:
            cached = self._hooks.get(parent.id)
            if cached is not None and (cached[0] is not None or time.monotonic() < cached[1]):
                return cached[0]
            hook = await self._acquire(bot, parent)
            self._hooks[parent.id] = (hook, time.monotonic() + RETRY_SECONDS)
            return hook

    @staticmethod
    async def _acquire(bot: Any, parent: Any) -> discord.Webhook | None:
        me = getattr(getattr(bot, "user", None), "id", None)
        try:
            hooks = await parent.webhooks()
            hook = next(
                (
                    h
                    for h in hooks
                    if h.name == VOICE_WEBHOOK_NAME
                    and h.token
                    and getattr(h.user, "id", None) == me
                ),
                None,
            )
            if hook is None:
                hook = await parent.create_webhook(
                    name=VOICE_WEBHOOK_NAME, reason="c-lord: sub-agent messages (#971)"
                )
        except (discord.Forbidden, discord.HTTPException, AttributeError) as exc:
            logger.info(
                "%s SubagentLog: no webhook here (%s) — names go in front of the text",
                log_ctx(channel_id=getattr(parent, "id", None)),
                type(exc).__name__,
            )
            return None
        # Before the first post: c-lord must never take what it says as an order.
        register(hook.id)
        return hook


_CACHE = _WebhookCache()


def _username(speaker: str) -> str:
    name = _RESERVED_RE.sub("…", speaker).strip()[:80]
    return name or "subagent"


class DiscordSubagentPoster:
    """:class:`~c_lord.transcript.subagents.SubagentPoster` for one thread."""

    def __init__(
        self,
        *,
        bot: Any,
        thread_id: int,
        resolve_channel: ResolveChannel,
        send_chunks: SendChunks,
        cache: _WebhookCache | None = None,
    ) -> None:
        self._bot = bot
        self._thread_id = thread_id
        self._resolve = resolve_channel
        self._send_chunks = send_chunks
        self._cache = cache if cache is not None else _CACHE

    async def post_status(self, text: str, file: StatusFile | None) -> object | None:
        channel = await self._resolve(self._thread_id)
        if channel is None:
            return None
        kwargs: dict[str, Any] = {
            "content": text,
            "silent": True,
            "allowed_mentions": _NO_MENTIONS,
            "suppress_embeds": True,
        }
        if file is not None:
            kwargs["file"] = discord.File(file.path, filename=file.filename)
        return await channel.send(**kwargs)

    async def restore_handle(self, message_id: int) -> object | None:
        """The status line *message_id* posted by an earlier process, to edit it (#1027)."""
        channel = await self._resolve(self._thread_id)
        get = getattr(channel, "get_partial_message", None) if channel is not None else None
        return get(message_id) if get is not None else None

    async def edit_status(self, handle: object, text: str, file: StatusFile | None) -> None:
        attachments = [] if file is None else [discord.File(file.path, filename=file.filename)]
        edit = getattr(handle, "edit", None)
        if edit is None:
            return
        with contextlib.suppress(discord.NotFound):  # someone deleted the line
            await edit(content=text, attachments=attachments, allowed_mentions=_NO_MENTIONS)

    async def post_voice(self, speaker: str, text: str) -> None:
        channel = await self._resolve(self._thread_id)
        if channel is None:
            return
        parent = getattr(channel, "parent", None)
        hook = await self._cache.get(self._bot, parent) if parent is not None else None
        if hook is not None:
            send = functools.partial(
                hook.send,
                username=_username(speaker),
                thread=channel,
                wait=True,
                allowed_mentions=_NO_MENTIONS,
            )
            try:
                await self._send_chunks(send, text, silent=True)
                return
            except discord.HTTPException as exc:
                # Deleted or revoked: ask again next time, say it this time.
                logger.warning(
                    "%s SubagentLog: webhook post failed (%s) — posting as the bot",
                    log_ctx(thread_id=self._thread_id),
                    getattr(exc, "status", "?"),
                )
                self._cache.forget(getattr(parent, "id", 0))
        send = functools.partial(channel.send, allowed_mentions=_NO_MENTIONS)
        await self._send_chunks(send, f"**{speaker}** {text}", silent=True)
