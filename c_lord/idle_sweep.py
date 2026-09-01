"""アイドルなワークスペースを定期的に拾う土台 — Issue #572（#574 から抽出）.

**なぜ土台を切り出すのか。** #574 の7日自動停止と #572 の4時間スリープは、
やることが違うだけで**選び方と回し方は完全に同じ**:

* 人間の発言 (``sessions.last_used_at``) だけを時計に使う
* 走っているターンには絶対に触らない
* 対象はほぼ必ず Discord 側でアーカイブ済みなので ``fetch_channel`` まで見る
* 引けなかったスレッドが行列の先頭を塞がないようにする (#593)

これを2つ書けば、片方に入れた修正がもう片方に入らない日が必ず来る。#538 は
まさにその形の事故（同じ結果を2箇所に書いてズレた）だった。だから**回し方は
ここ1箇所**にして、サブクラスは「1件に対して何をするか」(:meth:`IdleSweepLoop._act`)
だけを持つ。

判定に tmux の ``window_activity`` を使わない理由は変わらない。Claude の TUI は
スピナーとステータス行を描き続けるので、**誰も触っていなくても更新される** —
本番実測では生きている 51 本すべてが「直近1〜3時間以内に activity あり」に
見えた。正直な時計は最後の人間の発言だけ。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database.repository import SessionRecord

logger = logging.getLogger(__name__)

__all__ = ["IdleSweepLoop", "parse_timestamp", "select_idle_workspaces"]

_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


def parse_timestamp(ts: str | None) -> datetime.datetime | None:
    """A stored wall-clock timestamp, or ``None`` when it cannot be read."""
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
    threshold: datetime.timedelta,
    in_flight: set[int] | None = None,
) -> list[int]:
    """Thread ids nobody has touched for longer than *threshold*, oldest first.

    A record is selected only when **all** hold:

    * it is not already stopped — a stopped workspace has no Claude left to
      reclaim, and revisiting it would re-post its notice on every tick, forever,
    * no turn is running for it. ``last_used_at`` is stamped when a turn *starts*,
      so a turn that has been grinding for a week still looks idle by timestamp
      alone; killing it would destroy real work, and
    * its ``last_used_at`` parses **and** is strictly older than the threshold. An
      unparsable timestamp is skipped rather than guessed — nothing gets destroyed
      because a date failed to parse, and a boundary tick must not fire early.

    Ordering is oldest-first so runs and their evidence stay comparable.
    """
    in_flight = in_flight or set()
    cutoff = now - threshold

    aged: list[tuple[datetime.datetime, int]] = []
    for rec in records:
        if rec.closed_at:
            continue
        if rec.thread_id in in_flight:
            continue
        last = parse_timestamp(rec.last_used_at)
        if last is None:
            logger.debug(
                "idle-sweep: thread=%s has an unparsable last_used_at (%r) — skipping",
                rec.thread_id,
                rec.last_used_at,
            )
            continue
        if last >= cutoff:
            continue
        aged.append((last, rec.thread_id))

    aged.sort(key=lambda pair: pair[0])
    return [tid for _, tid in aged]


class IdleSweepLoop:
    """Background task that acts on workspaces nobody has touched for a while.

    Subclasses decide **what** happens to one workspace (:meth:`_act`); this class
    owns **which** and **when**. Nothing here knows about stopping or sleeping.
    """

    #: Short name used in log lines, e.g. ``idle-stop`` / ``idle-sleep``.
    name = "idle-sweep"

    def __init__(
        self,
        bot: object,
        session_repo: object,
        *,
        threshold: datetime.timedelta,
        interval_seconds: float,
        now_fn: object | None = None,
        in_flight_fn: object | None = None,
        max_per_tick: int = 1000,
    ) -> None:
        self._bot = bot
        self._repo = session_repo
        self._threshold = threshold
        self._interval = interval_seconds
        self._now = now_fn or datetime.datetime.now
        self._in_flight = in_flight_fn
        self._max_per_tick = max_per_tick
        # Threads Discord says do not exist. Retrying them is pure noise, and
        # leaving them at the head of an oldest-first list is what froze the
        # backlog at 93 for four days (#593).
        self._unresolvable: set[int] = set()
        self._task: object | None = None

    # ── subclass hooks ───────────────────────────────────────────────────

    async def _act(self, thread_id: int, channel: object) -> bool:
        """Do the thing to one workspace. ``True`` when work actually happened.

        The return value is what the per-tick cap and the summary log count, so
        it must mean "this workspace changed", not "the call returned". Counting
        attempts instead is how a sweep reports progress it never made (#604).
        """
        raise NotImplementedError

    def _needs_action(self, record: SessionRecord) -> bool:
        """Cheap pre-filter, before a Discord fetch is spent on *record*."""
        return True

    def _mark_visited(self, record: SessionRecord) -> None:
        """Called once *record* has been visited without raising."""

    def _threshold_label(self) -> str:
        return str(self._threshold)

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the loop task. Idempotent, and a no-op when disabled."""
        if self._threshold <= datetime.timedelta(0):
            logger.info("%s: disabled", self.name)
            return
        if self._task is not None and not getattr(self._task, "done", lambda: True)():
            return
        self._task = asyncio.create_task(self._run(), name=self.name.replace("-", "_"))
        logger.info(
            "Started %s loop (threshold=%s, interval=%.0fs)",
            self.name,
            self._threshold_label(),
            self._interval,
        )

    async def stop(self) -> None:
        """Cancel the loop. Safe to call multiple times."""
        if self._task is None:
            return
        self._task.cancel()  # type: ignore[attr-defined]
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task  # type: ignore[misc]
        self._task = None

    async def _run(self) -> None:
        # Let the bot finish connecting before the first sweep.
        await asyncio.sleep(min(60.0, self._interval))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s tick raised", self.name)
            await asyncio.sleep(self._interval)

    # ── one pass ─────────────────────────────────────────────────────────

    def _current_in_flight(self) -> set[int]:
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
        after its inactivity window — which for anything a sweep selects has
        often already elapsed. So the cache misses many candidates, and the
        original code treated that as "deleted" and moved on silently (#593).
        Ask the API before believing it.

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
            logger.info("%s: thread=%d no longer exists — not retrying", self.name, thread_id)
            self._unresolvable.add(thread_id)
            return None
        except discord.Forbidden:
            logger.info(
                "%s: thread=%d is not visible to the bot — not retrying", self.name, thread_id
            )
            self._unresolvable.add(thread_id)
            return None
        except Exception as exc:
            logger.info(
                "%s: thread=%d could not be fetched (%s) — will retry", self.name, thread_id, exc
            )
            return None

    async def tick(self) -> int:
        """One sweep. Returns how many workspaces actually changed.

        Public so tests can drive it without a real clock.
        """
        if self._threshold <= datetime.timedelta(0):
            return 0

        records = await self._repo.list_all(limit=1000)  # type: ignore[attr-defined]
        by_id = {rec.thread_id: rec for rec in records}
        due = select_idle_workspaces(
            records,
            now=self._now(),  # type: ignore[operator]
            threshold=self._threshold,
            in_flight=self._current_in_flight(),
        )
        due = [
            tid for tid in due if tid not in self._unresolvable and self._needs_action(by_id[tid])
        ]
        if not due:
            return 0

        backlog = len(due)
        if backlog > self._max_per_tick:
            # Never truncate silently: a log line that only counts what was done
            # reads as "everything is handled".
            logger.info(
                "%s: %d workspace(s) past the threshold; handling up to %d this tick, "
                "the rest follow on later ticks",
                self.name,
                backlog,
                self._max_per_tick,
            )

        acted = 0
        for thread_id in due:
            if acted >= self._max_per_tick:
                break
            # Count against the cap only once a workspace actually changed, and
            # keep walking past the ones that cannot be reached. Slicing the
            # oldest-first list *before* resolving meant five unreachable
            # threads at the head starved the other 88 forever (#593).
            channel = await self._resolve_thread(thread_id)
            if channel is None:
                continue
            try:
                changed = await self._act(thread_id, channel)
            except Exception:
                logger.exception("%s: failed on thread=%d", self.name, thread_id)
                continue
            self._mark_visited(by_id[thread_id])
            if changed:
                acted += 1

        if acted:
            self._log_summary(acted)
        return acted

    def _log_summary(self, acted: int) -> None:
        logger.info(
            "%s: handled %d workspace(s) idle for more than %s",
            self.name,
            acted,
            self._threshold_label(),
        )
