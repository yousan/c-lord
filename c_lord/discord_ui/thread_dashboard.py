"""The 📊 Session Status board and the turn-end ping.

One embed per channel lists every thread Claude is working in, each marked
🟢 (working by itself) or 🟡 (waiting for a person). The board is opt-in; the
state table behind it, and the ping below, work whether or not it is shown.

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
from ..discord_failures import DISCORD_CALL_FAILURES
from ..notify_policy import owner_fallback_allowed
from ..utils.logger import log_ctx

logger = logging.getLogger(__name__)

# The board's stripe: the calmest colour that is still true.
_COLOR_PROCESSING = 0x5865F2  # every listed thread is working by itself
_COLOR_WAITING = 0xFEE75C  # at least one thread wants a person
_COLOR_IDLE = 0x99AAB5  # nothing listed

# How a state reads on the board, keyed by ``ThreadState.value``.
_STATE_ICON = {"processing": "🟢", "waiting": "🟡"}
_STATE_LABEL = {"processing": "Auto-processing", "waiting": "Waiting for input"}

# A row shows this much of its description; longer ones end in an ellipsis.
_EXCERPT_CHARS = 60
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

#: Opt in to the board itself (#761). Off by default: every start used to post
#: or rewrite the board in the channel, and development restarts the bot many
#: times a day. The turn-end ping is NOT governed by this — only the board.
_BOARD_ENV_FLAG = "CLORD_SESSION_STATUS_BOARD"
_ON_VALUES = {"1", "true", "yes", "on"}

#: A row whose state has not changed for this many hours is dropped.
_STALE_HOURS = 4
#: How often the board looks for rows that went stale (#754). Pruning used to
#: happen only inside a state change, so a day with no posts left 47-hour-old
#: rows reading "0s ago". A tick that prunes nothing makes no Discord call.
_PRUNE_INTERVAL_SECONDS = 300


def board_enabled() -> bool:
    """Whether this deployment shows the 📊 Session Status board (#761, opt-in)."""
    return os.getenv(_BOARD_ENV_FLAG, "").strip().lower() in _ON_VALUES


def _sweep_enabled() -> bool:
    """Whether startup deletes the dead boards of earlier processes (#720)."""
    return os.getenv(_SWEEP_ENV_FLAG, "").strip().lower() not in _OFF_VALUES


class ThreadState(str, Enum):  # noqa: UP042 — StrEnum is 3.11+, we support 3.10
    """Where a thread stands, as the board shows it."""

    PROCESSING = "processing"  # Claude is running a turn here
    WAITING_INPUT = "waiting"  # Claude answered; a person is next


def _completion_text(
    mention_id: int,
    no_response: bool,
    usage_limit: UsageLimit | None = None,
    login_required: bool = False,
    model_unavailable: str | None = None,
    api_error: str | None = None,
) -> str:
    """The turn-end ping. Says what actually happened (#562, #631, #812).

    "終わりました" is a summons: the user drops what they are doing and comes to
    look. When the turn produced nothing at all, that summons is a lie, and a
    notification that lies stops being worth reading. So a turn that never
    produced a response says so, and tells the reader what to do next instead of
    implying an answer is waiting.

    #631 adds the second half of that principle: "もう一度送ってください" is also
    a lie when the account is rate limited, because sending it again cannot
    work until the limit resets. A limited turn therefore reports the limit and
    its reset time, and says nothing about resending.

    #812 is the same lie for a logged-out Claude Code: nothing in the thread can
    fix it, only ``/login`` on the host can.

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
    if login_required:
        return (
            "🔑 Claude Code のログインが切れているため、このターンは実行されていません。"
            "ホストで `claude` を開いて `/login` してください。"
            f"ログインするまでは送り直しても同じ結果になります。 <@{mention_id}>"
        )
    if model_unavailable is not None:
        return (
            f"🧩 Claude Code がモデル `{model_unavailable}` を使えないため、"
            "このターンは実行されていません。`/model` で使えるモデルに戻してください。"
            f"戻すまでは送り直しても同じ結果になります。 <@{mention_id}>"
        )
    if api_error is not None:
        # #908: the turn stopped on Claude Code's API error — not "finished".
        return f"❌ Claude Code が API エラーで止まりました。{api_error} <@{mention_id}>"
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


def _elapsed_text(seconds: float) -> str:
    """``59s`` / ``1m 0s`` / ``125m 3s`` — minutes are never rolled into hours."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


@dataclass
class _ThreadInfo:
    """One row of the board."""

    thread_id: int
    description: str
    state: ThreadState
    # When the row first appeared — rows are listed oldest first.
    started_at: float = field(default_factory=time.monotonic)
    # When ``state`` was last set — the "ago" figure, and the staleness clock.
    state_changed_at: float = field(default_factory=time.monotonic)

    def as_field(self, now: float) -> tuple[str, str]:
        """This row as an embed field ``(name, value)``, seen at monotonic *now*."""
        key = self.state.value
        excerpt = self.description[:_EXCERPT_CHARS]
        if len(self.description) > _EXCERPT_CHARS:
            excerpt += "…"
        age = _elapsed_text(now - self.state_changed_at)
        return (
            f"{_STATE_ICON[key]} <#{self.thread_id}>",
            f"**{_STATE_LABEL[key]}** · {age} ago\n{excerpt}",
        )


class ThreadStatusDashboard:
    """A channel's single 📊 Session Status board, plus the turn-end ping.

    Every public method is a coroutine, and one lock guards the state table.

    How it is used:

    1. Build one per channel and ``await initialize()`` once the bot is ready.
    2. ``await set_state(...)`` whenever a thread starts or finishes a turn.
    3. ``await remove(thread_id)`` takes a thread off once it is no longer
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

    With the board switched off (the default) none of that happens: the state
    table and the turn-end ping still work, and no board message exists.
    """

    def __init__(
        self,
        channel: discord.TextChannel,
        owner_id: int | None = None,
        bot_user_id: int | None = None,
        board: bool | None = None,
    ) -> None:
        self._channel = channel
        # #761: None → ``CLORD_SESSION_STATUS_BOARD`` decides (off by default).
        self._board_enabled = board_enabled() if board is None else board
        self._retired = False
        self._bot_user_id = bot_user_id
        self._sweep_task: asyncio.Task[None] | None = None
        self._prune_task: asyncio.Task[None] | None = None
        self._owner_id = owner_id
        # thread_id -> its row, in the order the threads first appeared
        self._threads: dict[int, _ThreadInfo] = {}
        # The live board message; None while the board is off or not yet up.
        self._dashboard_message: discord.Message | None = None
        # Guards ``_threads`` and every repaint of the board.
        self._lock = asyncio.Lock()

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
        if not self._board_enabled:
            await self._retire_boards()
            return

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

    async def _retire_boards(self) -> None:
        """Board off (#761): post nothing, and sweep the boards earlier starts left.

        A board nobody updates any more is the #754 lie — rows reading
        "0s ago" for days — so it goes, under the same rules as the #720 sweep
        (only our own boards; ``CLORD_DASHBOARD_SWEEP=0`` keeps them). Done
        once per process: ``on_ready`` fires again on every reconnect.
        """
        if self._retired:
            return
        self._retired = True
        boards = await self._find_own_boards()
        logger.info(
            "Session Status board is off (set %s=1 to show it) — %d board(s) from "
            "earlier starts to retire in channel %s",
            _BOARD_ENV_FLAG,
            len(boards),
            getattr(self._channel, "id", "?"),
        )
        if boards:
            self._sweep_task = asyncio.create_task(self._sweep_dead_boards(boards))

    async def _adopt(self, candidate: discord.Message, embed: discord.Embed) -> bool:
        """Try to take over *candidate* as the live board. True when adopted."""
        try:
            await candidate.edit(embed=embed)
        except DISCORD_CALL_FAILURES:
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
        except DISCORD_CALL_FAILURES as exc:
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
            except DISCORD_CALL_FAILURES:
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
        login_required: bool = False,
        model_unavailable: str | None = None,
        api_error: str | None = None,
        already_pinged: int | None = None,
    ) -> None:
        """Record *thread_id*'s state and repaint the board.

        The first time a thread arrives at WAITING_INPUT, this additionally
        posts a reply in *thread* mentioning the turn's poster so Discord
        surfaces the notification immediately.

        thread_id:
            The thread whose row is being set.
        state:
            The state it is in now.
        description:
            A short text for the row. An empty string keeps the text the row
            already has.
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
        already_pinged:
            #915: the user the turn's ❌ embed already mentioned. For a turn
            whose cause is known (usage limit / login / model) that embed says
            everything the turn-end line would, so the line is skipped when it
            would ping the same person a second time.
        """
        async with self._lock:
            row = self._threads.get(thread_id)
            previous = None if row is None else row.state
            if row is None:
                self._threads[thread_id] = _ThreadInfo(
                    thread_id=thread_id, description=description, state=state
                )
            else:
                # Same state or not, the row was just touched: restart its clock.
                row.state = state
                row.state_changed_at = time.monotonic()
                if description:
                    row.description = description
            # #481: mention the turn's poster (notify_user_id); fall back to the
            # configured owner. Mention on the first WAITING_INPUT transition.
            # #525: the owner fallback for a turn nobody human asked for is
            # deployment policy — a server running many automated threads reads
            # "Claude has finished" pings for threads it never opened as noise.
            owner_fallback = self._owner_id if owner_fallback_allowed("completion") else None
            mention_id = notify_user_id if notify_user_id is not None else owner_fallback
            should_mention = (
                state == ThreadState.WAITING_INPUT
                and previous != ThreadState.WAITING_INPUT
                and mention_id is not None
                and thread is not None
                # #583: a turn the user replaced summons nobody.
                and not preempted
            )
            # #967: an API error (#908) is a known cause too — its embed carries
            # the same advice the line would.
            known_cause = (
                usage_limit is not None
                or login_required
                or model_unavailable is not None
                or api_error is not None
            )
            if should_mention and known_cause and already_pinged == mention_id:
                should_mention = False
                logger.info(
                    "%s turn-end line skipped — the error embed already pinged <@%s> (#915)",
                    log_ctx(thread_id=thread_id),
                    mention_id,
                )

            await self._refresh_dashboard()

        # The ping is sent with the lock released: an HTTP round-trip must not
        # make every other thread's state change queue up behind it.
        # ``mention_id`` is non-None whenever ``should_mention`` is set, but that
        # is established inside the lock above and does not narrow out here.
        if should_mention and thread is not None and mention_id is not None:
            try:
                # #495: the mention trails the text so the Discord push preview
                # leads with "Claude has finished…" instead of "@you". A user
                # mention pings anywhere in the content, so trailing it does not
                # weaken the notification.
                await thread.send(
                    _completion_text(
                        mention_id,
                        no_response,
                        usage_limit,
                        login_required,
                        model_unavailable,
                        api_error,
                    )
                )
            except discord.HTTPException:
                logger.debug(
                    "Failed to send completion mention in thread %d", thread_id, exc_info=True
                )

    async def remove(self, thread_id: int) -> None:
        """Take *thread_id* off the board (a thread that is not on it is fine)."""
        async with self._repainting():
            self._threads.pop(thread_id, None)

    @contextlib.asynccontextmanager
    async def _repainting(self) -> AsyncIterator[None]:
        """Hold the lock around a change to the table, then repaint the board.

        A change that raises is not followed by a repaint.
        """
        async with self._lock:
            yield
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

    async def _refresh_dashboard(self) -> None:
        """Repaint the board from the state table. The caller holds the lock.

        Stale rows are dropped first, board or no board.
        """
        self._prune_stale()
        if self._dashboard_message is None:
            return
        embed = self._build_embed()
        try:
            await self._dashboard_message.edit(embed=embed)
        except discord.NotFound:
            await self._post_replacement(embed)
        except DISCORD_CALL_FAILURES:
            logger.debug("Failed to edit dashboard message", exc_info=True)

    async def _post_replacement(self, embed: discord.Embed) -> None:
        """The board was deleted from under us: post it again (unpinned).

        If that fails too, the vanished message stays on record and the next
        repaint tries again.
        """
        logger.debug("Dashboard message was deleted; re-posting")
        try:
            self._dashboard_message = await self._channel.send(embed=embed)
        except DISCORD_CALL_FAILURES:
            logger.warning("Failed to re-post dashboard message", exc_info=True)

    def _prune_stale(self) -> list[int]:
        """Remove threads that haven't changed state in ``_STALE_HOURS`` hours.

        Returns the ids it removed.
        """
        cutoff = time.monotonic() - _STALE_HOURS * 3600
        stale = [tid for tid, row in self._threads.items() if row.state_changed_at < cutoff]
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
        """The board for the state table as it is right now (no Discord call)."""
        rows = sorted(self._threads.values(), key=lambda row: row.started_at)
        if not rows:
            return discord.Embed(
                title=DASHBOARD_TITLE,
                description="No active sessions.",
                color=_COLOR_IDLE,
            )

        anyone_waiting = any(row.state == ThreadState.WAITING_INPUT for row in rows)
        color = _COLOR_WAITING if anyone_waiting else _COLOR_PROCESSING
        embed = discord.Embed(title=DASHBOARD_TITLE, color=color)
        now = time.monotonic()
        for row in rows:
            name, value = row.as_field(now)
            embed.add_field(name=name, value=value, inline=False)
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
