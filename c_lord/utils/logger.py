"""Logging for c-lord: the root handler and the ``[thread=…]`` context prefix."""

from __future__ import annotations

import logging
import sys

_LINE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: discord.py narrates every gateway event and HTTP call at INFO.
_NOISY_LOGGERS = ("discord", "discord.http")


def setup_logging(level: int = logging.INFO) -> None:
    """Send everything at *level* and above to stdout, one line per record.

    A handler is added to the root logger on every call; handlers that are
    already there are left alone.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LINE_FORMAT, datefmt=_TIME_FORMAT))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def log_ctx(
    *,
    thread_id: int | None = None,
    session_id: str | None = None,
    task_id: int | None = None,
    channel_id: int | None = None,
) -> str:
    """Format a structured context prefix for log messages.

    Returns ``"[thread=<id> session=<short> task=<id> channel=<id>]"`` (only
    fields that are not None are included). Returns ``""`` when all fields
    are None. Session IDs are truncated to the first UUID segment so that long
    UUIDs do not dominate log lines while remaining greppable.

    Use this at process entry/exit points and any log line where a reader needs
    to filter by ``thread_id`` or ``session_id``.
    """
    parts: list[str] = []
    if thread_id is not None:
        parts.append(f"thread={thread_id}")
    if session_id is not None:
        # Truncate UUID-shaped IDs (8-4-4-4-12) to the first segment for readability.
        short = session_id.split("-", 1)[0] if len(session_id) >= 32 else session_id
        parts.append(f"session={short}")
    if task_id is not None:
        parts.append(f"task={task_id}")
    if channel_id is not None:
        parts.append(f"channel={channel_id}")
    if not parts:
        return ""
    return "[" + " ".join(parts) + "]"
