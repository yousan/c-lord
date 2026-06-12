"""Incremental scan state: per-logfile byte offsets + seen fingerprints (#404).

Offsets are keyed by the *resolved* real path so the bot's ``…-latest.log``
symlink repointing to a fresh per-run file (each restart) is scanned from the
start of the new file rather than skipped. A file that shrank below its recorded
offset (truncation / rotation) is re-read from 0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MonitorState:
    offsets: dict[str, int] = field(default_factory=dict)
    seen: list[str] = field(default_factory=list)  # fingerprints (list for JSON)

    @property
    def seen_set(self) -> set[str]:
        return set(self.seen)


def load_state(path: Path) -> MonitorState:
    if not path.exists():
        return MonitorState()
    try:
        data = json.loads(path.read_text())
        return MonitorState(offsets=dict(data.get("offsets", {})), seen=list(data.get("seen", [])))
    except (ValueError, OSError):
        return MonitorState()


def save_state(path: Path, state: MonitorState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"offsets": state.offsets, "seen": sorted(set(state.seen))}, indent=1)
    )


def read_new_log_text(real_path: Path, prev_offset: int) -> tuple[str, int]:
    """Return (new_text_since_offset, new_offset). Missing file → ("", 0)."""
    try:
        size = real_path.stat().st_size
    except OSError:
        return "", 0
    start = 0 if size < prev_offset else prev_offset  # truncated/rotated → from 0
    try:
        with real_path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            text = f.read()
        return text, size
    except OSError:
        return "", prev_offset
