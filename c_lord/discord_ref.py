"""Reactive Discord-link enrichment (Issue #318, route-B Unit 1 — "push").

When a human pastes a Discord message link (``https://discord.com/channels/...``)
or uses Discord's reply feature, c-lord resolves the referenced content
*server-side* (via the already-authenticated ``bot``) and inlines it VERBATIM
into the prompt before it reaches Claude. This removes Claude's path-selection
from the loop entirely: Claude no longer has to decide how to fetch another
channel (and no longer gives up when the third-party MCP returns Missing Access).

Design notes:
- This is "push": c-lord stays *outside* Claude Code and only widens the human's
  own input turn — structurally the same as the existing attachment-inlining in
  ``claude_chat._build_prompt_and_images``. It injects nothing into Claude Code
  (no skill/tool/MCP), so it honours route-B principle #2. The enriched text is
  one ``send-keys`` payload that also shows verbatim in the tmux pane, so the
  terminal==Discord equivalence (principle #3) holds. The principle #1 tension
  ("c-lord adds content the human did not literally type") is accepted as the
  attachment-inlining precedent — see ``docs/DESIGN_DECISIONS.md``.
- The bot token is never handled here; discord.py carries auth internally. Only
  fetched message *text* ever enters the prompt.
- This function MUST NOT raise — a failed fetch becomes an explicit inline note
  so Claude still sees the human's intent.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import discord

logger = logging.getLogger(__name__)

# Hard caps — mirror the attachment-inlining limits in ``claude_chat``.
_MAX_REFS = 5  # resolve at most this many distinct references per turn
_MAX_REF_CHARS = 2000  # truncate any single referenced message to this many chars
_MAX_TOTAL_CHARS = 6000  # stop inlining once the appended blocks exceed this
_HISTORY_LIMIT = 5  # for a channel-only link, inline this many recent messages

_ENV_FLAG = "CLORD_RESOLVE_DISCORD_LINKS"
_DISABLE_VALUES = frozenset({"0", "false", "no", "off"})

# https://(canary.|ptb.)discord(app).com/channels/<guild|@me>/<channel>[/<message>]
_LINK_RE = re.compile(
    r"https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+|@me)/(?P<channel>\d+)(?:/(?P<message>\d+))?"
)


def _enabled(enabled: bool | None) -> bool:
    if enabled is not None:
        return enabled
    return os.getenv(_ENV_FLAG, "1").strip().lower() not in _DISABLE_VALUES


def _author_name(msg: Any) -> str:
    author = getattr(msg, "author", None)
    if author is None:
        return "unknown"
    return getattr(author, "display_name", None) or getattr(author, "name", None) or "unknown"


def _channel_label(channel: Any) -> str:
    name = getattr(channel, "name", None)
    return f"#{name}" if name else f"channel:{getattr(channel, 'id', '?')}"


def _truncate(text: str) -> str:
    if len(text) <= _MAX_REF_CHARS:
        return text
    return text[:_MAX_REF_CHARS] + "\n…[truncated by c-lord]"


def _format_block(channel: Any, msg: Any) -> str:
    content = getattr(msg, "content", "") or ""
    header = (
        f"--- Referenced Discord message "
        f"({_channel_label(channel)}, {_author_name(msg)}, {getattr(msg, 'created_at', '')}) ---"
    )
    return f"\n\n{header}\n{_truncate(content)}\n--- end ---"


def _label_for(channel_id: int, message_id: int | None) -> str:
    base = f"discord.com/channels/.../{channel_id}"
    return f"{base}/{message_id}" if message_id is not None else base


async def _resolve_one(bot: Any, channel_id: int, message_id: int | None) -> str | None:
    """Resolve a single reference into an inline block, or a ``[could not fetch]``
    note, or ``None`` (nothing to inline). Never raises."""
    label = _label_for(channel_id, message_id)
    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
    except Exception as exc:  # Forbidden / NotFound / HTTPException / anything
        logger.debug("enrich: channel %s unresolved: %s", channel_id, exc)
        return f"\n\n[c-lord could not fetch {label}: {type(exc).__name__}]"
    if channel is None:
        return f"\n\n[c-lord could not fetch {label}: channel not found]"

    try:
        if message_id is not None:
            msg = await channel.fetch_message(message_id)
            return _format_block(channel, msg)
        # Channel-only link → inline a small recent window (oldest first).
        msgs = [m async for m in channel.history(limit=_HISTORY_LIMIT)]
        msgs.reverse()
        if not msgs:
            return None
        return "".join(_format_block(channel, m) for m in msgs)
    except Exception as exc:
        logger.debug("enrich: message %s/%s unresolved: %s", channel_id, message_id, exc)
        return f"\n\n[c-lord could not fetch {label}: {type(exc).__name__}]"


async def enrich_discord_references(
    prompt: str,
    message: discord.Message,
    bot: discord.Client,
    *,
    enabled: bool | None = None,
) -> str:
    """Append the verbatim content of any Discord messages the human referenced.

    Args:
        prompt: The prompt built from the human's message (already includes
            attachment inlining). Scanned for ``discord.com/channels/...`` links.
        message: The incoming Discord message — its ``reference`` (Discord reply)
            is resolved in addition to links found in ``prompt``.
        bot: The authenticated discord client; used server-side to fetch content.
            The bot token is never exposed to ``prompt``.
        enabled: Override the ``CLORD_RESOLVE_DISCORD_LINKS`` env gate (for tests).
            When ``None``, defaults to on unless the env var is 0/false/no/off.

    Returns:
        ``prompt`` unchanged when disabled or nothing is referenced; otherwise
        ``prompt`` with clearly-delimited ``--- Referenced Discord message ---``
        blocks appended. Never raises.
    """
    if not _enabled(enabled):
        return prompt

    trigger_id = getattr(message, "id", None)
    refs: list[tuple[int, int | None]] = []
    seen: set[tuple[int, int | None]] = set()

    def _add(channel_id: int, message_id: int | None) -> None:
        # Don't re-inline the human's own message if they linked it.
        if message_id is not None and trigger_id is not None and message_id == trigger_id:
            return
        key = (channel_id, message_id)
        if key in seen:
            return
        seen.add(key)
        refs.append(key)

    # 1) Discord reply reference (resolved first so it leads the appended blocks).
    ref = getattr(message, "reference", None)
    if ref is not None:
        cid = getattr(ref, "channel_id", None)
        mid = getattr(ref, "message_id", None)
        if cid is not None and mid is not None:
            _add(int(cid), int(mid))

    # 2) Links pasted in the prompt text.
    for m in _LINK_RE.finditer(prompt or ""):
        raw_msg = m.group("message")
        _add(int(m.group("channel")), int(raw_msg) if raw_msg else None)

    if not refs:
        return prompt

    # Observability: count only (never the fetched content) — also the staging
    # before/after signal for this feature.
    logger.info("enrich_discord_references: resolving %d Discord reference(s)", len(refs))

    blocks: list[str] = []
    total = 0
    for channel_id, message_id in refs[:_MAX_REFS]:
        block = await _resolve_one(bot, channel_id, message_id)
        if not block:
            continue
        if total + len(block) > _MAX_TOTAL_CHARS:
            blocks.append("\n\n[c-lord: omitted further referenced messages — total size limit]")
            break
        blocks.append(block)
        total += len(block)

    if len(refs) > _MAX_REFS:
        dropped = len(refs) - _MAX_REFS
        blocks.append(f"\n\n[c-lord: {dropped} more reference(s) not resolved — limit {_MAX_REFS}]")

    return prompt + "".join(blocks)
