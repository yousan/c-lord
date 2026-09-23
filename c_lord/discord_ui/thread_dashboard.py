"""Thread status dashboard — live embed showing all active session states.

Posts and maintains a pinned embed in the main channel that shows which
threads are processing automatically vs. waiting for user input.
When a thread transitions to WAITING_INPUT, the bot mentions the turn's poster
(``notify_user_id``, falling back to the configured owner) so Discord's
notification system surfaces the request immediately (#481).

Why the mention is its OWN message (not appended to Claude's final reply)
------------------------------------------------------------------------
Two different actors post: Claude posts the answer itself via the discord-reply
skill (``POST /api/reply``), while c-lord posts this ``@poster`` mention when the
bot's turn-completion detector fires (WAITING_INPUT). Merging them into one
message was considered (issue discussion under #365) and is intentionally NOT
done:

* Editing Claude's last message to append ``@owner`` does **not** work — Discord
  edits do not trigger a push notification, which defeats the entire purpose of
  the mention (pinging the owner's device).
* Having the skill append ``@owner`` to Claude's final reply *would* ping (it is
  a new message), but it moves the notification's reliability from the bot
  (which authoritatively detects "turn done") to Claude (which does not reliably
  know which of its replies is the final one). That trade-off was judged not
  worth it, so the mention stays a separate, bot-authored message.

Issue: https://github.com/yousan/c-lord/issues/67
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum

import discord

from ..claude.types import UsageLimit
from ..notify_policy import owner_fallback_allowed
from ..utils.logger import log_ctx

logger = logging.getLogger(__name__)

# Embed colours
_COLOR_PROCESSING = 0x5865F2  # Discord blurple — all good, keep working
_COLOR_WAITING = 0xFEE75C  # Yellow — needs owner attention
_COLOR_IDLE = 0x99AAB5  # Grey — no active sessions

# State icons and labels shown in the embed
_STATE_ICON: dict[str, str] = {
    "processing": "🟢",
    "waiting": "🟡",
}
_STATE_LABEL: dict[str, str] = {
    "processing": "Auto-processing",
    "waiting": "Waiting for input",
}

#: Embed title that marks a message as a Session Status board. The board is
#: identified by this title + our own authorship — nothing else is ever
#: adopted or deleted (#720).
DASHBOARD_TITLE = "📊 Session Status"

#: How far back in channel history a starting bot looks for the board it should
#: take over. Deep enough to find it in a busy channel, shallow enough to cost
#: a handful of API calls at startup.
_BOARD_SCAN_LIMIT = 500

#: Upper bound on how many dead boards one start deletes. A channel that
#: accumulated boards for months is cleaned over a few starts rather than in
#: one multi-thousand-request burst.
_SWEEP_MAX_DELETES = 400

#: Stop sweeping after this many delete failures — they mean "not allowed",
#: not "try harder".
_SWEEP_MAX_FAILURES = 5

#: Opt out of deleting the dead boards (the board is still reused, never
#: duplicated). Set ``CLORD_DASHBOARD_SWEEP=0`` to keep the history as-is.
_SWEEP_ENV_FLAG = "CLORD_DASHBOARD_SWEEP"
_OFF_VALUES = {"0", "false", "no", "off"}

# Threads older than this are pruned from the dashboard automatically.
# Keeps the embed from accumulating stale entries after a long idle period.
_STALE_HOURS = 0.05  # VERIFY ONLY (#754): 3 min instead of 4h — never merge

#: How often the board looks for rows that went stale (#754). Pruning used to
#: happen only inside a state change, so a day with no posts left 47-hour-old
#: rows reading "0s ago". A tick that prunes nothing makes no Discord call.
_PRUNE_INTERVAL_SECONDS = 20  # VERIFY ONLY (#754)


def _sweep_enabled() -> bool:
    """Whether startup deletes the dead boards of earlier processes (#720)."""
    return os.getenv(_SWEEP_ENV_FLAG, "").strip().lower() not in _OFF_VALUES


class ThreadState(str, Enum):  # noqa: UP042 — requires-python = ">=3.10", StrEnum is 3.11+
    """Lifecycle state of a Claude Code session thread."""

    PROCESSING = "processing"
    """Claude Code CLI is currently running in this thread."""

    WAITING_INPUT = "waiting"
    """Claude finished responding; awaiting the next user message."""


def _completion_text(
    mention_id: int,
    no_response: bool,
    usage_limit: UsageLimit | None = None,
) -> str:
    """The turn-end ping. Says what actually happened (#562, #631).

    "終わりました" is a summons: the user drops what they are doing and comes to
    look. When the turn produced nothing at all, that summons is a lie, and a
    notification that lies stops being worth reading. So a turn that never
    produced a response says so, and tells the reader what to do next instead of
    implying an answer is waiting.

    #631 adds the second half of that principle: "もう一度送ってください" is also
    a lie when the account is rate limited, because sending it again cannot
    work until the limit resets. A limited turn therefore reports the limit and
    its reset time, and says nothing about resending.

    The mention trails the text either way so Discord's push preview leads with
    the message rather than "@you" (#495).
    """
    if usage_limit is not None:
        when = (
            f"{usage_limit.resets_at} に回復します"
            if usage_limit.resets_at
            else "回復時刻は報告されませんでした"
        )
        return (
            f"⏳ Claude の{usage_limit.scope}（上限）に達したため、このターンは実行されていません。"
            f"{when}。それまでは送り直しても同じ結果になります。 <@{mention_id}>"
        )
    if no_response:
        return (
            "⚠️ 応答がありませんでした — Claude がこのターンを開始しませんでした。"
            f"もう一度送るか、tmux ペインを確認してください。 <@{mention_id}>"
        )
    return f"🟡 Claude has finished — your reply is needed here. <@{mention_id}>"


async def _as_async_iter(source: object) -> AsyncIterator[discord.Message]:
    """Iterate a discord.py listing that may be an iterator *or* a coroutine.

    ``TextChannel.pins()`` returns a list to await on discord.py < 2.6 and an
    async iterator from 2.6 on; c-lord supports both (``discord.py>=2.4``).
    """
    if hasattr(source, "__aiter__"):
        async for item in source:  # type: ignore[attr-defined]
            yield item
        return
    for item in await source:  # type: ignore[misc]
        yield item


@dataclass
class _ThreadInfo:
    thread_id: int
    description: str
    state: ThreadState
    started_at: float = field(default_factory=time.monotonic)
    state_changed_at: float = field(default_factory=time.monotonic)


class ThreadStatusDashboard:
    """Maintains a live status embed in the bot's main channel.

    Lifecycle
    ---------
    1. Call ``await dashboard.initialize()`` once after the bot is ready.
    2. Call ``await dashboard.set_state(...)`` on every state transition.
    3. Call ``await dashboard.remove(thread_id)`` when a thread is no longer
       relevant (optional — stale entries are auto-pruned after ``_STALE_HOURS``,
       by a timer that ``initialize()`` starts, even when no state changes: #754).
    4. Turns nobody posted for (scheduler / webhook / ``/skill``) go through
       :func:`board_turn`; ``ClaudeChatCog`` calls ``set_state`` itself because
       its turn-end transition also carries the completion ping.

    One board per channel (#720)
    ----------------------------
    ``initialize()`` runs on every bot start, so it must not *post* on every
    bot start: it used to, and the production channel ended up holding 369
    boards — 77% of everything ever posted there — of which exactly one was
    alive. Worse, 📌 pointed at four boards from 2026-02-24, because the board
    is pinned when it is posted and the pin of the *live* board kept failing
    silently (Discord caps a channel at 50 pins).

    So a starting bot **takes over the board that is already there**: it looks
    through recent history and the pin list for a message it wrote itself
    carrying a ``DASHBOARD_TITLE`` embed, edits the newest one in place, makes
    sure that one is pinned, and deletes the dead ones in the background.
    Nothing else in the channel is ever touched.

    Thread safety
    -------------
    All public methods are coroutines protected by an ``asyncio.Lock``.
    """

    def __init__(
        self,
        channel: discord.TextChannel,
        owner_id: int | None = None,
        bot_user_id: int | None = None,
    ) -> None:
        self._channel = channel
        self._bot_user_id = bot_user_id
        self._sweep_task: asyncio.Task[None] | None = None
        self._prune_task: asyncio.Task[None] | None = None
        self._owner_id = owner_id
        self._threads: dict[int, _ThreadInfo] = {}
        self._dashboard_message: discord.Message | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def channel_id(self) -> int:
        """Id of the channel this dashboard posts in."""
        return self._channel.id

    async def initialize(self) -> None:
        """Take over the channel's board — or post one if there is none (#720).

        Never posts a second board: an already-initialised dashboard just
        refreshes, and a fresh process adopts the board its predecessor left
        behind. The dead boards of earlier processes are deleted in the
        background (opt out with ``CLORD_DASHBOARD_SWEEP=0``).
        """
        stale: list[discord.Message] = []
        self._start_prune_timer()
        async with self._lock:
            if self._dashboard_message is not None:
                # Already live in this process — refresh it, do not add one.
                await self._refresh_dashboard()
                await self._ensure_pinned()
                return

            embed = self._build_embed()
            for candidate in await self._find_own_boards():
                if self._dashboard_message is None and await self._adopt(candidate, embed):
                    continue
                stale.append(candidate)

            if self._dashboard_message is None:
                self._dashboard_message = await self._channel.send(embed=embed)
                logger.info(
                    "Posted a new Session Status board (%s) in channel %s",
                    getattr(self._dashboard_message, "id", "?"),
                    getattr(self._channel, "id", "?"),
                )

            await self._ensure_pinned()

        if stale:
            # Fire-and-forget: deleting hundreds of dead boards is rate-limited
            # and must never hold up on_ready. Keep the reference so the task
            # is not garbage collected mid-flight.
            self._sweep_task = asyncio.create_task(self._sweep_dead_boards(stale))

    async def _adopt(self, candidate: discord.Message, embed: discord.Embed) -> bool:
        """Try to take over *candidate* as the live board. True when adopted."""
        try:
            await candidate.edit(embed=embed)
        except discord.HTTPException:
            logger.warning(
                "Could not take over Session Status board %s; treating it as dead",
                getattr(candidate, "id", "?"),
                exc_info=True,
            )
            return False
        self._dashboard_message = candidate
        logger.info(
            "Took over the Session Status board (%s) left by a previous start",
            getattr(candidate, "id", "?"),
        )
        return True

    async def _ensure_pinned(self) -> None:
        """Pin the live board, and say so out loud when that fails (#678).

        A silent DEBUG line here is why the channel's 📌 pointed at a
        2026-02-24 board for half a year.
        """
        message = self._dashboard_message
        if message is None or getattr(message, "pinned", False):
            return
        try:
            await message.pin()
        except discord.HTTPException as exc:
            logger.warning(
                "Session Status board %s is NOT pinned (%s) — 📌 will not show the "
                "live board. Give the bot Manage Messages, or free a pin slot "
                "(Discord caps a channel at 50 pins).",
                getattr(message, "id", "?"),
                exc,
            )

    # ------------------------------------------------------------------
    # Finding / sweeping the boards of earlier processes (#720)
    # ------------------------------------------------------------------

    def _own_user_id(self) -> int | None:
        """Our own Discord user id, or None when it cannot be established."""
        if isinstance(self._bot_user_id, int):
            return self._bot_user_id
        me = getattr(getattr(self._channel, "guild", None), "me", None)
        user_id = getattr(me, "id", None)
        return user_id if isinstance(user_id, int) else None

    def _is_own_board(self, message: discord.Message, bot_user_id: int) -> bool:
        if getattr(getattr(message, "author", None), "id", None) != bot_user_id:
            return False
        return any(getattr(e, "title", None) == DASHBOARD_TITLE for e in (message.embeds or []))

    async def _find_own_boards(self) -> list[discord.Message]:
        """Our boards already in the channel, newest first.

        Reads both recent history and the pin list: the boards behind 📌 can be
        far older than any sane history scan (2026-02-24 in the #720 channel),
        and those are exactly the ones holding the pin slot hostage.
        """
        bot_user_id = self._own_user_id()
        if bot_user_id is None:
            logger.warning(
                "Cannot determine the bot's own user id — skipping the Session Status "
                "board scan and posting a fresh board (channel %s)",
                getattr(self._channel, "id", "?"),
            )
            return []

        found: dict[int, discord.Message] = {}
        for source, description in (
            (lambda: self._channel.history(limit=_BOARD_SCAN_LIMIT), "history"),
            (lambda: self._channel.pins(), "pins"),
        ):
            try:
                async for message in _as_async_iter(source()):
                    if self._is_own_board(message, bot_user_id):
                        found.setdefault(message.id, message)
            except discord.HTTPException:
                logger.warning(
                    "Could not read channel %s %s while looking for the Session Status board",
                    getattr(self._channel, "id", "?"),
                    description,
                    exc_info=True,
                )

        # Snowflake ids are monotonic in time: newest board first.
        return sorted(found.values(), key=lambda m: m.id, reverse=True)

    async def _sweep_dead_boards(self, boards: list[discord.Message]) -> None:
        """Delete the boards earlier processes left behind (#720 AC5).

        Only messages this bot wrote itself, only ones carrying the board
        embed, and never the live one. Bounded per start; whatever is left over
        is picked up by the next one.
        """
        if not _sweep_enabled():
            logger.info(
                "Session Status sweep disabled (%s=0) — %d dead board(s) left in place",
                _SWEEP_ENV_FLAG,
                len(boards),
            )
            return

        deleted = failed = 0
        for message in boards[:_SWEEP_MAX_DELETES]:
            try:
                await message.delete()
                deleted += 1
            except discord.NotFound:
                deleted += 1  # someone beat us to it — same outcome
            except discord.HTTPException:
                failed += 1
                if failed >= _SWEEP_MAX_FAILURES:
                    logger.warning(
                        "Stopping the Session Status sweep after %d failed deletes "
                        "(missing permissions?) — %d dead board(s) remain",
                        failed,
                        len(boards) - deleted,
                        exc_info=True,
                    )
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Session Status sweep aborted unexpectedly", exc_info=True)
                break

        remaining = len(boards) - deleted
        logger.info(
            "Session Status sweep: deleted %d dead board(s), %d failed, %d left for the next start",
            deleted,
            failed,
            max(remaining, 0),
        )

    async def set_state(
        self,
        thread_id: int,
        state: ThreadState,
        description: str,
        thread: discord.Thread | None = None,
        notify_user_id: int | None = None,
        no_response: bool = False,
        usage_limit: UsageLimit | None = None,
        preempted: bool = False,
    ) -> None:
        """Update a thread's state and refresh the dashboard embed.

        When transitioning to ``WAITING_INPUT`` for the first time, the bot
        posts a reply in *thread* mentioning the turn's poster so Discord
        surfaces the notification immediately.

        Parameters
        ----------
        thread_id:
            Discord thread ID.
        state:
            New ``ThreadState`` value.
        description:
            Short human-readable summary (e.g. the first 100 chars of the prompt).
        thread:
            The ``discord.Thread`` object, required for mentions.
        notify_user_id:
            #481: the Discord user to @-mention on the WAITING_INPUT transition —
            the poster of this turn. Falls back to the configured ``owner_id``
            when ``None``. This makes the completion ping reach whoever is
            actually waiting (any guild, any authorized user) instead of a single
            fixed owner, and still fires when no owner is configured.
        preempted:
            #583: this turn ended because the user's next message replaced it,
            not because Claude finished. The state still moves — the turn IS
            over — but nobody is summoned: the ping would land seconds after
            they typed, tell them their reply is needed, and be answered by a
            new turn one second later. They are already here.
        """
        async with self._lock:
            prev_state = self._threads[thread_id].state if thread_id in self._threads else None

            if thread_id not in self._threads:
                self._threads[thread_id] = _ThreadInfo(
                    thread_id=thread_id,
                    description=description,
                    state=state,
                )
            else:
                info = self._threads[thread_id]
                info.state = state
                info.state_changed_at = time.monotonic()
                if description:
                    info.description = description

            # #481: mention the turn's poster (notify_user_id); fall back to the
            # configured owner. Mention on the first WAITING_INPUT transition.
            # #525: the owner fallback for a turn nobody human asked for is
            # deployment policy — a server running many automated threads reads
            # "Claude has finished" pings for threads it never opened as noise.
            owner_fallback = self._owner_id if owner_fallback_allowed("completion") else None
            mention_id = notify_user_id if notify_user_id is not None else owner_fallback
            should_mention = (
                state == ThreadState.WAITING_INPUT
                and prev_state != ThreadState.WAITING_INPUT
                and mention_id is not None
                and thread is not None
                # #583: a turn the user replaced summons nobody.
                and not preempted
            )

            await self._refresh_dashboard()

        # Send mention outside the lock to avoid holding it during an HTTP call
        # ``mention_id`` is non-None whenever ``should_mention`` is set, but that
        # is established inside the lock above and does not narrow out here.
        if should_mention and thread is not None and mention_id is not None:
            try:
                # #495: the mention trails the text so the Discord push preview
                # leads with "Claude has finished…" instead of "@you". A user
                # mention pings anywhere in the content, so trailing it does not
                # weaken the notification.
                await thread.send(_completion_text(mention_id, no_response, usage_limit))
            except discord.HTTPException:
                logger.debug(
                    "Failed to send completion mention in thread %d", thread_id, exc_info=True
                )

    async def remove(self, thread_id: int) -> None:
        """Remove a thread from the dashboard and refresh."""
        async with self._lock:
            self._threads.pop(thread_id, None)
            await self._refresh_dashboard()

    async def prune_stale_rows(self) -> None:
        """Drop rows idle for ``_STALE_HOURS`` and repaint — no state change needed (#754).

        Edits the board only when a row actually went, so the periodic tick
        costs nothing on a board that has nothing to drop.
        """
        async with self._lock:
            if self._prune_stale():
                await self._refresh_dashboard()

    async def aclose(self) -> None:
        """Stop the periodic prune. The board itself stays in the channel."""
        task, self._prune_task = self._prune_task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ------------------------------------------------------------------
    # Periodic prune (#754)
    # ------------------------------------------------------------------

    def _start_prune_timer(self) -> None:
        """Start the prune loop once. ``initialize()`` runs on every reconnect."""
        if self._prune_task is not None and not self._prune_task.done():
            return
        self._prune_task = asyncio.create_task(
            self._prune_loop(), name="clord-session-status-prune"
        )

    async def _prune_loop(self) -> None:
        """Prune on a timer, so the board empties even on a day nobody posts."""
        while True:
            await asyncio.sleep(_PRUNE_INTERVAL_SECONDS)
            try:
                await self.prune_stale_rows()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The board is decoration (#632): one failed tick must not end
                # the timer, or the board freezes again — the bug this fixes.
                logger.warning(
                    "Session Status prune tick failed; retrying next tick", exc_info=True
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _refresh_dashboard(self) -> None:
        """Edit the dashboard message with current state.

        Must be called while the caller holds ``self._lock``.
        Falls back to posting a new message if the original is gone.
        """
        self._prune_stale()

        if self._dashboard_message is None:
            return

        embed = self._build_embed()
        try:
            await self._dashboard_message.edit(embed=embed)
        except discord.NotFound:
            logger.debug("Dashboard message was deleted; re-posting")
            try:
                self._dashboard_message = await self._channel.send(embed=embed)
            except discord.HTTPException:
                logger.warning("Failed to re-post dashboard message", exc_info=True)
        except discord.HTTPException:
            logger.debug("Failed to edit dashboard message", exc_info=True)

    def _prune_stale(self) -> list[int]:
        """Remove threads that haven't changed state in ``_STALE_HOURS`` hours.

        Returns the ids it removed.
        """
        cutoff = time.monotonic() - _STALE_HOURS * 3600
        stale = [tid for tid, info in self._threads.items() if info.state_changed_at < cutoff]
        for tid in stale:
            del self._threads[tid]
        if stale:
            logger.info(
                "Session Status: dropped %d row(s) idle for %dh (threads=%s)",
                len(stale),
                _STALE_HOURS,
                stale,
            )
        return stale

    def _build_embed(self) -> discord.Embed:
        """Construct the Discord embed reflecting current thread states."""
        if not self._threads:
            return discord.Embed(
                title=DASHBOARD_TITLE,
                description="No active sessions.",
                color=_COLOR_IDLE,
            )

        any_waiting = any(t.state == ThreadState.WAITING_INPUT for t in self._threads.values())
        color = _COLOR_WAITING if any_waiting else _COLOR_PROCESSING

        embed = discord.Embed(title=DASHBOARD_TITLE, color=color)

        now = time.monotonic()
        for info in sorted(self._threads.values(), key=lambda t: t.started_at):
            icon = _STATE_ICON[info.state.value]
            label = _STATE_LABEL[info.state.value]
            elapsed = int(now - info.state_changed_at)
            mins, secs = divmod(elapsed, 60)
            elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            desc_preview = info.description[:60] + ("…" if len(info.description) > 60 else "")

            embed.add_field(
                name=f"{icon} <#{info.thread_id}>",
                value=f"**{label}** · {elapsed_str} ago\n{desc_preview}",
                inline=False,
            )

        embed.set_footer(text="Updates automatically · stale entries removed after 4h")
        return embed


# ----------------------------------------------------------------------
# Putting a turn on the board (#754)
# ----------------------------------------------------------------------


def dashboard_of(bot: object) -> ThreadStatusDashboard | None:
    """The bot's Session Status board, or None when it has none (yet)."""
    dashboard = getattr(bot, "thread_dashboard", None)
    return dashboard if isinstance(dashboard, ThreadStatusDashboard) else None


async def safe_set_state(
    dashboard: ThreadStatusDashboard,
    thread_id: int,
    state: ThreadState,
    description: str,
    **kwargs: object,
) -> None:
    """Update the dashboard, never letting its failure take the turn down (#632).

    The dashboard embed is decoration: a closed aiohttp session, a revoked
    permission or a Discord outage must not stop Claude from running or from
    answering. Before #632 the PROCESSING update was the one un-guarded Discord
    call on the turn path, so any of those killed the task before
    ``run_claude_with_config`` was reached and the user's message vanished with
    no reply, no ❌, nothing. Swallowed — but logged at WARNING, never silently.
    """
    try:
        await dashboard.set_state(thread_id, state, description, **kwargs)  # type: ignore[arg-type]
    except Exception:
        logger.warning(
            "%s dashboard set_state(%s) failed; continuing the turn",
            log_ctx(thread_id=thread_id),
            state.value,
            exc_info=True,
        )


@contextlib.asynccontextmanager
async def board_turn(
    dashboard: ThreadStatusDashboard | None, thread_id: int, label: str
) -> AsyncIterator[None]:
    """🟢 on the board while the block runs, 🟡 after — however it ends (#754).

    For the turns nobody posted for: the scheduler, webhook triggers and
    ``/skill``. Before #754 only ``ClaudeChatCog`` touched the board, so a
    scheduled run was live while the board said nothing was.

    *label* is what the row says — pass something already public (the task
    name, the trigger prefix, the ``/skill`` line), **not** a server-side
    prompt: those never reached Discord before, and the board is in the main
    channel.

    Only the board changes. No ``thread`` is passed, so the turn-end
    "Claude has finished" ping stays exactly as it was for these paths (none).
    """
    if dashboard is None:
        yield
        return
    description = label[:100].replace("\n", " ")
    await safe_set_state(dashboard, thread_id, ThreadState.PROCESSING, description)
    try:
        yield
    finally:
        await safe_set_state(dashboard, thread_id, ThreadState.WAITING_INPUT, description)
