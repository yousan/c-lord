"""In-memory tracker recording when ``discord-reply`` was last invoked.

Used to detect turns where the Claude session finished without calling the
skill (issue #67), and to let post-turn helpers append to the same message
that Claude just sent (so context-usage and similar metadata stay inside the
same bubble rather than creating fresh ones).

Process-scoped state — both writer (api_server) and reader (run_helper)
live in the same bot process, so plain module-level dicts are sufficient.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

_last_reply_at: dict[int, float] = {}
_last_reply_message: dict[int, discord.Message] = {}


def record_reply(thread_id: int) -> None:
    """Record that ``discord-reply`` was just invoked for ``thread_id``."""
    _last_reply_at[thread_id] = time.monotonic()


def record_reply_message(thread_id: int, message: discord.Message) -> None:
    """Record the last Discord ``Message`` posted by ``/api/reply``.

    When a reply is split into multiple chunks, only the *last* chunk is
    recorded — that is where post-turn helpers (e.g. the context-usage line)
    should append so the addendum sits at the bottom of the answer.
    """
    _last_reply_message[thread_id] = message


def get_last_reply_message(thread_id: int) -> discord.Message | None:
    """Return the last recorded reply message for ``thread_id``, or ``None``."""
    return _last_reply_message.get(thread_id)


def was_replied_since(thread_id: int, since: float) -> bool:
    """Return True if a reply was recorded for ``thread_id`` at or after ``since``."""
    last = _last_reply_at.get(thread_id)
    return last is not None and last >= since


def reset_tracker() -> None:
    """Clear all recorded replies. For tests only."""
    _last_reply_at.clear()
    _last_reply_message.clear()
