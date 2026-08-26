"""Recover the final answer of the last completed turn from a JSONL transcript.

Issue #215: when the bot is killed mid/post-turn, the final ``assistant_text``
is written to the JSONL while the mirror is not tailing.  On restart the mirror
resumes at end-of-file (``from_start=False``) and skips that line, so the user
never receives the answer.  :func:`last_completed_final_answer` lets the mirror
cog detect such an undelivered answer on startup and re-deliver it once,
deduplicated by the assistant event ``uuid``.

Issue #537: this scan reads and parses a whole transcript (up to ~100 MB on the
production host) and startup runs it once per session row.  It must therefore
never touch the event loop — the cog awaits
:func:`final_answer_needs_recovery_async`, which hands the work to a worker
thread — and it must not materialise the transcript in memory: the scan keeps
only the current candidate answer, not the parsed events.

Issue #553: "is this answer the stored cursor" is the wrong question — the
cursor can sit *past* the last completed turn's answer, and comparing the two
for equality then reported a delivered answer as dropped and re-posted it.
:func:`final_answer_needs_recovery` asks the ordering question instead, in the
same single streaming pass.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from .formatter import render_event
from .resolver import latest_session_jsonl


@dataclass(frozen=True)
class FinalAnswer:
    """The final assistant text of a completed turn, with its event uuid."""

    uuid: str
    text: str


def _is_turn_end(event: dict) -> bool:
    """True for JSONL events that mark the end of a Claude turn.

    Mirrors :func:`c_lord.transcript.mirror._is_turn_end` — both the legacy
    ``{"type": "result"}`` and current ``{"type": "system",
    "subtype": "turn_duration"}`` shapes count.
    """
    t = event.get("type")
    return t == "result" or (t == "system" and event.get("subtype") == "turn_duration")


def _as_final_answer(event: dict) -> FinalAnswer | None:
    """Return the plain-text answer ``event`` renders as, or ``None``.

    ``None`` for anything the mirror would not post as a reply — tool-use
    events, empty bodies, and events without a stable ``uuid`` to dedup on.
    """
    if event.get("type") != "assistant":
        return None
    uuid = event.get("uuid")
    if not uuid:
        return None
    rendered = render_event(event)
    if rendered is None or rendered.kind != "assistant_text" or not rendered.body:
        return None
    return FinalAnswer(uuid=uuid, text=rendered.body)


def last_completed_final_answer(project_dir: Path) -> FinalAnswer | None:
    """Return the final answer of the last completed turn, or ``None``.

    The "final answer" is the last assistant event that renders as plain
    ``assistant_text`` (the rendering the mirror would post as the reply) at or
    before the last turn-end marker in the most recent ``*.jsonl``.  Returns
    ``None`` when there is no completed turn carrying a final text answer.

    Blocking: reads and parses the whole file.  Callers on the event loop must
    use :func:`last_completed_final_answer_async` (Issue #537).
    """
    jsonl = latest_session_jsonl(project_dir)
    if jsonl is None:
        return None

    # Streamed in one pass with O(1) memory: ``candidate`` is the newest answer
    # seen so far and ``answer`` is the snapshot taken at the newest turn end,
    # which is exactly "the last answer at or before the last turn-end marker".
    candidate: FinalAnswer | None = None
    answer: FinalAnswer | None = None
    try:
        with jsonl.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _is_turn_end(event):
                    answer = candidate
                    continue
                found = _as_final_answer(event)
                if found is not None:
                    candidate = found
    except OSError:
        return None
    return answer


async def last_completed_final_answer_async(project_dir: Path) -> FinalAnswer | None:
    """Await :func:`last_completed_final_answer` off the event loop (Issue #537).

    Startup scans one transcript per session row; doing that inline stalled the
    loop long enough for the Discord gateway heartbeat to be missed, and every
    message sent during the resulting reconnect was lost.
    """
    return await asyncio.to_thread(last_completed_final_answer, project_dir)


def final_answer_needs_recovery(project_dir: Path, cursor_uuid: str | None) -> FinalAnswer | None:
    """Return the final answer that still has to be delivered, or ``None`` (#553).

    ``cursor_uuid`` is the last uuid the mirror recorded as **delivered as a
    final answer** (``sessions.mirror_replied_uuid``).

    The question is not "is the cursor equal to this answer" but **"has the
    cursor already passed it"**.  Equality was the #553 bug: a turn still running
    when the bot went down left the cursor on a *later* line than the last
    completed turn's final answer, so the two differed, and the #215 rescue
    re-posted an answer the user had already read.

    Rules, in order:

    - cursor at or after the answer → already delivered, return ``None``.  This
      is the #553 fix: the cursor being *later* is precisely the mid-turn
      shutdown case that used to be misread as a drop;
    - cursor before the answer → the answer was written after the last delivery,
      so nothing posted it: return it (the #215 rescue);
    - cursor is not in this transcript at all → return it too.  For the cursor to
      be missing here, no delivery from *this* file was ever committed — which is
      what a mirror that was down for the whole file looks like (a ``/clear``
      starting a fresh transcript, say).  Had the mirror been up and posted, it
      would have committed a cursor into this same file;
    - no cursor at all → return the answer; the caller seeds the cursor instead
      of posting (see the cog).

    Same single streaming pass and O(1) memory as
    :func:`last_completed_final_answer` (#537): positions are line counters, not
    stored events.

    Blocking: callers on the event loop must use
    :func:`final_answer_needs_recovery_async`.
    """
    jsonl = latest_session_jsonl(project_dir)
    if jsonl is None:
        return None

    candidate: FinalAnswer | None = None
    candidate_idx = -1
    answer: FinalAnswer | None = None
    answer_idx = -1
    cursor_idx = -1
    try:
        with jsonl.open("r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cursor_uuid is not None and cursor_idx < 0 and event.get("uuid") == cursor_uuid:
                    cursor_idx = idx
                if _is_turn_end(event):
                    answer, answer_idx = candidate, candidate_idx
                    continue
                found = _as_final_answer(event)
                if found is not None:
                    candidate, candidate_idx = found, idx
    except OSError:
        return None

    if answer is None:
        return None
    if cursor_uuid is None:
        return answer
    if cursor_idx >= 0 and cursor_idx >= answer_idx:
        return None  # the cursor has already passed this answer (#553)
    return answer


async def final_answer_needs_recovery_async(
    project_dir: Path, cursor_uuid: str | None
) -> FinalAnswer | None:
    """Await :func:`final_answer_needs_recovery` off the event loop (#537)."""
    return await asyncio.to_thread(final_answer_needs_recovery, project_dir, cursor_uuid)
