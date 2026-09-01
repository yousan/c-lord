"""JSONL transcript mirror (Issue #71).

c-lord acts as a transparent mirror of the Claude Code session transcript at
``~/.claude/projects/<slug>/<session-id>.jsonl``.  This package provides the
three building blocks of that mirror:

- :mod:`.resolver` — map a tmux ``cwd`` to its project transcript directory and
  pick the session file **this thread** owns.  One cwd holds many sessions
  (every ``claude -p`` writes its own), so the choice is "newest transcript
  c-lord itself drove", not "newest file" (#627).
- :mod:`.formatter` — render a single JSONL event into Discord-bound text, or
  return ``None`` for events that should be skipped (thinking, framing meta,
  c-lord-originated ZWSP-marked input, etc.).
- :mod:`.tail` — async generator that follows the active jsonl, switches to a
  newer session of the same thread (``/clear``), and recovers from
  truncation/rewrite.  All of its I/O runs off the event loop (#537).
"""

from __future__ import annotations

from .formatter import ZWSP_MARKER, RenderedEvent, render_event
from .resolver import ThreadSessionResolver, derive_project_dir, latest_session_jsonl
from .tail import tail_events

__all__ = [
    "ZWSP_MARKER",
    "RenderedEvent",
    "ThreadSessionResolver",
    "derive_project_dir",
    "latest_session_jsonl",
    "render_event",
    "tail_events",
]
