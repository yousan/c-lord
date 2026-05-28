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


def last_completed_final_answer(project_dir: Path) -> FinalAnswer | None:
    """Return the final answer of the last completed turn, or ``None``.

    The "final answer" is the last assistant event that renders as plain
    ``assistant_text`` (the rendering the mirror would post as the reply) at or
    before the last turn-end marker in the most recent ``*.jsonl``.  Returns
    ``None`` when there is no completed turn carrying a final text answer.
    """
    jsonl = latest_session_jsonl(project_dir)
    if jsonl is None:
        return None
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
        return None

    last_end = -1
    for i, ev in enumerate(events):
        if _is_turn_end(ev):
            last_end = i
    if last_end < 0:
        return None

    for ev in reversed(events[: last_end + 1]):
        if ev.get("type") != "assistant":
            continue
        uuid = ev.get("uuid")
        rendered = render_event(ev)
        if uuid and rendered is not None and rendered.kind == "assistant_text" and rendered.body:
            return FinalAnswer(uuid=uuid, text=rendered.body)
    return None
