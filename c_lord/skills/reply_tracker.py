"""In-memory record of the last answer message posted to each thread.

Lets post-turn helpers append to the same message the answer just landed in, so
context-usage and similar metadata stay inside that bubble rather than creating
fresh ones. Written by both delivery-adjacent writers (the JSONL transcript
mirror and ``POST /api/reply``) and read in the same bot process, so plain
module-level dicts are sufficient.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

_last_reply_message: dict[int, discord.Message] = {}


def record_reply_message(thread_id: int, message: discord.Message) -> None:
    """Record the last Discord ``Message`` an answer was delivered in.

    When a reply is split into multiple chunks, only the *last* chunk is
    recorded — that is where post-turn helpers (e.g. the context-usage line)
    should append so the addendum sits at the bottom of the answer.
    """
    _last_reply_message[thread_id] = message


def get_last_reply_message(thread_id: int) -> discord.Message | None:
    """Return the last recorded reply message for ``thread_id``, or ``None``."""
    return _last_reply_message.get(thread_id)


def reset_tracker() -> None:
    """Clear all recorded replies. For tests only."""
    _last_reply_message.clear()
