"""Pure rendering core for the ``/clord-status`` command (#363).

This module holds the *observation-based* status classification and the table
layout, with **no** Discord / tmux / filesystem dependency, so the behaviour can
be locked down by unit tests and never drifts from the documented definitions.

Status is defined by what is observable, not by the (poll-maintained, up-to-30s
stale) DB ``state`` column:

============  ========  ==================================================
table word    emoji     truth (the only source)
============  ========  ==================================================
``run``       🟢        tmux window exists + pane shows a spinner
``wait``      🟡        tmux window exists + pane shows the ``❯`` prompt
``err``       🔴        tmux window exists + pane shows an error
``closed``    ⚪        no tmux window + session dir exists
(deleted)     —         no tmux window + no session dir  → never a row
============  ========  ==================================================

The docker mental model: default ``/clord-status`` ≈ ``docker ps`` (live only),
``/clord-status all`` ≈ ``docker ps -a`` (live + closed). ``deleted`` is the
``docker rm`` case — counted in the footer, never listed.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime

# DB ``state`` → table word, used only to refine a session that *has* a window.
# A window's existence is what makes it "live"; the DB just says which kind.
_LIVE_STATE_WORD = {
    "running": "run",
    "alive": "run",  # legacy alias for "running"
    "waiting": "wait",
    "error": "err",
    "pending": "run",  # external setter; treat as live
}


def classify_status(*, has_window: bool, db_state: str | None, dir_exists: bool) -> str | None:
    """Return the table word for a session, or ``None`` when it has no row.

    Truth is observation: a tmux window means "live" (``run``/``wait``/``err``,
    refined by ``db_state``); no window but a session dir means ``closed``; no
    window and no dir means deleted → ``None`` (counted, never a row).
    """
    if has_window:
        return _LIVE_STATE_WORD.get((db_state or "").lower(), "run")
    if dir_exists:
        return "closed"
    return None


def format_size(num_bytes: int) -> str:
    """Human-readable byte size (decimal units, terse): ``412 MB`` / ``1.5 GB``."""
    mb = num_bytes / 1_000_000
    if mb >= 1000:
        return f"{mb / 1000:.1f} GB"
    if mb >= 1:
        return f"{round(mb)} MB"
    return f"{round(num_bytes / 1000)} KB"


def format_relative(last_used: str, *, now: datetime) -> str:
    """Compact "time since" label (``30s`` / ``2m`` / ``2h`` / ``3d``)."""
    try:
        then = datetime.fromisoformat(last_used)
    except ValueError:
        return "?"
    secs = max(0, int((now - then).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


@dataclass
class StatusRow:
    """One session's row in the status table.

    ``attach`` is the **measured** ``session:window`` this thread actually lives
    in — read back from tmux, never assembled from a template (#616). It is
    ``None`` for ``closed`` sessions (no tmux window), which renders as ``-``
    and means "no attach target".

    ``window_number`` is kept for ordering only (it is the number inside
    ``attach``); it is not a column any more.
    """

    window_number: int | None
    attach: str | None
    status: str  # run | wait | err | closed
    topic: str
    size_bytes: int
    last_used: str  # ISO-ish "YYYY-MM-DD HH:MM:SS"
    session_id: str


def _short_id(session_id: str) -> str:
    """The first id segment, for ``claude --resume`` display."""
    return session_id.split("-", 1)[0][:8] if session_id else "-"


def _display_width(s: str) -> int:
    """Monospace display columns for ``s`` — East-Asian wide/fullwidth count as 2.

    Discord (and terminals) render the table in a monospace font where CJK
    glyphs are double-width, so padding by ``len()`` misaligns rows with CJK
    topics. Width is computed from ``unicodedata.east_asian_width`` (#408).
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int) -> str:
    """Left-justify ``s`` to ``width`` *display* columns (not char count)."""
    return s + " " * max(0, width - _display_width(s))


def _cell(value: str, *, cap: int = 40) -> str:
    """Make ``value`` safe + bounded for a fenced monospace table cell.

    A topic carrying a newline or a ``` run would otherwise wreck the row
    alignment or break out of the code fence, so collapse whitespace, neutralise
    backticks, and cap the width.
    """
    s = value.replace("\r", " ").replace("\n", " ").replace("\t", " ").replace("`", "'")
    s = " ".join(s.split())
    return s if len(s) <= cap else s[: cap - 1] + "…"


def _build_table(
    rows: list[StatusRow], *, now: datetime, max_rows: int, include_session: bool
) -> str:
    # The CC-session id is niche (few people resume from a cold terminal), so it
    # only rides along in the ``all`` view, at the right edge (#363 feedback).
    header = ["attach", "status", "topic", "size", "used"]
    if include_session:
        header.append("cc-session")
    body: list[list[str]] = []
    for r in rows[:max_rows]:
        cells = [
            _cell(r.attach, cap=24) if r.attach else "-",
            r.status,
            # #616: the attach column costs ~16 columns, paid for out of the
            # topic's budget so the table still fits a phone-width code block.
            _cell(r.topic, cap=20),
            format_size(r.size_bytes),
            format_relative(r.last_used, now=now),
        ]
        if include_session:
            cells.append(_short_id(r.session_id))
        body.append(cells)

    widths = [_display_width(h) for h in header]
    for cells in body:
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], _display_width(c))

    def fmt(cells: list[str]) -> str:
        # pad by display width so CJK-topic rows still line up; last col ragged
        return "  ".join(
            _pad(c, widths[i]) if i < len(cells) - 1 else c for i, c in enumerate(cells)
        ).rstrip()

    lines = [fmt(header), *(fmt(c) for c in body)]
    if len(rows) > max_rows:
        lines.append(f"… 全 {len(rows)} 件中 {max_rows} 件のみ表示（多すぎるため省略）")
    return "\n".join(lines)


def render_status(
    *,
    rows: list[StatusRow],
    show_all: bool,
    channel_name: str,
    repo: str,
    deleted_count: int,
    now: datetime,
    max_rows: int = 25,
) -> str:
    """Render the ``/clord-status`` message content.

    ``rows`` is always the full set of live **and** closed sessions for the
    channel; ``show_all`` only decides what lands in the table. Header/footer
    counts are derived from the full set plus ``deleted_count``.

    There is no channel-wide session name any more (#615/#616): a channel's
    threads can sit in several tmux sessions, so the attach target is per row.
    """
    live = [r for r in rows if r.status != "closed"]
    closed = [r for r in rows if r.status == "closed"]
    # Sort live rows by window number ascending (#363 feedback); closed have no
    # window number so they trail, in given order.
    live.sort(key=lambda r: r.window_number if r.window_number is not None else 10**9)
    total_live = sum(r.size_bytes for r in live)
    total_all = total_live + sum(r.size_bytes for r in closed)
    closed_bytes = sum(r.size_bytes for r in closed)

    title = "c-lord status (all)" if show_all else "c-lord status"
    if show_all:
        head = (
            f"{title} · #{channel_name} · {repo} · "
            f"{len(live)} active · {len(closed)} closed · {format_size(total_all)}"
        )
    else:
        head = (
            f"{title} · #{channel_name} · {repo} · {len(live)} active · {format_size(total_live)}"
        )

    # attach pattern in its own copyable code block (#363 feedback: attach-only, pre).
    # #616: it names no session — rows disagree, so it points at the column that
    # carries the measured target instead of building one from a template.
    attach_block = "```\ntmux attach -t <attach 欄をコピペ>\n```"

    table_rows = (live + closed) if show_all else live
    table = _build_table(table_rows, now=now, max_rows=max_rows, include_session=show_all)

    parts = [head, attach_block, f"```\n{table}\n```"]

    if show_all:
        parts.append(
            f"deleted: {deleted_count} (作業dir削除済 — 会話はホームに残存し再投稿で復活可)"
        )
    elif closed or deleted_count:
        parts.append(
            f"+ {len(closed)} closed ({format_size(closed_bytes)}) · "
            f"deleted {deleted_count}   →   /clord-status all"
        )

    return "\n".join(parts)
