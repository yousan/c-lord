"""Where a running turn learns that Claude is still moving (#769).

The per-turn lamp (:class:`~c_lord.discord_ui.status.StatusManager`) paints
⏳ after 10s and ⚠️ after 30s *without activity*.  Its timer used to be reset
by tool-use / progress events from the tmux runner — producers that no longer
exist (#53/#723).  Nothing reset it during a turn, so it measured time since
the turn *started*, and every turn past 30s read ⚠️ however busy Claude was.

The only component that still sees Claude work is the transcript mirror: every
tool call, tool result, thinking block and message lands in the jsonl it tails.
This module is the hand-off, in the same spirit as
:mod:`c_lord.turn_end_bus`: the mirror :meth:`~TurnActivity.note` s each event,
and the turn's lamp :meth:`~TurnActivity.subscribe` s while the turn runs.

One listener per thread — a thread runs one turn at a time, and the newest
turn is the one whose lamp is live.  Everything runs on the asyncio loop, so no
locking is needed.  A listener that raises is logged and ignored: the lamp is
decoration and must never take the mirror (the delivery path) down with it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

Listener = Callable[[], None]


class TurnActivity:
    """Per-thread registry of the live turn's activity listener."""

    def __init__(self) -> None:
        self._listeners: dict[int, Listener] = {}

    def subscribe(self, thread_id: int, listener: Listener) -> Callable[[], None]:
        """Route *thread_id*'s activity to *listener*; returns the unsubscriber.

        The unsubscriber only removes *this* listener — a turn unwinding after
        the next one has already subscribed must not silence the next one.
        """
        self._listeners[thread_id] = listener

        def unsubscribe() -> None:
            if self._listeners.get(thread_id) is listener:
                del self._listeners[thread_id]

        return unsubscribe

    def note(self, thread_id: int) -> None:
        """The transcript showed *thread_id*'s session doing something."""
        listener = self._listeners.get(thread_id)
        if listener is None:
            return
        try:
            listener()
        except Exception:
            logger.warning("turn_activity listener failed thread=%d", thread_id, exc_info=True)


# Module-level singleton — import this everywhere.
turn_activity = TurnActivity()
