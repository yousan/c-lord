"""Recover the final answer of the last completed turn from a JSONL transcript.

Issue #215: when the bot is killed mid/post-turn, the final ``assistant_text``
is written to the JSONL while the mirror is not tailing.  On restart the mirror
resumes at end-of-file (``from_start=False``) and skips that line, so the user
never receives the answer.  :func:`last_completed_final_answer` lets the mirror
cog detect such an undelivered answer on startup and re-deliver it once,
deduplicated by the assistant event ``uuid``.
"""

from __future__ import annotations

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


def _load_events(project_dir: Path) -> list[dict]:
    """Parse the most recent session JSONL into events (empty on any failure)."""
    jsonl = latest_session_jsonl(project_dir)
    if jsonl is None:
        return []
    events: list[dict] = []
    try:
        with jsonl.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return events


def _final_answer_at(events: list[dict]) -> tuple[FinalAnswer, int] | None:
    """Return the last completed turn's final answer and its index in *events*."""
    last_end = -1
    for i, ev in enumerate(events):
        if _is_turn_end(ev):
            last_end = i
    if last_end < 0:
        return None

    for i in range(last_end, -1, -1):
        ev = events[i]
        if ev.get("type") != "assistant":
            continue
        uuid = ev.get("uuid")
        rendered = render_event(ev)
        if uuid and rendered is not None and rendered.kind == "assistant_text" and rendered.body:
            return FinalAnswer(uuid=uuid, text=rendered.body), i
    return None


def last_completed_final_answer(project_dir: Path) -> FinalAnswer | None:
    """Return the final answer of the last completed turn, or ``None``.

    The "final answer" is the last assistant event that renders as plain
    ``assistant_text`` (the rendering the mirror would post as the reply) at or
    before the last turn-end marker in the most recent ``*.jsonl``.  Returns
    ``None`` when there is no completed turn carrying a final text answer.
    """
    found = _final_answer_at(_load_events(project_dir))
    return found[0] if found is not None else None


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

    - cursor at or after the answer  → already delivered, return ``None``;
    - cursor is not in this transcript → unknown, return ``None``.  It says
      nothing about this file (a ``/clear`` starts a new one), and the harm being
      fixed here is duplication, so an unknown cursor keeps quiet.  The rescue
      case that matters (a turn that completed while the mirror was down) always
      has both uuids in the same file, so it is unaffected;
    - cursor before the answer, or no cursor at all → return the answer.  With no
      cursor the caller seeds it instead of posting (see the cog).
    """
    events = _load_events(project_dir)
    found = _final_answer_at(events)
    if found is None:
        return None
    answer, answer_idx = found
    if cursor_uuid is None:
        return answer
    cursor_idx = next(
        (i for i, ev in enumerate(events) if ev.get("uuid") == cursor_uuid),
        None,
    )
    if cursor_idx is None or cursor_idx >= answer_idx:
        return None
    return answer
