"""4時間アイドルで自動スリープする — Issue #572, design agreed in #540.

**なぜスリープが要るのか。** 常駐 ``claude`` はターンが終わっても居座り続ける。
リークではない — **片付けないだけ**（本番実測: 80時間経過した9本の RSS ドリフト
は 0.0〜+7.5 MB/h でほぼ完全にフラット）。だから TTL で回収すれば確実に効く。
実トランスクリプト272スレッドを15分刻みで再現すると、TTL 無しの現状は p95 で
**249本・99.6 GB** を要求する。31〜47 GiB のホストがそれを支えられるはずがなく、
いま落ちていないのは claude が勝手に死んでいるから (#570) にすぎない。

**なぜ4時間か。** 人間が同じスレッドに戻ってくるまでの間隔 (n=2650) は
p95 = 2.42h。4時間なら**影響を受ける復帰は 3.89%**（26回に1回）で、その1回も
次の投稿で無言復元するので利用者には見えない。TTL 4h の p95 常駐は 13本・5.2 GB。

**なぜ手動コマンドが無いのか。** スリープのゴールは「気づかれないこと」。手で
打てるようにすると「停止とどう違う?」という説明の要る概念が1つ増える。3操作
（スリープ ⊂ 停止 ⊂ 削除）のうち、利用者が名前で呼ぶ必要があるのは下2つだけ。

あるべき動きは ``docs/specs/workspace-sleep.md``。
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import TYPE_CHECKING

from .idle_sweep import IdleSweepLoop

if TYPE_CHECKING:
    from .database.repository import SessionRecord

logger = logging.getLogger(__name__)

__all__ = [
    "IDLE_SLEEP_HOURS_DEFAULT",
    "MAX_SLEEPS_PER_TICK",
    "IdleSleepLoop",
    "idle_label_for_hours",
    "idle_sleep_hours",
]

#: Hours of no human activity before a workspace's Claude is stopped.
#:
#: A constant, not something derived from the host — same reasoning as
#: :data:`c_lord.idle_stop.IDLE_STOP_DAYS_DEFAULT`. The number comes from the
#: distribution of *return gaps between human turns*, which does not change with
#: RAM, so it is safe to ship as a zero-config default to a 2 GiB VPS (#540).
IDLE_SLEEP_HOURS_DEFAULT = 4

_ENV_VAR = "CLORD_IDLE_SLEEP_HOURS"

#: Guard against a pathological session table, not a pacing mechanism. Sleeping
#: posts nothing, so there is no burst of notices to spread out (#607) — the only
#: reason to bound a tick is to keep one sweep from running unboundedly long.
MAX_SLEEPS_PER_TICK = 1000

#: Every 10 minutes. The threshold is hours, so this is already 24× finer than
#: it needs to be, and each tick reads the whole session table once.
_TICK_INTERVAL_SECONDS = 600.0


def idle_sleep_hours() -> float:
    """Threshold in hours. ``0`` disables automatic sleeping entirely.

    Fractional values are accepted so an operator (and a staging run) can use a
    span shorter than an hour. A malformed value falls back to the default
    rather than disabling the feature: a typo in ``.env`` must not silently turn
    off memory reclamation on a host that needs it, nor crash the bot.
    """
    raw = os.getenv(_ENV_VAR, "").strip()
    if not raw:
        return IDLE_SLEEP_HOURS_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number — using %d", _ENV_VAR, raw, IDLE_SLEEP_HOURS_DEFAULT)
        return IDLE_SLEEP_HOURS_DEFAULT
    if value < 0:
        logger.warning("%s=%r is negative — using %d", _ENV_VAR, raw, IDLE_SLEEP_HOURS_DEFAULT)
        return IDLE_SLEEP_HOURS_DEFAULT
    return value


def idle_label_for_hours(hours: float) -> str:
    """The span as it appears in the notice ("4時間"), without a stray ``.0``."""
    return f"{hours:g}時間"


class IdleSleepLoop(IdleSweepLoop):
    """Puts the Claude of untouched workspaces to sleep.

    Calls ``SessionManageCog._sleep_workspace_impl`` — the one implementation of
    "sleep a workspace", which #576's resident cap will also call when it evicts
    by LRU. A second copy of the operation here would be the #538 failure again.
    """

    name = "idle-sleep"

    def __init__(
        self,
        bot: object,
        session_repo: object,
        *,
        threshold_hours: float | None = None,
        interval_seconds: float = _TICK_INTERVAL_SECONDS,
        now_fn: object | None = None,
        in_flight_fn: object | None = None,
        max_per_tick: int = MAX_SLEEPS_PER_TICK,
    ) -> None:
        self._hours = threshold_hours if threshold_hours is not None else idle_sleep_hours()
        super().__init__(
            bot,
            session_repo,
            threshold=datetime.timedelta(hours=self._hours),
            interval_seconds=interval_seconds,
            now_fn=now_fn,
            in_flight_fn=in_flight_fn,
            max_per_tick=max_per_tick,
        )
        # thread_id -> the ``last_used_at`` this loop already acted on. See
        # :meth:`_needs_action`.
        self._visited: dict[int, str] = {}

    def _threshold_label(self) -> str:
        return idle_label_for_hours(self._hours)

    # ── which candidates are worth a Discord round-trip ───────────────────

    def _needs_action(self, record: SessionRecord) -> bool:
        """Skip a workspace this loop has already handled at this timestamp.

        Unlike the 7-day stop, a slept workspace stays *selectable*: sleeping
        writes no ``closed_at``, so the row keeps matching the threshold until
        the 7-day stop finally closes it — for up to a week. Without this filter
        every tick would re-resolve and re-probe the same hundreds of threads,
        spending a ``fetch_channel`` on each (most of them archived, so the
        cache never answers) to discover there is nothing left to kill.

        Keying on ``last_used_at`` rather than a "we slept it" flag makes it
        exact and self-healing: the entry stops matching the moment a human
        speaks again, and a workspace whose pane died on its own is visited once
        and then left alone too. The map is bounded by the session table and is
        rebuilt from scratch after a restart, which costs exactly one extra
        sweep.
        """
        return self._visited.get(record.thread_id) != record.last_used_at

    def _mark_visited(self, record: SessionRecord) -> None:
        self._visited[record.thread_id] = record.last_used_at

    # ── the operation ────────────────────────────────────────────────────

    async def _act(self, thread_id: int, channel: object) -> bool:
        cog = self._bot.get_cog("SessionManageCog")  # type: ignore[attr-defined]
        impl = getattr(cog, "_sleep_workspace_impl", None)
        if impl is None:
            logger.warning(
                "idle-sleep: SessionManageCog unavailable — skipping thread=%d", thread_id
            )
            return False

        from .workspace_notice import WorkspaceReason

        return bool(
            await impl(
                channel=channel,
                reason=WorkspaceReason.IDLE,
                idle_label=idle_label_for_hours(self._hours),
            )
        )

    def _log_summary(self, acted: int) -> None:
        # Report the *output* side. A sweep that only counts its candidates
        # reads as working while it stops nothing at all (#604).
        logger.info(
            "idle-sleep: slept %d workspace(s) idle for more than %s",
            acted,
            idle_label_for_hours(self._hours),
        )
