"""Read an AskUserQuestion's real outcome back out of the transcript (#651).

Until #651, c-lord treated "tmux accepted the keystrokes" as "the answer
reached Claude". Those are different questions, and the gap is not theoretical:
on 2026-09-01 the keys were delivered exactly as asked, the menu closed, and
Claude still recorded ``(No answer provided)`` — so Discord showed ✅ over an
answer that had been thrown away (#650).

Claude Code writes the menu's ``tool_result`` into its own transcript, and that
text says in plain language which of the two happened. Reading it back is the
only check c-lord has that is about *the answer* rather than about the
keystrokes or the pixels.

The tool_use id is recovered from the transcript rather than passed in: three of
the four bridge call sites discover their menu by parsing the pane and never
have an id to pass.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

#: The answer reached Claude.
ASK_ANSWERED = "answered"
#: The menu resolved, but Claude was told no answer was given.
ASK_NOT_ANSWERED = "not_answered"
#: Nothing conclusive — no result yet, or wording we do not recognise.
ASK_UNKNOWN = "unknown"

AskOutcome = Literal["answered", "not_answered", "unknown"]

# Claude Code's own wording for a resolved AskUserQuestion.  Matching on text is
# unusual for this codebase, but it is what the transcript records — and both
# strings are stable, tool-specific, and carried verbatim into Claude's context.
_ANSWERED_MARKER = "The user answered:"
_NOT_ANSWERED_MARKERS = (
    "(No answer provided)",
    "The user wants to clarify these questions",
)

# Cheap pre-filter: only lines mentioning the tool are worth parsing.
_ASK_TOOL_NAME = "AskUserQuestion"


def _iter_events(project_dir: Path, needle: str, only: Path | None = None):
    """Yield ``(path, event)`` for jsonl lines in *project_dir* containing *needle*.

    One cwd holds many session files — the #627 example dir held **182**, some
    of them megabytes (see :mod:`c_lord.transcript.resolver`) — so *only* exists
    to pin the search to the one file already known to hold the menu. Without it
    the answer-confirmation poll would re-read the whole directory twice a
    second. Unreadable files are skipped: a transcript we cannot read is
    "unknown", never an error that could take down a turn.
    """
    if only is not None:
        paths = [only]
    else:
        try:
            paths = sorted(project_dir.glob("*.jsonl"))
        except OSError:
            return
    for path in paths:
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if needle not in line:
                        continue
                    try:
                        yield path, json.loads(line)
                    except (ValueError, TypeError):
                        continue
        except OSError:
            continue


def _blocks(event: dict) -> list:
    content = event.get("message", {}).get("content")
    return content if isinstance(content, list) else []


def latest_ask_tool_use(project_dir: Path) -> tuple[str, Path] | None:
    """The most recent ``AskUserQuestion`` tool_use: ``(id, session file)``.

    "Most recent" is the menu currently on screen — the bridge looks this up
    while the menu is still open, so nothing newer can exist yet.

    The file is returned along with the id so the outcome can later be polled
    from that one file: a ``tool_result`` always lands in the same session
    transcript as its ``tool_use``.
    """
    best: tuple[str, str, Path] | None = None  # (timestamp, id, path)
    for path, event in _iter_events(project_dir, _ASK_TOOL_NAME):
        ts = str(event.get("timestamp") or "")
        for block in _blocks(event):
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == _ASK_TOOL_NAME
            ):
                tool_use_id = block.get("id")
                if isinstance(tool_use_id, str) and (best is None or ts >= best[0]):
                    best = (ts, tool_use_id, path)
    return (best[1], best[2]) if best else None


def latest_ask_tool_use_id(project_dir: Path) -> str | None:
    """Id half of :func:`latest_ask_tool_use`."""
    found = latest_ask_tool_use(project_dir)
    return found[0] if found else None


def _result_text(block: dict) -> str | None:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
        ]
        if parts:
            return "\n".join(parts)
    return None


def read_ask_result(
    project_dir: Path, tool_use_id: str, session_path: Path | None = None
) -> str | None:
    """The ``tool_result`` text for *tool_use_id*, or None while it is unanswered.

    Pass *session_path* (from :func:`latest_ask_tool_use`) when polling: the
    result lands in the same file as the tool_use, so there is no reason to
    re-read every session in the directory on every tick.
    """
    for _path, event in _iter_events(project_dir, tool_use_id, only=session_path):
        for block in _blocks(event):
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == tool_use_id
            ):
                text = _result_text(block)
                if text is not None:
                    return text
    return None


def classify_ask_result(text: str | None) -> AskOutcome:
    """Did the user's answer reach Claude, per the transcript's own wording?

    Anything unrecognised is :data:`ASK_UNKNOWN`, never a success: guessing ✅ on
    text we do not understand is exactly the failure this module exists to stop.
    """
    if not text:
        return ASK_UNKNOWN
    if any(marker in text for marker in _NOT_ANSWERED_MARKERS):
        return ASK_NOT_ANSWERED
    if _ANSWERED_MARKER in text:
        return ASK_ANSWERED
    return ASK_UNKNOWN
