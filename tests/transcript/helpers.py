"""Shared fixtures for the transcript tests.

Since #627 the mirror only reads a transcript that **c-lord itself drove** —
recognised by the zero-width space c-lord prefixes every pane prompt with.  A
project dir holds many sessions (every ``claude -p`` writes its own), and
picking the mtime-latest was how a sub-invocation's private conversation ended
up posted into a user's thread.

So a test transcript that the mirror is expected to read has to look like one:
:func:`clord_transcript` stamps that marker.
"""

from __future__ import annotations

import json
from pathlib import Path

# The marker c-lord prefixes pane input with (``c_lord.transcript.formatter``).
ZWSP = "​"


def clord_marker_event(uuid: str = "clord-marker") -> dict:
    """A ``user`` event shaped like a prompt c-lord drove into the pane."""
    return {
        "type": "user",
        "uuid": uuid,
        "message": {"role": "user", "content": ZWSP + "(c-lord が送ったプロンプト)"},
    }


def clord_transcript(path: Path, uuid: str = "clord-marker") -> Path:
    """Create/overwrite ``path`` as a transcript the mirror may read (#627).

    Written *before* the tail starts in most tests, so the marker line is part
    of the already-delivered baseline and is never yielded — the test's own
    events stay the only output.
    """
    path.write_text(json.dumps(clord_marker_event(uuid), ensure_ascii=False) + "\n", "utf-8")
    return path
