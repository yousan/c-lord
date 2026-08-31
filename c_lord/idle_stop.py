"""7日アイドルで自動停止する — Issue #574, design agreed in #540.

**Why seven days.** Measured across 2650 human turns in the production
transcripts, the gap before someone comes back to a thread is p95 = 2.42h and
**only 0.49% of returns exceed 7 days** — one in two hundred. Stopping loses
nothing (the working copy, the conversation and the volumes all survive; the
docker stack just needs starting again), so being wrong is nearly free. Waiting
30 days instead buys 1.8 percentage points and costs a month of held host ports.

**How idle is measured.** Not from tmux. ``window_activity`` looked like the
obvious signal and is useless: Claude's TUI repaints its spinner and status line
continuously, so a pane nobody has touched for days still reports fresh
activity — measured on the production host, *all 51* live sessions looked active
within the last 3 hours. The honest clock is the last human turn, which is what
``sessions.last_used_at`` records.

The selection is a pure function so the policy can be tested without a bot, a
database, or a clock.
"""

from __future__ import annotations

import datetime
import logging
import os
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database.repository import SessionRecord

logger = logging.getLogger(__name__)

#: Days of no human activity before a workspace is stopped automatically.
#:
#: A constant rather than something derived from the host, because the number
#: describes **how people use Discord**, not how much RAM the machine has — the
#: return-gap distribution it comes from is the same on a 47 GiB host and a
#: Raspberry Pi. That is what makes it safe to ship as a zero-config default
#: without assuming anything about the operator's hardware (#540).
IDLE_STOP_DAYS_DEFAULT = 7

_ENV_VAR = "CLORD_IDLE_STOP_DAYS"

_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


def idle_stop_days() -> int:
    """Threshold in days. ``0`` disables automatic stopping entirely.

    A malformed value falls back to the default rather than disabling the
    feature: a typo in ``.env`` should not silently switch off memory reclamation
    on a host that needs it, and it should certainly not crash the bot.
    """
    raw = os.getenv(_ENV_VAR, "").strip()
    if not raw:
        return IDLE_STOP_DAYS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer — using %d", _ENV_VAR, raw, IDLE_STOP_DAYS_DEFAULT)
        return IDLE_STOP_DAYS_DEFAULT
    if value < 0:
        logger.warning("%s=%r is negative — using %d", _ENV_VAR, raw, IDLE_STOP_DAYS_DEFAULT)
        return IDLE_STOP_DAYS_DEFAULT
    return value


def idle_label_for(days: int) -> str:
    """The span as it appears in the notice ("7日間")."""
    return f"{days}日間"


def _parse(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.datetime.strptime(ts[:26], fmt)
        except ValueError:
            continue
    return None


def select_idle_workspaces(
    records: Iterable[SessionRecord],
    *,
    now: datetime.datetime,
    threshold_days: int,
    in_flight: set[int] | None = None,
) -> list[int]:
    """Thread ids to stop, oldest-idle first.

    A record is selected only when **all** hold:

    * it is not already stopped — re-stopping would re-post the notice on every
      tick, forever,
    * no turn is running for it. ``last_used_at`` is stamped when a turn *starts*,
      so a turn that has been grinding for over a week still looks idle by
      timestamp alone; killing it would destroy real work, and
    * its ``last_used_at`` parses **and** is strictly older than the threshold. An
      unparsable timestamp is skipped rather than guessed — nothing gets destroyed
      because a date failed to parse, and a boundary tick must not fire a day
      early.

    Ordering is oldest-first so runs and their evidence stay comparable.
    """
    in_flight = in_flight or set()
    cutoff = now - datetime.timedelta(days=threshold_days)

    aged: list[tuple[datetime.datetime, int]] = []
    for rec in records:
        if rec.closed_at:
            continue
        if rec.thread_id in in_flight:
            continue
        last = _parse(rec.last_used_at)
        if last is None:
            logger.debug(
                "idle-stop: thread=%s has an unparsable last_used_at (%r) — skipping",
                rec.thread_id,
                rec.last_used_at,
            )
            continue
        if last >= cutoff:
            continue
        aged.append((last, rec.thread_id))

    aged.sort(key=lambda pair: pair[0])
    return [tid for _, tid in aged]


#: Most workspaces stopped in a single sweep.
#:
#: Switching this on for the first time finds a backlog — 20 on the production
#: host — and stopping all of them at once means that many embeds and thread
#: archives back to back. That is the burst #277 had to fix for the rename loop,
#: and it would be the reader's first experience of the feature. Everything
#: selected has already been idle for a week, so spreading the backlog over
#: consecutive ticks costs nothing.
MAX_STOPS_PER_TICK = 5

#: How often to look for idle workspaces. Generous on purpose: the threshold is
#: measured in days, so checking every 10 minutes is already 1000× finer than it
#: needs to be, and each tick reads the whole session table.
_TICK_INTERVAL_SECONDS = 600.0


async def _noop_ack() -> None:
    """No interaction to defer — nobody is waiting on a slash command."""
    return None


def _thread_responder(channel: object):
    """A ``respond`` callable that posts into *channel*.

    Built by a factory rather than defined inside the loop so the channel is
    bound per call instead of being read from the enclosing loop variable —
    otherwise every deferred post would target whichever thread the loop
    happened to end on.
    """
    import contextlib

    async def _post(content=None, *, embed=None, ephemeral=False, **_kw) -> None:
        # The user is not here to receive an interaction response, so the notice
        # goes into the thread itself.
        with contextlib.suppress(Exception):
            if embed is not None:
                await channel.send(embed=embed)  # type: ignore[attr-defined]
            elif content:
                await channel.send(content)  # type: ignore[attr-defined]

    return _post


class IdleStopLoop:
    """Background task that stops workspaces nobody has touched for a while.

    Calls the very function ``/workspace-stop`` calls, passing
    :attr:`WorkspaceReason.IDLE`. Writing a second "stop a workspace" path here
    would be the #538 failure again — two implementations of the same outcome
    always drift — so this loop only decides *which* and *when*, never *how*.
    """

    def __init__(
        self,
        bot: object,
        session_repo: object,
        *,
        threshold_days: int | None = None,
        interval_seconds: float = _TICK_INTERVAL_SECONDS,
        now_fn: object | None = None,
        in_flight_fn: object | None = None,
        max_per_tick: int = MAX_STOPS_PER_TICK,
    ) -> None:
        self._bot = bot
        self._repo = session_repo
        self._threshold = threshold_days if threshold_days is not None else idle_stop_days()
        self._interval = interval_seconds
        self._now = now_fn or datetime.datetime.now
        self._in_flight = in_flight_fn
        self._max_per_tick = max_per_tick
        # Threads Discord says do not exist. Retrying them is pure noise, and
        # leaving them at the head of an oldest-first list is what froze the
        # backlog at 93 for four days (#593).
        self._unresolvable: set[int] = set()
        self._task: object | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the loop task. Idempotent, and a no-op when disabled."""
        import asyncio

        if self._threshold <= 0:
            logger.info("idle-stop: disabled (%s=0)", _ENV_VAR)
            return
        if self._task is not None and not getattr(self._task, "done", lambda: True)():
            return
        self._task = asyncio.create_task(self._run(), name="idle_stop")
        logger.info(
            "Started idle-stop loop (threshold=%dd, interval=%.0fs)",
            self._threshold,
            self._interval,
        )

    async def stop(self) -> None:
        """Cancel the loop. Safe to call multiple times."""
        import asyncio
        import contextlib

        if self._task is None:
            return
        self._task.cancel()  # type: ignore[attr-defined]
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task  # type: ignore[misc]
        self._task = None

    async def _run(self) -> None:
        import asyncio

        # Let the bot finish connecting before the first sweep.
        await asyncio.sleep(min(60.0, self._interval))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("idle-stop tick raised")
            await asyncio.sleep(self._interval)

    # ── one pass ─────────────────────────────────────────────────────────

    def _current_in_flight(self) -> set[int]:
        import contextlib

        if self._in_flight is not None:
            with contextlib.suppress(Exception):
                return set(self._in_flight())  # type: ignore[operator]
        with contextlib.suppress(Exception):
            cog = self._bot.get_cog("ClaudeChatCog")  # type: ignore[attr-defined]
            active = getattr(cog, "_active_tasks", None)
            if isinstance(active, dict):
                return {int(t) for t in active}
        return set()

    async def _resolve_thread(self, thread_id: int) -> object | None:
        """The thread for *thread_id*, or None when it cannot be reached.

        ``get_channel`` only reads the cache, and Discord auto-archives a thread
        after its inactivity window — which for anything this loop selects has
        by definition already elapsed. So the cache misses **almost every**
        candidate, and the original code treated that as "deleted" and moved on
        silently (#593). Ask the API before believing it.

        A thread the API reports as gone is remembered, so it is never tried
        again this process. Anything else (a network blip, a rate limit) is left
        alone: writing a workspace off for the life of the process because one
        request failed is the wrong trade.
        """
        import discord

        channel = self._bot.get_channel(thread_id)  # type: ignore[attr-defined]
        if channel is not None:
            return channel
        try:
            return await self._bot.fetch_channel(thread_id)  # type: ignore[attr-defined]
        except discord.NotFound:
            logger.info(
                "idle-stop: thread=%d no longer exists in Discord — not retrying", thread_id
            )
            self._unresolvable.add(thread_id)
            return None
        except discord.Forbidden:
            logger.info("idle-stop: thread=%d is not visible to the bot — not retrying", thread_id)
            self._unresolvable.add(thread_id)
            return None
        except Exception as exc:
            logger.info(
                "idle-stop: thread=%d could not be fetched (%s) — will retry", thread_id, exc
            )
            return None

    async def tick(self) -> None:
        """One sweep. Public so tests can drive it without a real clock."""

        if self._threshold <= 0:
            return

        records = await self._repo.list_all(limit=1000)  # type: ignore[attr-defined]
        due = select_idle_workspaces(
            records,
            now=self._now(),  # type: ignore[operator]
            threshold_days=self._threshold,
            in_flight=self._current_in_flight(),
        )
        if not due:
            return

        due = [tid for tid in due if tid not in self._unresolvable]
        if not due:
            return

        backlog = len(due)
        if backlog > self._max_per_tick:
            # Never truncate silently: a log line that only counts what was done
            # reads as "everything is handled".
            logger.info(
                "idle-stop: %d workspace(s) past the threshold; stopping up to %d this "
                "tick, the rest follow on later ticks",
                backlog,
                self._max_per_tick,
            )

        cog = self._bot.get_cog("SessionManageCog")  # type: ignore[attr-defined]
        impl = getattr(cog, "_close_workspace_impl", None)
        if impl is None:
            logger.warning("idle-stop: SessionManageCog unavailable — skipping %d", len(due))
            return

        from .workspace_notice import WorkspaceReason

        label = idle_label_for(self._threshold)
        stopped = 0
        for thread_id in due:
            if stopped >= self._max_per_tick:
                break
            # Count against the cap only once a thread is actually stopped, and
            # keep walking past the ones that cannot be reached. Slicing the
            # oldest-first list *before* resolving meant five unreachable
            # threads at the head starved the other 88 forever (#593).
            channel = await self._resolve_thread(thread_id)
            if channel is None:
                continue

            try:
                await impl(
                    channel=channel,
                    respond=_thread_responder(channel),
                    ack=_noop_ack,
                    reason=WorkspaceReason.IDLE,
                    idle_label=label,
                )
                stopped += 1
            except Exception:
                logger.exception("idle-stop: failed to stop thread=%d", thread_id)

        if stopped:
            logger.info(
                "idle-stop: stopped %d workspace(s) idle for more than %d day(s)",
                stopped,
                self._threshold,
            )
