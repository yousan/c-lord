"""One gate for message-backed (text / webhook) command invocations.

``ClaudeDiscordBot.process_commands`` deliberately lets webhook messages reach
the text commands (#209) — that is what makes CI/CD and E2E automation
possible.  What it never established is who, on the other side, is supposed to
*act* on them.  Two independent questions were left to each command to answer
on its own, and both went wrong:

* **担当** — *which instance* answers.  A guild can hold several c-lord bots,
  and a text command is delivered to **every one that can read the channel**.
  Almost no command asked the question at all, so on 2026-08-27 a single
  ``!workspace-stop`` in a production thread was executed by three staging bots
  as well as by production (#596; the narrow channel-level guard added in #522
  only covered one branch of ``!clord``).
* **認可** — *who* may drive it.  Commands that gate on the human allowlist
  reject the webhook pseudo-user by construction, so configuring
  ``DISCORD_OWNER_ID`` silently broke webhook automation (#507, then #508 for
  ``!skill`` / ``!clord-init`` / ``!clord-thread-init``).

Both come from the same root — no shared gate for message-backed invocations —
so the rules live here, once, and are applied at one place each:
:func:`owns_channel` from ``process_commands`` (covering every text command
that exists or will exist, #596 AC3), :func:`is_message_authorized` from the
command implementations that need to tell a human apart from infrastructure.

Ownership is deliberately a *positive claim*: an instance answers only where it
can point at a record tying it to the channel or thread.  The consequence worth
knowing is that a channel which is neither ``DISCORD_CHANNEL_ID`` nor bound
belongs to nobody, so ``!clord-init`` can no longer bootstrap one — use the
slash command, which Discord routes to a single application and which therefore
has no ambiguity to resolve.  See ``docs/specs/command-ownership.md``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import discord

logger = logging.getLogger(__name__)

# A binding lookup: ``(channel_id, thread_id=...) -> manager | None``.
_Resolver = Callable[..., Awaitable[Any]]


def _trusted_bot_ids() -> set[int]:
    """Bot ids allowed to drive c-lord like a human (``CLORD_TRUSTED_BOT_IDS``)."""
    raw = os.getenv("CLORD_TRUSTED_BOT_IDS", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def is_message_authorized(
    message: discord.Message,
    is_allowed: Callable[[Any], bool],
) -> bool:
    """Whether *message* may drive this bot (#507, #508).

    Infrastructure bypasses the human allowlist so that configuring an owner
    does not break it:

    * **Webhook** messages (``webhook_id`` set) — possession of the webhook URL
      is itself authorization (CI/CD triggers, E2E).
    * **Trusted bots** (``CLORD_TRUSTED_BOT_IDS``) — companion bots treated like
      humans; pre-authorized.

    Any other bot is rejected.  Humans must satisfy *is_allowed* (the cog's
    owner / role predicate), so ``DISCORD_OWNER_ID`` still restricts access to
    the owner **without** locking out webhooks or trusted bots.

    Applies to every invocation that has a message behind it.  Slash commands
    have no message and can never be webhook-driven, so they call *is_allowed*
    directly.
    """
    if message.webhook_id:
        return True
    if message.author.bot:
        return message.author.id in _trusted_bot_ids()
    return is_allowed(message.author)


async def owns(
    *,
    home_channel_id: int | None,
    channel_id: int | None,
    thread_id: int | None = None,
    resolvers: Iterable[_Resolver] = (),
    session_get: Callable[[int], Awaitable[Any]] | None = None,
) -> bool:
    """Whether this instance is responsible for this channel / thread (#596).

    True when it can point at a record claiming the place:

    1. the channel is the configured ``DISCORD_CHANNEL_ID`` (*home_channel_id*);
    2. one of *resolvers* finds a ``/clord-init`` / ``/clord-thread-init``
       binding for it — each is called ``(channel_id, thread_id=...)`` and a
       non-``None`` return means "bound";
    3. *session_get* finds a row for the thread in this instance's ``sessions``
       table — the thread is ours even if its channel later lost its binding.

    Otherwise the answer is no, and the caller must stay **silent**: an error in
    a channel we do not own is one more bot talking over the one that does
    (#522).

    The lookups are passed in rather than reached for, so the one rule can be
    applied both to a bot (``process_commands``, via :func:`owns_channel`) and
    to a cog holding its own resolvers and repository — without either growing
    a second copy of the rule.

    A lookup that raises answers nothing rather than granting ownership — a DB
    hiccup must not promote a bystander into the owner, which is the exact
    failure #596 is about.
    """
    if channel_id is not None and channel_id == home_channel_id:
        return True

    if channel_id is not None:
        for resolve in resolvers:
            try:
                if await resolve(channel_id, thread_id=thread_id) is not None:
                    return True
            except Exception:
                logger.warning("command gate: binding lookup failed — not claiming", exc_info=True)

    if thread_id is not None and session_get is not None:
        try:
            if await session_get(thread_id) is not None:
                return True
        except Exception:
            logger.warning("command gate: session lookup failed — not claiming", exc_info=True)

    return False


async def owns_channel(bot: Any, channel: Any) -> bool:
    """:func:`owns` for a bot, addressed by the channel a command was invoked in.

    Collects the bot's own lookups — ``ChannelRepoCog``'s resolvers and the
    ``sessions`` repository — and applies the shared rule to them.  The cog is
    fetched by name and duck-typed on purpose: the gate sits below the cogs and
    must not import them, and a consumer may register its own resolver under
    that name (the Zero-Config Principle cuts both ways).

    A channel outside a guild (a DM) is always ours: it reaches exactly this
    bot, so there is no other instance to defer to.
    """
    if getattr(channel, "guild", None) is None:
        return True

    channel_id = getattr(channel, "id", None)
    thread_id: int | None = None
    if isinstance(channel, discord.Thread):
        thread_id = channel.id
        channel_id = channel.parent_id or channel.id

    channel_cog = bot.get_cog("ChannelRepoCog") if hasattr(bot, "get_cog") else None
    resolvers = [
        resolve
        for name in ("resolve_tmux_manager", "resolve_manager")
        if (resolve := getattr(channel_cog, name, None)) is not None
    ]
    session_repo = getattr(bot, "session_repo", None)

    return await owns(
        home_channel_id=getattr(bot, "channel_id", None),
        channel_id=channel_id,
        thread_id=thread_id,
        resolvers=resolvers,
        session_get=getattr(session_repo, "get", None),
    )
