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

# Co-located legends so the definition travels with the output (anti-drift).
_LEGEND_DEFAULT = "legend: run=実行中  wait=入力待ち  err=エラー"
_LEGEND_ALL = (
    "legend: run/wait/err=live  "
    "closed=/close-workspace済(window無し/dir残・容量を食う)  "
    "deleted=dir削除済(行なし)"
)


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

    ``window_number`` is ``None`` for ``closed`` sessions (no tmux window), which
    renders as ``-`` and means "no attach target".
    """

    window_number: int | None
    status: str  # run | wait | err | closed
    topic: str
    size_bytes: int
    last_used: str  # ISO-ish "YYYY-MM-DD HH:MM:SS"
    session_id: str


def _short_id(session_id: str) -> str:
    """The first id segment, for ``claude --resume`` display."""
    return session_id.split("-", 1)[0][:8] if session_id else "-"


def _cell(value: str, *, cap: int = 40) -> str:
    """Make ``value`` safe + bounded for a fenced monospace table cell.

    A topic carrying a newline or a ``` run would otherwise wreck the row
    alignment or break out of the code fence, so collapse whitespace, neutralise
    backticks, and cap the width.
    """
    s = value.replace("\r", " ").replace("\n", " ").replace("\t", " ").replace("`", "'")
    s = " ".join(s.split())
    return s if len(s) <= cap else s[: cap - 1] + "…"


def _build_table(rows: list[StatusRow], *, now: datetime, max_rows: int) -> str:
    header = ["#", "status", "topic", "size", "used", "resume"]
    body: list[list[str]] = []
    for r in rows[:max_rows]:
        body.append(
            [
                str(r.window_number) if r.window_number is not None else "-",
                r.status,
                _cell(r.topic, cap=28),
                format_size(r.size_bytes),
                format_relative(r.last_used, now=now),
                _short_id(r.session_id),
            ]
        )

    widths = [len(h) for h in header]
    for cells in body:
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))

    def fmt(cells: list[str]) -> str:
        # last column not padded (ragged right is fine)
        return "  ".join(
            c.ljust(widths[i]) if i < len(cells) - 1 else c for i, c in enumerate(cells)
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
    session_name: str,
    deleted_count: int,
    now: datetime,
    max_rows: int = 25,
) -> str:
    """Render the ``/clord-status`` message content.

    ``rows`` is always the full set of live **and** closed sessions for the
    channel; ``show_all`` only decides what lands in the table. Header/footer
    counts are derived from the full set plus ``deleted_count``.
    """
    live = [r for r in rows if r.status != "closed"]
    closed = [r for r in rows if r.status == "closed"]
    total_live = sum(r.size_bytes for r in live)
    total_all = total_live + sum(r.size_bytes for r in closed)
    closed_bytes = sum(r.size_bytes for r in closed)

    title = "c-lord status (all)" if show_all else "c-lord status"
    if show_all:
        head = (
            f"{title} · #{channel_name} · {repo} · session {session_name} · "
            f"{len(live)} active · {len(closed)} closed · {format_size(total_all)}"
        )
    else:
        head = (
            f"{title} · #{channel_name} · {repo} · session {session_name} · "
            f"{len(live)} active · {format_size(total_live)}"
        )

    attach_line = f"attach: tmux attach -t {session_name}:work<#>      resume: claude --resume <id>"
    legend = _LEGEND_ALL if show_all else _LEGEND_DEFAULT

    table_rows = (live + closed) if show_all else live
    table = _build_table(table_rows, now=now, max_rows=max_rows)

    parts = [head, attach_line, legend, f"```\n{table}\n```"]

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
