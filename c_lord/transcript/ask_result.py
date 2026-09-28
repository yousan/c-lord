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

from ..log_sampler import LogSampler

logger = logging.getLogger(__name__)

#: The answer reached Claude.
ASK_ANSWERED = "answered"
#: The menu resolved, but Claude was told no answer was given.
ASK_NOT_ANSWERED = "not_answered"
#: Nothing conclusive — no result yet, or wording we do not recognise.
ASK_UNKNOWN = "unknown"

AskOutcome = Literal["answered", "not_answered", "unknown"]

# Claude Code's own wording for a resolved AskUserQuestion.  Matching on text is
# unusual for this codebase, but it is what the transcript records.
#
# #707: this used to be one string, described here as "stable".  It was not —
# the CLI changed the success wording and c-lord went quietly blind: EVERY
# answered menu classified as ``unknown``, so the ✅ that #651 exists to earn was
# effectively never shown and users were told "Claude が受け取ったかどうかを確認
# できませんでした" over answers Claude had plainly received.  Nothing broke
# loudly, because ``unknown`` is also the normal state while polling.
#
# Both wordings are kept: transcripts written by older CLIs are still on disk and
# still read back.
_ANSWERED_MARKERS = (
    "The user answered:",  # ≤ v2.1.x
    "Your questions have been answered:",  # v2.1.263, measured 2026-09-08
)
_NOT_ANSWERED_MARKERS = (
    "(No answer provided)",
    "The user wants to clarify these questions",
)

# #707 AC3: adding a literal fixes today and rebuilds the same trap for tomorrow.
# What actually failed is that c-lord could not tell "no result yet" from "a
# result I do not understand" — both are ``unknown``, and ``unknown`` is normal
# while polling, so nothing ever looked wrong.  A result that exists and matches
# nothing is now audible.  Sampled per distinct wording (the confirm poll re-reads
# the same text every 0.5s for up to 12s) — the #678 rule: never DEBUG-and-forget,
# never flood.
_unknown_wording_sampler = LogSampler()

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


def ask_tool_uses(project_dir: Path) -> list[tuple[str, str, Path]]:
    """Every ``AskUserQuestion`` tool_use in *project_dir*, oldest first.

    ``(timestamp, id, session file)`` — the file comes along so the outcome can
    later be polled from that one file: a ``tool_result`` always lands in the
    same session transcript as its ``tool_use``.
    """
    found: list[tuple[str, str, Path]] = []
    for path, event in _iter_events(project_dir, _ASK_TOOL_NAME):
        ts = str(event.get("timestamp") or "")
        for block in _blocks(event):
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == _ASK_TOOL_NAME
                and isinstance(block.get("id"), str)
            ):
                found.append((ts, block["id"], path))
    found.sort(key=lambda item: item[0])
    return found


def latest_ask_tool_use(project_dir: Path) -> tuple[str, Path] | None:
    """The most recent ``AskUserQuestion`` tool_use: ``(id, session file)``.

    Beware: this is not always the menu on screen. The CLI sometimes writes a
    menu's ``tool_use`` only together with its result, i.e. after the menu is
    answered — and then the most recent ask is an *earlier*, finished one
    (#746, found on staging). ``ask_handler._locate_menu`` checks for that.
    """
    found = ask_tool_uses(project_dir)
    return (found[-1][1], found[-1][2]) if found else None


def first_ask_tool_use_after(project_dir: Path, after: str | None) -> tuple[str, Path] | None:
    """The earliest ``AskUserQuestion`` tool_use written after timestamp *after*.

    For a menu whose ``tool_use`` was not in the transcript yet when it was
    answered (#746): it is the first ask to appear after the newest one that
    *was* there. The earliest, not the newest — a later question must never be
    mistaken for this one. ``None`` for *after* means "any".
    """
    for ts, tool_use_id, path in ask_tool_uses(project_dir):
        if after is None or ts > after:
            return tool_use_id, path
    return None


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
        # Nothing written yet — the normal case while the confirm poll waits.
        return ASK_UNKNOWN
    if any(marker in text for marker in _NOT_ANSWERED_MARKERS):
        return ASK_NOT_ANSWERED
    if any(marker in text for marker in _ANSWERED_MARKERS):
        return ASK_ANSWERED
    sample = _unknown_wording_sampler.sample(text[:200])
    if sample.emit:
        logger.warning(
            "AskUserQuestion tool_result in wording c-lord does not recognise — "
            "the ✅/❔ decision cannot be made and every answered menu will read as "
            "unconfirmed until this is taught. Claude Code may have changed its "
            "wording again (#707). First 200 chars: %r%s",
            text[:200],
            sample.suffix,
        )
    return ASK_UNKNOWN
