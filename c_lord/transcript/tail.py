"""Async follower for a project's active Claude Code transcript jsonl.

Yields each parsed JSON event in the order it was appended.  Which jsonl is
followed is decided by :class:`~c_lord.transcript.resolver.ThreadSessionResolver`
— the newest transcript **this thread's own Claude session** wrote, so ``/clear``
(which starts a new ``<session-id>.jsonl``) is followed without restarting the
tail, while a ``claude -p`` sub-invocation sharing the working copy is not
(Issue #627).  Truncation or rewrite of the current file is handled by resetting
the read offset to 0.

Re-reading from byte 0 (truncation / in-place rewrite / new active file) must
never re-emit an event that was already yielded — otherwise a ``--resume`` that
rewrites the active jsonl in place (preserving history) would flood the consumer
with the whole transcript again (Issue #433).  Each event is therefore yielded
at most once per follow session, deduplicated by its stable ``uuid``.  At start
(``from_start=False``) the uuids already present in the file are seeded so the
pre-existing history is treated as already-delivered and never replayed.

**Nothing in here may run on the asyncio event loop** (Issue #537).  Every
filesystem call and every ``json.loads`` happens in :func:`_tail_executor`, a
pool of this module's own.  The first round of #537 only moved the *startup*
seeding off the loop and left the follow loop below untouched; production runs
252-255 of these loops at 2 Hz, and their combined ``glob`` / ``stat`` /
``read`` / ``parse`` starved the Discord gateway heartbeat until the shard
reconnected — dropping, for good, every message sent while it was disconnected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .resolver import ThreadSessionResolver

logger = logging.getLogger(__name__)

# Read the seeding baseline in bounded slices instead of one ``read(offset)``:
# a production transcript reaches ~100 MB and the seeding of every session now
# runs concurrently in worker threads (Issue #537), so slurping whole files
# would spike memory by the sum of every active transcript.
_SEED_CHUNK_BYTES = 1 << 20

# Most bytes one poll cycle may read.  A file switch (or a tail that fell
# behind) otherwise reads the whole transcript in a single call: measured on the
# production host, the 109 MB transcript costs 2.07 s to read and parse, and
# holding the decoded text plus its split lines in memory at once is the same
# spike the seeding chunk size above exists to avoid.  Above the cap the cycle
# repeats immediately instead of sleeping, so catching up is not slowed down.
_READ_CHUNK_BYTES = 8 << 20

# Worker pool for the follow loops.  Deliberately **not** ``asyncio.to_thread``,
# which shares one bounded pool with the rest of c-lord (git checkouts, the #215
# recovery scans, ``_transcript_has_ask_result``): 250 mirrors polling twice a
# second would queue in front of that work.  Bounded, because one thread per
# mirror is 250 threads.
_TAIL_EXECUTOR: ThreadPoolExecutor | None = None
_DEFAULT_TAIL_WORKERS = 8


def _tail_worker_count() -> int:
    """Worker threads for the tail pool (``CLORD_TAIL_WORKERS``, default 8)."""
    raw = os.getenv("CLORD_TAIL_WORKERS", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            logger.warning("CLORD_TAIL_WORKERS=%r is not an integer — using default", raw)
        else:
            if value >= 1:
                return min(value, 32)
            logger.warning("CLORD_TAIL_WORKERS=%r must be >= 1 — using default", raw)
    return _DEFAULT_TAIL_WORKERS


def _tail_executor() -> ThreadPoolExecutor:
    """The shared, lazily created pool every tail poll runs in (Issue #537)."""
    global _TAIL_EXECUTOR
    if _TAIL_EXECUTOR is None:
        _TAIL_EXECUTOR = ThreadPoolExecutor(
            max_workers=_tail_worker_count(),
            thread_name_prefix="clord-tail",
        )
    return _TAIL_EXECUTOR


async def _run_off_loop(fn, /, *args):
    """Await ``fn(*args)`` in the tail pool.

    The tail's own :func:`asyncio.to_thread` equivalent — see
    :data:`_TAIL_EXECUTOR` for why it is not ``to_thread`` itself.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_tail_executor(), fn, *args)


def _record_uuid(line: bytes, seen: set[str]) -> None:
    """Add the ``uuid`` of one raw JSONL line to ``seen``, if it has one."""
    if not line.strip():
        return
    try:
        event = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return
    uid = event.get("uuid")
    if isinstance(uid, str) and uid:
        seen.add(uid)


def _seed_seen_uuids(path: Path, upto_offset: int, seen: set[str]) -> None:
    """Record the uuids of events in ``path[:upto_offset]`` into ``seen``.

    Reads exactly ``upto_offset`` bytes (the same baseline the follow loop skips
    past) so events that existed when the tail started are marked already-seen
    and are never re-emitted on a later offset-0 reset.  A partial trailing line
    (offset cutting mid-line) fails to parse and is ignored — it is read afresh
    by the follow loop from ``upto_offset``.

    Blocking: parses every pre-existing line.  Callers on the event loop must
    hand it to a worker thread (Issue #537) — :func:`tail_events` does.
    """
    remaining = upto_offset
    buffer = b""
    try:
        with path.open("rb") as f:
            while remaining > 0:
                chunk = f.read(min(_SEED_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                lines = (buffer + chunk).split(b"\n")
                buffer = lines[-1]  # last piece may be a partial line
                for line in lines[:-1]:
                    _record_uuid(line, seen)
    except OSError:
        return
    _record_uuid(buffer, seen)


@dataclass
class _FollowState:
    """Everything one follow loop carries between polls.

    Lives on the event loop thread and is handed to the worker for the duration
    of a single :func:`_poll_once` call.  Only one poll per tail is ever in
    flight (the generator awaits it), so no locking is needed.
    """

    project_dir: Path
    resolver: ThreadSessionResolver
    from_start: bool = False
    # Size of each file that already existed when the tail started.  Everything
    # below that mark predates our watch, so if one of these is adopted later
    # (#627: a transcript becomes eligible the moment c-lord drives a turn into
    # it) reading starts at the mark — the history that was on disk before we
    # were looking is never replayed, and the turn that made it eligible is.
    pre_existing: dict[Path, int] = field(default_factory=dict)
    current_path: Path | None = None
    offset: int = 0
    buffer: str = ""
    last_mtime: float = -1.0
    # Read position per file, so switching back and forth never re-reads (and
    # the consumer never re-receives) a transcript already consumed (#627 AC3).
    offsets: dict[Path, int] = field(default_factory=dict)
    # Set when a poll stopped at the read cap: the caller repeats immediately
    # rather than sleeping, so falling behind is not also made slow.
    more_to_read: bool = False
    seen_uuids: set[str] = field(default_factory=set)


def _open_initial_state(project_dir: Path, from_start: bool) -> _FollowState:
    """Snapshot the project dir as it was when the tail started.

    Blocking (``glob`` + ``stat`` + the eligibility probe): runs in the tail
    pool.  ``from_start=False`` only skips bytes that *already existed* at that
    moment — a file that appears later (a ``/clear``-induced new session) is
    read from byte 0 so events that landed before we noticed are not missed.
    """
    resolver = ThreadSessionResolver(project_dir)
    state = _FollowState(project_dir=project_dir, resolver=resolver, from_start=from_start)
    try:
        state.pre_existing = {p: _size_of(p) for p in project_dir.glob("*.jsonl") if p.is_file()}
    except OSError:
        state.pre_existing = {}

    initial_path = resolver.resolve()
    if initial_path is not None and not from_start:
        # Issue #537: a transcript can be ~100 MB, and every mirror seeds one at
        # bot startup. Parsing that on the event loop stalled the Discord
        # gateway heartbeat long enough to force a reconnect, during which every
        # message the user sent was dropped by Discord and never redelivered.
        state.offsets[initial_path] = _size_of(initial_path)
        _seed_seen_uuids(initial_path, state.offsets[initial_path], state.seen_uuids)
    return state


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _start_offset(state: _FollowState, path: Path) -> int:
    """Where to (re)start reading ``path``, and seed uuids when adopting it late.

    - already read in this follow session → resume exactly where we stopped;
    - existed before the tail started → resume at **the size it had then**, not
      its size now: the history on disk before we were watching was never ours
      to deliver, but the turn that just made it eligible is (#627 AC3).  This
      is the "a transcript becomes eligible only once c-lord drives a turn into
      it" case;
    - created while we were watching → genuinely new content, read from 0.
    """
    known = state.offsets.get(path)
    if known is not None:
        return known
    baseline = state.pre_existing.get(path)
    if baseline is not None and not state.from_start:
        _seed_seen_uuids(path, baseline, state.seen_uuids)
        return baseline
    return 0


def _poll_once(state: _FollowState) -> list[dict[str, Any]]:
    """One follow-loop cycle: resolve, stat, read, parse.  Returns new events.

    Blocking — every syscall and every ``json.loads`` of the follow loop is in
    here precisely so the caller can run the whole cycle in one worker-thread
    hop (Issue #537).  Mutates ``state`` in place; the caller must not touch it
    while this is running.
    """
    state.more_to_read = False

    active = state.resolver.resolve()
    if active is None:
        return []

    if active != state.current_path:
        if state.current_path is not None:
            state.offsets[state.current_path] = state.offset
        state.offset = _start_offset(state, active)
        state.current_path = active
        state.buffer = ""
        state.last_mtime = -1.0

    try:
        stat = active.stat()
    except FileNotFoundError:
        state.current_path = None
        return []
    size = stat.st_size
    mtime = stat.st_mtime

    if size < state.offset:
        # Truncation — reset and re-read from start.
        state.offset = 0
        state.buffer = ""
    elif size == state.offset and mtime > state.last_mtime > 0:
        # In-place rewrite with identical size: rare, but recover by
        # restarting the read from byte 0.
        state.offset = 0
        state.buffer = ""

    state.last_mtime = mtime

    if size <= state.offset:
        return []

    try:
        with active.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(state.offset)
            chunk = f.read(_READ_CHUNK_BYTES)
            state.offset = f.tell()
    except OSError:
        state.current_path = None
        return []

    state.more_to_read = state.offset < size

    state.buffer += chunk
    lines = state.buffer.split("\n")
    state.buffer = lines[-1]  # last fragment may be a partial line

    events: list[dict[str, Any]] = []
    for line in lines[:-1]:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        uid = event.get("uuid")
        if isinstance(uid, str) and uid:
            if uid in state.seen_uuids:
                continue  # already emitted — do not replay on offset reset
            state.seen_uuids.add(uid)
        events.append(event)
    return events


async def tail_events(
    project_dir: Path,
    *,
    poll_interval: float = 0.5,
    from_start: bool = False,
) -> AsyncGenerator[dict[str, Any], None]:
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
    # Issue #433: events already yielded (or present at start) must not be
    # re-emitted when the read offset is reset to 0 (truncation / in-place
    # rewrite / new active file).  Dedup by stable ``uuid``; events without a
    # uuid are non-rendered metadata (mode/permission-mode/file-history-snapshot/
    # …) that the consumer drops, so re-emitting them on a reset is harmless and
    # they are intentionally not deduplicated (avoids collapsing two distinct
    # uuid-less records that happen to serialise identically).
    state = await _run_off_loop(_open_initial_state, project_dir, from_start)

    while True:
        # The whole cycle — glob, stat, read, parse — in one worker-thread hop.
        # Splitting it would put the loop back in the middle of the syscalls.
        for event in await _run_off_loop(_poll_once, state):
            yield event

        if state.more_to_read:
            # Catching up on a backlog: go straight round again rather than
            # trickling one capped read per poll interval.
            continue
        await asyncio.sleep(poll_interval)
