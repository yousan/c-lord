"""常駐ワークスペース数の上限 — Issue #576, design agreed in #540.

**これはバックストップであって主たる制御ではない。** 主たる制御は TTL
(:mod:`c_lord.idle_sleep` の4時間 / :mod:`c_lord.idle_stop` の7日)。ここが在るのは
「TTL が想定していない速さで増えたとき、ホストが落ちる前に止まる」ためだけ。

**``MAX_CONCURRENT_SESSIONS`` はこの役目を果たしていない。** あれは
``async with self._semaphore:`` が**ターンの実行だけ**を包む同時実行数で、ターンが
終われば解放される — tmux の claude は生き続ける。名前が「上限」に見えるせいで
上限だと誤解され続けてきたが、**常駐プロセス数を縛ったことは一度もない**。

**既定値だけは TTL と扱いが違う。** TTL は「人がどう Discord を使うか」で決まる
のでホスト規模に依存しないが、**上限は依存する** (#540)::

    N = max(2, floor(MemTotal_GiB × 0.4 / 0.45 GiB))

* ``0.45 GiB`` — claude 1本の実測限界コスト（PSS private 255–398 MB ＋ 残骸分）
* ``0.4`` — 「c-lord の常駐分がホスト RAM の4割を超えたら、c-lord 専用機でない
  限り事故」という保守側の線。実測ホストには naranu 3.8GB / factorio 3.0GB /
  java 2.6GB が同居していて、全部は使えない

だから固定既定値は配れない。2 GiB の VPS に 30 を配ると、その人のホストが死ぬ。

**超過時は LRU でスリープ。新規は絶対に待たせない。** 実測で yousan は同時に11本の
並行タスクを走らせている。待ちキューを入れると、いま普通にできている並行作業が
塞がれる。**上限を主制御にすると「他人の作業を殺す」か「自分の作業が始まらない」の
どちらかが必ず起きる**ので、殺すほうを選び、しかも殺す先を「いちばん長く使われて
いないもの」に限定する。復帰は投稿1通（#572 の無言復元）なので、外したときの損害は
次のターンが数秒遅いことだけ。

**緊急ブレーキは片方向のみ。** ``MemAvailable`` が総量の10%を3回連続（90秒）下回った
ら、TTL を待たずにアイドルの長い順にスリープさせ、回復したら止める。**余裕がある
から上限を上げる方向には決して動かさない** — 上げる方向は事故を増やすだけで、利用者
から見た挙動が予測不能になる。常時フィードバック（``MemAvailable`` を見て上限を動かし
続ける）も却下: 非決定的でテストできず、「なぜ自分のワークスペースだけ落ちたのか」に
答えられなくなる。実測でも PSI の avg10 は 0.00 で**常時逼迫ではなく間欠**なので、
スパイクだけ潰すブレーキが正しい形。

あるべき動きは ``docs/specs/resident-cap.md``。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import math
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from .idle_sweep import parse_timestamp
from .utils.logger import log_ctx

if TYPE_CHECKING:
    from .database.repository import SessionRecord

logger = logging.getLogger(__name__)

__all__ = [
    "EMERGENCY_CONSECUTIVE_SAMPLES",
    "EMERGENCY_LOW_FRACTION",
    "MIN_RESIDENT_LIMIT",
    "ResidentCapLoop",
    "compute_limit",
    "max_resident_workspaces",
    "memory_available",
    "memory_total",
    "select_lru_victims",
]

_GIB = 1024**3

#: Fraction of the host's memory c-lord's resident processes may occupy.
_MEMORY_BUDGET_FRACTION = 0.4

#: Measured marginal cost of one resident workspace, in GiB.
_WORKSPACE_COST_GIB = 0.45

#: Never compute a limit below this. A limit of 0 or 1 would make c-lord unable
#: to do the thing it exists for; this is a brake, not an off switch.
MIN_RESIDENT_LIMIT = 2

_ENV_VAR = "CLORD_MAX_RESIDENT_WORKSPACES"

#: ``MemAvailable`` below this fraction of the total counts as a low sample.
EMERGENCY_LOW_FRACTION = 0.10

#: Consecutive low samples before the brake fires. At the 30s tick that is 90
#: seconds — long enough that an intermittent spike (the measured PSI avg10 is
#: 0.00) does not trip it, short enough to act before the host swaps itself flat.
EMERGENCY_CONSECUTIVE_SAMPLES = 3

#: How often to sample memory. The cap itself is re-checked on the same tick;
#: both reads are cheap (one ``/proc/meminfo`` read, one ``tmux list-panes -a``).
_TICK_INTERVAL_SECONDS = 30.0

_CGROUP_V2_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V2_CURRENT = Path("/sys/fs/cgroup/memory.current")
_CGROUP_V1_MAX = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
_CGROUP_V1_USAGE = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")

#: cgroup v1 spells "unlimited" as a number near 2^63. Read literally it makes
#: the computed limit astronomical, which is the same as having no limit — the
#: precise failure this module exists to prevent.
_V1_UNLIMITED_FLOOR = 1 << 62


# ── how much memory do we actually have ──────────────────────────────────────


def _read_int(path: Path) -> int | None:
    """First integer in *path*, or ``None`` when it cannot be read."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None  # "max" (v2, unlimited) and anything unexpected


def _read_meminfo_total() -> int:
    """``MemTotal`` in bytes, or 0 when ``/proc/meminfo`` is unreadable."""
    return _read_meminfo_field("MemTotal")


def _read_meminfo_field(field: str) -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(field + ":"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def _cgroup_limit() -> int | None:
    """The container's memory limit in bytes, or None when unconfined.

    Checked **before** ``/proc/meminfo`` because inside a container the latter
    reports the *host's* memory. Reading 47 GiB inside a 2 GiB container yields a
    limit of 41 workspaces and a guaranteed OOM in the one environment that
    actually has a limit (#576 AC2).
    """
    v2 = _read_int(_CGROUP_V2_MAX)
    if v2 is not None and 0 < v2 < _V1_UNLIMITED_FLOOR:
        return v2
    v1 = _read_int(_CGROUP_V1_MAX)
    if v1 is not None and 0 < v1 < _V1_UNLIMITED_FLOOR:
        return v1
    return None


def memory_total() -> int:
    """Memory this process may use, in bytes. cgroup limit wins over the host's."""
    return _cgroup_limit() or _read_meminfo_total()


def memory_available() -> int:
    """Memory still available, in bytes — from the same source as the total.

    Mixing sources would make the emergency brake compare a cgroup total against
    the host's free memory, which is not a ratio of anything.
    """
    limit = _cgroup_limit()
    if limit is not None:
        used = _read_int(_CGROUP_V2_CURRENT)
        if used is None:
            used = _read_int(_CGROUP_V1_USAGE)
        if used is not None:
            return max(0, limit - used)
    return _read_meminfo_field("MemAvailable")


def compute_limit(total_bytes: int) -> int:
    """Resident workspaces that fit in *total_bytes*, per the #540 formula."""
    if total_bytes <= 0:
        return MIN_RESIDENT_LIMIT
    budget_gib = (total_bytes / _GIB) * _MEMORY_BUDGET_FRACTION
    return max(MIN_RESIDENT_LIMIT, math.floor(budget_gib / _WORKSPACE_COST_GIB))


def max_resident_workspaces(*, total_bytes: int | None = None) -> int:
    """The cap. ``0`` disables it entirely.

    ``CLORD_MAX_RESIDENT_WORKSPACES`` wins when it is a non-negative integer.
    **Anything else falls back to the computed value, never to a constant** —
    shipping a fixed default would break every host smaller than the one it was
    measured on, which is the whole reason this one number is derived and the
    TTLs are not (#540).
    """
    computed = compute_limit(total_bytes if total_bytes is not None else memory_total())
    raw = os.getenv(_ENV_VAR, "").strip()
    if not raw:
        return computed
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer — using %d", _ENV_VAR, raw, computed)
        return computed
    if value < 0:
        logger.warning("%s=%r is negative — using %d", _ENV_VAR, raw, computed)
        return computed
    return value


# ── who gets evicted ─────────────────────────────────────────────────────────


def select_lru_victims(
    records: Iterable[SessionRecord],
    *,
    resident: set[int],
    target: int,
    in_flight: set[int],
) -> list[int]:
    """Thread ids to sleep so at most *target* stay resident, longest-idle first.

    Only workspaces that are **actually resident** can be chosen — sleeping one
    that is already asleep would make the count never converge.

    A running turn is never chosen. Evicting live work to satisfy a cap betrays
    what the cap is for: the point is to survive a runaway, not to interrupt the
    person using the tool.

    A resident window with no ``sessions`` row still **counts** (it occupies the
    same memory) but is never chosen: there is no timestamp to order it by, and
    guessing would evict by accident. Those are the tmux reaper's and #613's job.
    """
    if target < 0:
        target = 0
    over = len(resident) - target
    if over <= 0:
        return []

    aged: list[tuple[datetime.datetime, int]] = []
    for rec in records:
        if rec.thread_id not in resident or rec.thread_id in in_flight:
            continue
        last = parse_timestamp(rec.last_used_at)
        if last is None:
            continue
        aged.append((last, rec.thread_id))

    aged.sort(key=lambda pair: pair[0])
    return [tid for _, tid in aged[:over]]


# ── the loop ─────────────────────────────────────────────────────────────────


class ResidentCapLoop:
    """Keeps the resident workspace count under the cap, and brakes on pressure.

    Nothing on the turn path may reference this class. The moment the code that
    creates a workspace consults a cap, "新規は待たせない" is only a convention
    away from being broken — so the guarantee is structural: this loop only ever
    *removes* residents, and never has anything to hand out.
    """

    name = "resident-cap"

    def __init__(
        self,
        bot: object,
        session_repo: object,
        *,
        limit: int | None = None,
        interval_seconds: float = _TICK_INTERVAL_SECONDS,
        resident_fn: object | None = None,
        in_flight_fn: object | None = None,
        mem_fn: object | None = None,
    ) -> None:
        self._bot = bot
        self._repo = session_repo
        # Written once. See :attr:`limit` — there is deliberately no setter.
        self._limit = limit if limit is not None else max_resident_workspaces()
        self._interval = interval_seconds
        self._resident_fn = resident_fn
        self._in_flight_fn = in_flight_fn
        self._mem_fn = mem_fn or (lambda: (memory_total(), memory_available()))
        self._low_streak = 0
        self._braking = False
        self._task: object | None = None

    @property
    def limit(self) -> int:
        """The cap. **Read-only on purpose** (#576 AC7).

        A settable attribute is one refactor away from someone writing "there is
        plenty of memory, let's widen it". Making the value unwritable means the
        upward direction cannot be expressed, not merely that nobody wrote it.
        """
        return self._limit

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not getattr(self._task, "done", lambda: True)():
            return
        self._task = asyncio.create_task(self._run(), name="resident_cap")
        logger.info(
            "Started resident-cap loop (limit=%s, interval=%.0fs, emergency<%.0f%% of total)",
            self._limit if self._limit > 0 else "disabled",
            self._interval,
            EMERGENCY_LOW_FRACTION * 100,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()  # type: ignore[attr-defined]
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task  # type: ignore[misc]
        self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(min(90.0, self._interval * 3))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("resident-cap tick raised")
            await asyncio.sleep(self._interval)

    # ── inputs ───────────────────────────────────────────────────────────

    def _resident(self) -> set[int]:
        if self._resident_fn is not None:
            with contextlib.suppress(Exception):
                return set(self._resident_fn())  # type: ignore[operator]
            return set()
        from .tmux import resident_thread_ids

        with contextlib.suppress(Exception):
            return resident_thread_ids()
        return set()

    def _in_flight(self) -> set[int]:
        if self._in_flight_fn is not None:
            with contextlib.suppress(Exception):
                return set(self._in_flight_fn())  # type: ignore[operator]
            return set()
        with contextlib.suppress(Exception):
            cog = self._bot.get_cog("ClaudeChatCog")  # type: ignore[attr-defined]
            active = getattr(cog, "_active_tasks", None)
            if isinstance(active, dict):
                return {int(t) for t in active}
        return set()

    def _memory(self) -> tuple[int, int]:
        with contextlib.suppress(Exception):
            total, available = self._mem_fn()  # type: ignore[operator, misc]
            return int(total), int(available)
        return 0, 0

    # ── one pass ─────────────────────────────────────────────────────────

    async def tick(self) -> int:
        """One pass. Returns how many workspaces were actually put to sleep."""
        total, available = self._memory()
        under_pressure = self._observe_pressure(total, available)

        if under_pressure:
            return await self._emergency_brake()

        if self._limit <= 0:
            return 0
        return await self._enforce_cap()

    def _observe_pressure(self, total: int, available: int) -> bool:
        """Update the low-sample streak. True when the brake should fire now.

        A single dip means nothing — the measured pressure on this host is
        intermittent (PSI avg10 = 0.00) while the damage is real (2763s of full
        stalls over five days). Requiring a streak is what separates the two.
        """
        if total <= 0:
            self._low_streak = 0
            return False
        if available >= total * EMERGENCY_LOW_FRACTION:
            if self._braking:
                logger.warning(
                    "resident-cap: EMERGENCY brake released — MemAvailable back to %.1f%%",
                    available / total * 100,
                )
                self._braking = False
            self._low_streak = 0
            return False

        self._low_streak += 1
        logger.warning(
            "resident-cap: memory low (%.1f%% available, %d/%d consecutive samples)",
            available / total * 100,
            self._low_streak,
            EMERGENCY_CONSECUTIVE_SAMPLES,
        )
        return self._low_streak >= EMERGENCY_CONSECUTIVE_SAMPLES

    async def _emergency_brake(self) -> int:
        """Sleep the longest-idle workspaces until memory recovers.

        One at a time, re-reading memory after each: the goal is the smallest
        intervention that clears the spike, not a purge. Sleeping is reversible
        (the next message restores silently, #572), so erring toward one extra
        is cheap — erring toward twenty is not.

        The cap is **never** touched here. This method can only remove residents.
        """
        self._braking = True
        total, available = self._memory()
        logger.warning(
            "resident-cap: EMERGENCY brake ENGAGED — MemAvailable %.1f%% of total "
            "for %d consecutive samples; sleeping the longest-idle workspaces "
            "ahead of their TTL",
            (available / total * 100) if total else 0.0,
            self._low_streak,
        )

        records = await self._repo.list_all(limit=1000)  # type: ignore[attr-defined]
        victims = select_lru_victims(
            records,
            resident=self._resident(),
            target=0,  # ordering only — the loop below stops as soon as it recovers
            in_flight=self._in_flight(),
        )
        if not victims:
            logger.warning(
                "resident-cap: EMERGENCY brake has nothing it may sleep "
                "(every resident workspace has a running turn or no session row)"
            )
            return 0

        from .workspace_notice import WorkspaceReason

        slept = 0
        for thread_id in victims:
            if await self._sleep_one(thread_id, WorkspaceReason.PRESSURE, None):
                slept += 1
                logger.warning(
                    "%s resident-cap: slept under EMERGENCY memory pressure",
                    log_ctx(thread_id=thread_id),
                )
            total, available = self._memory()
            if total > 0 and available >= total * EMERGENCY_LOW_FRACTION:
                logger.warning(
                    "resident-cap: EMERGENCY brake released after %d workspace(s) — "
                    "MemAvailable back to %.1f%%",
                    slept,
                    available / total * 100,
                )
                self._braking = False
                self._low_streak = 0
                break
        return slept

    async def _enforce_cap(self) -> int:
        resident = self._resident()
        if len(resident) <= self._limit:
            return 0

        records = await self._repo.list_all(limit=1000)  # type: ignore[attr-defined]
        victims = select_lru_victims(
            records,
            resident=resident,
            target=self._limit,
            in_flight=self._in_flight(),
        )
        if not victims:
            # Say so: "over the cap and doing nothing" must not look like "fine".
            logger.info(
                "resident-cap: %d resident > limit %d, but nothing may be slept "
                "(running turns / no session row)",
                len(resident),
                self._limit,
            )
            return 0

        logger.info(
            "resident-cap: %d resident > limit %d — sleeping %d longest-idle workspace(s)",
            len(resident),
            self._limit,
            len(victims),
        )

        from .workspace_notice import WorkspaceReason

        label = f"{self._limit}本"
        slept = 0
        for thread_id in victims:
            if await self._sleep_one(thread_id, WorkspaceReason.CAP, label):
                slept += 1
        if slept:
            logger.info("resident-cap: slept %d workspace(s) to stay under the limit", slept)
        return slept

    async def _sleep_one(self, thread_id: int, reason: object, label: str | None) -> bool:
        """Put one workspace to sleep through the one shared implementation.

        Never a second copy of "sleep a workspace" — #572 owns that, and two
        implementations of one outcome always drift (#538).
        """
        cog = self._bot.get_cog("SessionManageCog")  # type: ignore[attr-defined]
        impl = getattr(cog, "_sleep_workspace_impl", None)
        if impl is None:
            logger.warning("resident-cap: SessionManageCog unavailable — cannot sleep anything")
            return False

        channel = await self._resolve_thread(thread_id)
        if channel is None:
            return False
        try:
            return bool(await impl(channel=channel, reason=reason, idle_label=label))
        except Exception:
            logger.exception("resident-cap: failed to sleep thread=%d", thread_id)
            return False

    async def _resolve_thread(self, thread_id: int) -> object | None:
        """The thread, asking the API when the cache misses (#593)."""
        import discord

        channel = self._bot.get_channel(thread_id)  # type: ignore[attr-defined]
        if channel is not None:
            return channel
        try:
            return await self._bot.fetch_channel(thread_id)  # type: ignore[attr-defined]
        except (discord.NotFound, discord.Forbidden):
            logger.info("resident-cap: thread=%d is not reachable — skipping", thread_id)
            return None
        except Exception as exc:
            logger.info("resident-cap: thread=%d could not be fetched (%s)", thread_id, exc)
            return None
