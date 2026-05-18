"""Async follower for a project's active Claude Code transcript jsonl.

Yields each parsed JSON event in the order it was appended.  The active jsonl
is re-selected on every poll as the mtime-latest ``*.jsonl`` under the project
directory, so ``/clear`` (which causes Claude Code to start a new
``<session-id>.jsonl``) is followed without restarting the tail.  Truncation
or rewrite of the current file is handled by resetting the read offset to 0.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .resolver import latest_session_jsonl


async def tail_events(
    project_dir: Path,
    *,
    poll_interval: float = 0.5,
    from_start: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Yield JSONL events as they appear under ``project_dir``.

    Parameters
    ----------
    project_dir
        Output of :func:`c_lord.transcript.resolver.derive_project_dir`.
    poll_interval
        Seconds between filesystem checks.  Small values keep latency low at
        the cost of more ``stat`` calls; ``0.5`` is a reasonable default.
    from_start
        When ``True``, replay the current file from byte 0 before following.
        Useful for catching up to an existing session.  Default ``False`` —
        only newly appended lines are yielded.
    """
    # Snapshot the initial state of the project dir: ``from_start=False`` only
    # skips bytes that *already existed* when tail was first called.  Any file
    # that appears later (including a /clear-induced new session) is read from
    # byte 0 so we don't miss events that landed before we noticed.
    initial_path = latest_session_jsonl(project_dir)
    initial_offset = (
        initial_path.stat().st_size if initial_path is not None and not from_start else 0
    )

    current_path: Path | None = None
    offset = 0
    buffer = ""
    last_mtime = -1.0

    while True:
        active = latest_session_jsonl(project_dir)
        if active is None:
            await asyncio.sleep(poll_interval)
            continue

        if active != current_path:
            offset = initial_offset if initial_path is not None and active == initial_path else 0
            current_path = active
            buffer = ""
            last_mtime = -1.0

        try:
            stat = active.stat()
        except FileNotFoundError:
            current_path = None
            await asyncio.sleep(poll_interval)
            continue
        size = stat.st_size
        mtime = stat.st_mtime

        if size < offset:
            # Truncation — reset and re-read from start.
            offset = 0
            buffer = ""
        elif size == offset and mtime > last_mtime > 0:
            # In-place rewrite with identical size: rare, but recover by
            # restarting the read from byte 0.
            offset = 0
            buffer = ""

        last_mtime = mtime

        if size > offset:
            with active.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
            buffer += chunk
            lines = buffer.split("\n")
            buffer = lines[-1]  # last fragment may be a partial line
            for line in lines[:-1]:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

        await asyncio.sleep(poll_interval)
