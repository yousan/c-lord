"""In-memory tracker recording when ``discord-reply`` was last invoked.

Used to detect turns where the Claude session finished without calling the
skill (issue #67). The api_server records the timestamp on every successful
``POST /api/reply``; the run helper checks the tracker at the end of a turn
and surfaces a fallback notification to the Discord thread if the skill was
never called.

Process-scoped state — both writer (api_server) and reader (run_helper)
live in the same bot process, so a plain module-level dict is sufficient.
"""

from __future__ import annotations

import time

_last_reply_at: dict[int, float] = {}


def record_reply(thread_id: int) -> None:
    """Record that ``discord-reply`` was just invoked for ``thread_id``."""
    _last_reply_at[thread_id] = time.monotonic()


def was_replied_since(thread_id: int, since: float) -> bool:
    """Return True if a reply was recorded for ``thread_id`` at or after ``since``."""
    last = _last_reply_at.get(thread_id)
    return last is not None and last >= since


def reset_tracker() -> None:
    """Clear all recorded replies. For tests only."""
    _last_reply_at.clear()
