"""Async follower for a project's active Claude Code transcript jsonl.

Yields each parsed JSON event in the order it was appended.  The active jsonl
is re-selected on every poll as the mtime-latest ``*.jsonl`` under the project
directory, so ``/clear`` (which causes Claude Code to start a new
``<session-id>.jsonl``) is followed without restarting the tail.  Truncation
or rewrite of the current file is handled by resetting the read offset to 0.

Re-reading from byte 0 (truncation / in-place rewrite / new active file) must
never re-emit an event that was already yielded — otherwise a ``--resume`` that
rewrites the active jsonl in place (preserving history) would flood the consumer
with the whole transcript again (Issue #433).  Each event is therefore yielded
at most once per follow session, deduplicated by its stable ``uuid``.  At start
(``from_start=False``) the uuids already present in the file are seeded so the
pre-existing history is treated as already-delivered and never replayed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .resolver import latest_session_jsonl


def _seed_seen_uuids(path: Path, upto_offset: int, seen: set[str]) -> None:
    """Record the uuids of events in ``path[:upto_offset]`` into ``seen``.

    Reads exactly ``upto_offset`` bytes (the same baseline the follow loop skips
    past) so events that existed when the tail started are marked already-seen
    and are never re-emitted on a later offset-0 reset.  A partial trailing line
    (offset cutting mid-line) fails to parse and is ignored — it is read afresh
    by the follow loop from ``upto_offset``.
    """
    try:
        with path.open("rb") as f:
            data = f.read(upto_offset)
    except OSError:
        return
    for line in data.decode("utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        uid = event.get("uuid")
        if isinstance(uid, str) and uid:
            seen.add(uid)


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

    # Issue #433: events already yielded (or present at start) must not be
    # re-emitted when the read offset is reset to 0 (truncation / in-place
    # rewrite / new active file).  Dedup by stable ``uuid``; events without a
    # uuid are non-rendered metadata (mode/permission-mode/file-history-snapshot/
    # …) that the consumer drops, so re-emitting them on a reset is harmless and
    # they are intentionally not deduplicated (avoids collapsing two distinct
    # uuid-less records that happen to serialise identically).
    seen_uuids: set[str] = set()
    if initial_path is not None and not from_start:
        _seed_seen_uuids(initial_path, initial_offset, seen_uuids)

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
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                uid = event.get("uuid")
                if isinstance(uid, str) and uid:
                    if uid in seen_uuids:
                        continue  # already emitted — do not replay on offset reset
                    seen_uuids.add(uid)
                yield event

        await asyncio.sleep(poll_interval)
