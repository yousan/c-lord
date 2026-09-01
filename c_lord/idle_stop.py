"""7日アイドルで自動停止する — Issue #574, design agreed in #540.

**Why seven days.** Measured across 2650 human turns in the production
transcripts, the gap before someone comes back to a thread is p95 = 2.42h and
**only 0.49% of returns exceed 7 days** — one in two hundred. Stopping loses
nothing (the working copy, the conversation and the volumes all survive; the
docker stack just needs starting again), so being wrong is nearly free. Waiting
30 days instead buys 1.8 percentage points and costs a month of held host ports.

**How idle is measured.** Not from tmux — see :mod:`c_lord.idle_sweep`, which
owns the selection and the loop machinery this shares with the 4-hour sleep
(#572). Only the "what happens to one workspace" half lives here.
"""

from __future__ import annotations

import datetime
import logging
import os
from collections.abc import Iterable
from typing import TYPE_CHECKING

from .idle_sweep import IdleSweepLoop
from .idle_sweep import select_idle_workspaces as _select

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


def select_idle_workspaces(
    records: Iterable[SessionRecord],
    *,
    now: datetime.datetime,
    threshold_days: float,
    in_flight: set[int] | None = None,
) -> list[int]:
    """Day-flavoured spelling of :func:`c_lord.idle_sweep.select_idle_workspaces`.

    The rules live there — this only says what a day is, so callers that think
    in days do not have to build a ``timedelta`` at every call site.
    """
    return _select(
        records,
        now=now,
        threshold=datetime.timedelta(days=threshold_days),
        in_flight=in_flight,
    )


#: Most workspaces stopped in a single sweep.
#:
#: Effectively unlimited. It started at 5 to avoid a burst of notices, but that
#: traded a real cost for an imagined one: everything selected has been idle for
#: at least a week, and drip-feeding five per ten minutes stretched a 93-item
#: backlog across three hours while the sidebar stayed cluttered the whole time.
#: yousan asked for them to go at once (2026-08-31). The ceiling stays as a guard
#: against a pathological table, not as a pacing mechanism.
MAX_STOPS_PER_TICK = 1000

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


class IdleStopLoop(IdleSweepLoop):
    """Stops workspaces nobody has touched for a week.

    Calls the very function ``/workspace-stop`` calls, passing
    :attr:`WorkspaceReason.IDLE`. Writing a second "stop a workspace" path here
    would be the #538 failure again — two implementations of the same outcome
    always drift — so this class only decides *which* and *when*, never *how*.
    """

    name = "idle-stop"

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
        self._threshold_days = threshold_days if threshold_days is not None else idle_stop_days()
        super().__init__(
            bot,
            session_repo,
            threshold=datetime.timedelta(days=self._threshold_days),
            interval_seconds=interval_seconds,
            now_fn=now_fn,
            in_flight_fn=in_flight_fn,
            max_per_tick=max_per_tick,
        )

    def _threshold_label(self) -> str:
        return f"{self._threshold_days}d"

    async def _act(self, thread_id: int, channel: object) -> bool:
        cog = self._bot.get_cog("SessionManageCog")  # type: ignore[attr-defined]
        impl = getattr(cog, "_close_workspace_impl", None)
        if impl is None:
            logger.warning(
                "idle-stop: SessionManageCog unavailable — skipping thread=%d", thread_id
            )
            return False

        from .workspace_notice import WorkspaceReason

        await impl(
            channel=channel,
            respond=_thread_responder(channel),
            ack=_noop_ack,
            reason=WorkspaceReason.IDLE,
            idle_label=idle_label_for(self._threshold_days),
        )
        return True

    def _log_summary(self, acted: int) -> None:
        logger.info(
            "idle-stop: stopped %d workspace(s) idle for more than %d day(s)",
            acted,
            self._threshold_days,
        )
