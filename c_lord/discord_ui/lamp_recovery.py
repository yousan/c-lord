"""Take back a turn lamp a bot restart left behind — #718.

The per-turn lamp (:class:`~c_lord.discord_ui.status.StatusManager`) lives in
the process. When the bot restarts while a turn is running — most often c-lord
deploying itself from inside that very turn — the manager dies and its last
paint stays on the trigger message for good: ⚠️ (before #769 every turn past 30s
read ⚠️) or 🟢. Claude in tmux survives the restart (#406), finishes the turn,
and the thread's answer arrives under a lamp that says "maybe stuck" / "still
working". All six "⚠️ only" lamps measured after #727 were this.

On startup, for each thread that had a turn in flight (``pending_resumes``), the
lamp is adopted by a new manager:

- the transcript says the turn is **over** (it ended while the bot was down) →
  🟡 right away;
- it is **still running** → 🟢, with the ordinary stall monitor fed by the
  transcript (#769), until the mirror reads the turn's end marker, a re-read of
  the transcript says it is over, or a newer turn in the thread takes over — then
  🟡, exactly as a turn that ended in this process would.

Only lamps are touched: the bot's own 🟢/⏳/⚠️. A 🟡 or ❌ is already final.
Failures are logged and dropped — the lamp is decoration (#632).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import discord

from ..turn_end_bus import turn_end_bus
from ..utils.logger import log_ctx
from .status import EMOJI_RUNNING, EMOJI_STALL_HARD, EMOJI_STALL_SOFT, StatusManager

logger = logging.getLogger(__name__)

# ``datetime.UTC`` is 3.11+ and c-lord supports 3.10.
_UTC = timezone.utc  # noqa: UP017

#: The lamps that mean "this turn is not over" — the ones a restart orphans.
_OPEN_LAMPS = (EMOJI_RUNNING, EMOJI_STALL_SOFT, EMOJI_STALL_HARD)

#: How often to look at the mirror's turn-end record (cheap, in memory).
POLL_SECONDS = 2.0
#: How often to re-read the transcript tail — the backstop for an end the mirror
#: did not see (it tails from EOF, so an end written before it started is only
#: visible in the file).
RECHECK_SECONDS = 15.0
#: A turn still "running" after this long is left to the stall monitor's ⚠️;
#: the watcher stops so it cannot live for ever.
MAX_SECONDS = 6 * 3600.0


def _open_lamp(message: discord.Message) -> str | None:
    """The bot's own not-yet-final lamp on *message*, if there is one."""
    for reaction in message.reactions:
        if getattr(reaction, "me", False) and str(reaction.emoji) in _OPEN_LAMPS:
            return str(reaction.emoji)
    return None


async def adopt_orphaned_lamp(
    message: discord.Message,
    *,
    thread_id: int,
    turn_running: Callable[[], Awaitable[bool]],
    poll_seconds: float = POLL_SECONDS,
    recheck_seconds: float = RECHECK_SECONDS,
    max_seconds: float = MAX_SECONDS,
) -> None:
    """Bring *message*'s orphaned lamp back in line with the turn it belongs to.

    *turn_running* answers "is this thread's turn still going?" from the
    transcript. Returns once the lamp is final (or the watch gave up). Run it
    as its own task: while the turn is running it waits for the turn's end.
    """
    ctx = log_ctx(thread_id=thread_id)
    try:
        emoji = _open_lamp(message)
    except Exception:
        logger.warning("%s lamp recovery: could not read the reactions", ctx, exc_info=True)
        return
    if emoji is None:
        return

    manager = StatusManager.adopt(message, emoji, thread_id=thread_id)
    try:
        if not await turn_running():
            logger.info("%s lamp recovery: %s left by a restart, turn is over → 🟡", ctx, emoji)
            await manager.set_waiting()
            return

        logger.info("%s lamp recovery: %s left by a restart, turn still running", ctx, emoji)
        since = datetime.now(_UTC)
        loop = asyncio.get_running_loop()
        started = last_check = loop.time()
        # Makes this task the turn's task, so the monitor behaves as it would
        # for a turn started in this process.
        await manager.set_running()
        while True:
            await asyncio.sleep(poll_seconds)
            now = loop.time()
            ended = turn_end_bus.ended_at(thread_id)
            if ended is not None and ended >= since:
                reason = "turn-end marker"
                break
            if manager.displaced:
                reason = "a newer turn took over"
                break
            if now - last_check >= recheck_seconds:
                last_check = now
                if not await turn_running():
                    reason = "transcript says the turn is over"
                    break
            if now - started >= max_seconds:
                logger.info(
                    "%s lamp recovery: still running after %.0fs, giving up", ctx, now - started
                )
                await manager.cleanup_monitor()
                return
        logger.info("%s lamp recovery: %s → 🟡", ctx, reason)
        await manager.set_waiting()
    except asyncio.CancelledError:
        await manager.cleanup_monitor()
        raise
    except Exception:
        logger.warning("%s lamp recovery failed", ctx, exc_info=True)
        await manager.cleanup_monitor()
