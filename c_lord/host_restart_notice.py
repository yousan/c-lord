"""Tell a thread its Claude died mid-turn in a host reboot — #807.

A c-lord-only restart leaves Claude running in tmux (#406). A **host** reboot
does not: tmux goes down with everything else, and because the shutdown is not
c-lord's, ``cog_unload`` never gets to say anything. A thread that was in the
middle of a turn then sits there looking busy, forever — on 2026-09-25 the whole
fleet went down and nobody noticed for three days. Nothing is lost (posting
again resumes the conversation, #700); what is missing is *knowing*.

So on startup, for every thread whose Claude is **not** running, c-lord reads
**the last entry of Claude Code's own transcript** and, when it shows a turn cut
off halfway, posts one line into the thread. Nothing else is consulted:

- **No DB state** — not for "was it running", and not for "already told them"
  (yousan, 2026-09-28: tracking it in the DB is how the bugs keep coming back).
  Deduplication is read off the thread itself: if our notice is already there
  and nobody has written since, it is not posted again.
- **No mention, no button, no automatic resume.** Whether to continue is the
  person's call (#406); a button that looks pressable and does nothing is #796.

Which transcript is the thread's is decided exactly as the mirror decides it
(:class:`~c_lord.transcript.resolver.ThreadSessionResolver`, #773).

The expected behaviour is written down in ``docs/specs/host-restart-notice.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

import discord

from .transcript.resolver import ThreadSessionResolver, derive_project_dir
from .utils.logger import log_ctx

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .tmux import LiveClaude

logger = logging.getLogger(__name__)

#: The one line posted into a thread whose Claude died mid-turn.
HOST_RESTART_NOTICE = (
    "⚠️ ホストの再起動で、作業の途中で Claude が止まりました。"
    "続けるなら、このスレに「続けて」と投稿してください。"
)

# Reading backwards from the end: the last message usually sits under a handful
# of small bookkeeping lines, so one block is almost always enough. The cap only
# bounds a pathological single line (a huge tool output); the transcript can be
# ~100 MB and must never be slurped (#537).
_TAIL_BLOCK_BYTES = 64 * 1024
_TAIL_MAX_BYTES = 32 * 1024 * 1024

# How far back in a thread to look for our own notice. Only the stretch since
# the last message someone else wrote matters, so this is generous.
_DEDUP_SCAN_MESSAGES = 50

# User rows that are not a request for Claude to do anything: a turn the user
# stopped themselves (Esc), and slash-command bookkeeping.
_NOT_A_TURN_PREFIXES = (
    "[Request interrupted by user",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)

# An assistant row with one of these is Claude still going: it asked for a tool,
# or it is one streamed block of a message whose end was never written.
_MID_TURN_STOP_REASONS = (None, "tool_use", "pause_turn")


def _is_message(event: object) -> TypeGuard[dict[str, Any]]:
    """A user/assistant row of the main conversation — the rows a turn is made of."""
    if not isinstance(event, dict) or event.get("type") not in ("user", "assistant"):
        return False
    if event.get("isMeta") or event.get("isSidechain"):
        return False
    return isinstance(event.get("message"), dict)


def _last_message(path: Path) -> dict[str, Any] | None:
    """The last conversation row in ``path``, reading from the end. Blocking."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            partial = b""
            scanned = 0
            while pos > 0 and scanned < _TAIL_MAX_BYTES:
                step = min(_TAIL_BLOCK_BYTES, pos)
                pos -= step
                scanned += step
                f.seek(pos)
                lines = (f.read(step) + partial).split(b"\n")
                # Unless we reached the start, the first piece may be the tail
                # of a line that begins in the next block back.
                partial = lines.pop(0) if pos > 0 else b""
                for raw in reversed(lines):
                    event = _parse(raw)
                    if _is_message(event):
                        return event
            if pos == 0 and partial:
                event = _parse(partial)
                if _is_message(event):
                    return event
    except OSError:
        return None
    return None


def _parse(raw: bytes) -> object:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None  # a torn final line, or garbage — not evidence either way


def _user_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if (
        isinstance(content, list)
        and content
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
    ):
        return str(content[0].get("text", ""))
    return None


def transcript_stopped_mid_turn(path: Path) -> bool:
    """Whether the last entry of ``path`` is a turn that never finished.

    - assistant, ``end_turn`` (or any other final stop) → finished, waiting;
    - assistant asking for a tool / a streamed block with no stop yet → mid-turn;
    - ``tool_result`` → mid-turn (Claude had not seen it yet);
    - a user prompt → mid-turn (Claude never answered it);
    - ``[Request interrupted by user…]`` or a slash command → not a turn.

    Blocking.
    """
    event = _last_message(path)
    if event is None:
        return False
    message = event["message"]
    if event["type"] == "assistant":
        if event.get("isApiErrorMessage"):
            return False
        return message.get("stop_reason") in _MID_TURN_STOP_REASONS
    text = _user_text(message.get("content"))
    # A tool_result, or a prompt Claude never answered: cut off mid-turn.
    return text is None or not text.lstrip().startswith(_NOT_A_TURN_PREFIXES)


def stopped_mid_turn(project_dir: Path) -> bool:
    """:func:`transcript_stopped_mid_turn` on the transcript this thread owns. Blocking."""
    jsonl = ThreadSessionResolver(project_dir).resolve()
    return jsonl is not None and transcript_stopped_mid_turn(jsonl)


async def _already_noticed(thread: discord.Thread, my_id: int | None) -> bool:
    """Our notice is in the thread and nobody else has written since."""
    async for message in thread.history(limit=_DEDUP_SCAN_MESSAGES):
        if getattr(message.author, "id", None) != my_id:
            return False
        if message.content == HOST_RESTART_NOTICE:
            return True
    return False


async def _resolve_thread(bot: Bot, thread_id: int) -> discord.Thread | None:
    channel = bot.get_channel(thread_id)
    if channel is None:
        with contextlib.suppress(discord.HTTPException):
            channel = await bot.fetch_channel(thread_id)
    return channel if isinstance(channel, discord.Thread) else None


async def notify_host_restart_stops(
    bot: Bot,
    candidates: Sequence[tuple[int, str]],
    *,
    live: LiveClaude | None,
) -> int:
    """Post :data:`HOST_RESTART_NOTICE` where Claude died mid-turn. Returns how many.

    ``candidates`` is ``(thread_id, working_dir)`` for each open thread that owns
    its transcript (closed ones are the caller's to leave out). ``live`` is what
    :func:`c_lord.tmux.live_claude_panes` found; ``None`` — tmux could not be
    asked — posts nothing, because "could not tell" is not "it died".

    Never raises; one unreadable thread does not stop the rest.
    """
    if live is None:
        logger.info("host-restart notice: tmux could not be read — not judging any thread (#807)")
        return 0
    me = getattr(getattr(bot, "user", None), "id", None)
    sent = 0
    for thread_id, working_dir in candidates:
        ctx = log_ctx(thread_id=thread_id)
        if live.covers(thread_id, working_dir):
            continue
        try:
            mid = await asyncio.to_thread(stopped_mid_turn, derive_project_dir(working_dir))
        except Exception:
            logger.warning("%s host-restart notice: transcript unreadable", ctx, exc_info=True)
            continue
        if not mid:
            continue
        thread = await _resolve_thread(bot, thread_id)
        if thread is None:
            logger.info("%s host-restart notice: stopped mid-turn but thread is gone", ctx)
            continue
        if thread.locked:
            logger.info("%s host-restart notice: stopped mid-turn but thread is locked", ctx)
            continue
        try:
            if await _already_noticed(thread, me):
                logger.info("%s host-restart notice: already posted, not repeating", ctx)
                continue
            await thread.send(HOST_RESTART_NOTICE, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.warning("%s host-restart notice: could not post", ctx, exc_info=True)
            continue
        sent += 1
        logger.info("%s host-restart notice: Claude stopped mid-turn — told the thread", ctx)
    return sent
