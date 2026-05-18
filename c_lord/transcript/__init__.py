"""JSONL transcript mirror (Issue #71).

c-lord acts as a transparent mirror of the Claude Code session transcript at
``~/.claude/projects/<slug>/<session-id>.jsonl``.  This package provides the
three building blocks of that mirror:

- :mod:`.resolver` — map a tmux ``cwd`` to its project transcript directory and
  pick the current session file (mtime-latest, so ``/clear`` is followed).
- :mod:`.formatter` — render a single JSONL event into Discord-bound text, or
  return ``None`` for events that should be skipped (thinking, framing meta,
  c-lord-originated ZWSP-marked input, etc.).
- :mod:`.tail` — async generator that follows the active jsonl, switches to a
  newer file when the project's mtime-latest changes, and recovers from
  truncation/rewrite.
"""

from __future__ import annotations

from .formatter import ZWSP_MARKER, RenderedEvent, render_event
from .resolver import derive_project_dir, latest_session_jsonl
from .tail import tail_events

__all__ = [
    "ZWSP_MARKER",
    "RenderedEvent",
    "derive_project_dir",
    "latest_session_jsonl",
    "render_event",
    "tail_events",
]
