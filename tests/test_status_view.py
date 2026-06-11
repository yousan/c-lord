"""Tests for the pure /clord-status rendering core (#363).

This module is the drift-prevention seam for the status command: the status
classification table and the rendered layout are exercised here without Discord,
tmux, or the filesystem.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from c_lord.status_view import (
    StatusRow,
    classify_status,
    format_relative,
    format_size,
    render_status,
)

NOW = datetime(2026, 6, 11, 12, 0, 0)


# ── classify_status: observation, not the DB ────────────────────────────────


@pytest.mark.parametrize(
    ("has_window", "db_state", "dir_exists", "expected"),
    [
        # has a tmux window -> live; DB state only refines the word
        (True, "running", True, "run"),
        (True, "alive", True, "run"),  # legacy alias for running
        (True, "waiting", True, "wait"),
        (True, "error", True, "err"),
        (True, "dead", True, "run"),  # stale DB; window exists -> still live
        (True, None, True, "run"),
        # no window, dir present -> closed (/close-workspace result)
        (False, "dead", True, "closed"),
        (False, "waiting", True, "closed"),  # DB is stale; no window wins
        # no window, no dir -> deleted -> no row
        (False, "dead", False, None),
        (False, None, False, None),
    ],
)
def test_classify_status(has_window, db_state, dir_exists, expected):
    assert (
        classify_status(has_window=has_window, db_state=db_state, dir_exists=dir_exists)
        == expected
    )


# ── format_size ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (412_000_000, "412 MB"),
        (96_000_000, "96 MB"),
        (1_500_000_000, "1.5 GB"),
        (500_000, "500 KB"),
        (0, "0 KB"),
    ],
)
def test_format_size(num_bytes, expected):
    assert format_size(num_bytes) == expected


# ── format_relative ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("last_used", "expected"),
    [
        ("2026-06-11 11:58:00", "2m"),
        ("2026-06-11 10:00:00", "2h"),
        ("2026-06-08 12:00:00", "3d"),
        ("2026-06-11 11:59:30", "30s"),
    ],
)
def test_format_relative(last_used, expected):
    assert format_relative(last_used, now=NOW) == expected


# ── render_status ───────────────────────────────────────────────────────────


def _rows():
    return [
        StatusRow(
            window_number=1,
            status="run",
            topic="auth-bug-fix",
            size_bytes=412_000_000,
            last_used="2026-06-11 10:00:00",
            session_id="a1b2c3d4-1111",
        ),
        StatusRow(
            window_number=2,
            status="wait",
            topic="readme-update",
            size_bytes=188_000_000,
            last_used="2026-06-11 11:55:00",
            session_id="e5f6a7b8-2222",
        ),
        StatusRow(
            window_number=None,
            status="closed",
            topic="old-refactor",
            size_bytes=96_000_000,
            last_used="2026-06-08 12:00:00",
            session_id="c1d2e3f4-3333",
        ),
    ]


def _table_rows(out: str) -> list[str]:
    """Extract the data rows from the single fenced table block (drop the
    column header and any truncation note)."""
    block = out.split("```")[1]
    lines = [ln for ln in block.splitlines() if ln.strip()]
    # lines[0] is the column header ("#  status  topic ...")
    return [ln for ln in lines[1:] if not ln.lstrip().startswith("…")]


def _render(show_all: bool, **kw):
    defaults = dict(
        rows=_rows(),
        show_all=show_all,
        channel_name="dev-claude",
        repo="yousan/c-lord",
        session_name="c-lord",
        deleted_count=2,
        now=NOW,
    )
    defaults.update(kw)
    return render_status(**defaults)


def test_default_view_shows_only_live_rows():
    out = _render(show_all=False)
    # header reflects active count + session
    assert "c-lord status" in out
    assert "dev-claude" in out
    assert "session c-lord" in out or "c-lord" in out
    assert "2 active" in out
    # live topics present, closed topic absent from the table
    assert "auth-bug-fix" in out
    assert "readme-update" in out
    assert "old-refactor" not in out
    # legend is co-located with output (anti-drift)
    assert "legend:" in out
    # attach pattern shown once, derived from #
    assert "tmux attach -t c-lord:work<#>" in out
    # footer points to `all` with the closed + deleted summary
    assert "1 closed" in out
    assert "deleted 2" in out
    assert "/clord-status all" in out


def test_all_view_shows_closed_rows():
    out = _render(show_all=True)
    assert "(all)" in out
    assert "old-refactor" in out  # closed row now present
    assert "closed" in out
    assert "deleted: 2" in out
    # closed rows have no window number -> rendered as "-"
    closed_line = next(ln for ln in _table_rows(out) if "old-refactor" in ln)
    assert closed_line.lstrip().startswith("-")


def test_deleted_never_becomes_a_row():
    # deleted sessions are a count only — never a StatusRow — so even with a big
    # deleted_count the table body must not grow.
    out = _render(show_all=True, deleted_count=99)
    assert "deleted: 99" in out
    # only 3 data rows (2 live + 1 closed); deleted contributes none
    assert len(_table_rows(out)) == 3


def test_row_cap_is_announced_not_silent():
    many = [
        StatusRow(
            window_number=i,
            status="run",
            topic=f"task-{i}",
            size_bytes=10_000_000,
            last_used="2026-06-11 11:00:00",
            session_id=f"{i:08d}-xxxx",
        )
        for i in range(40)
    ]
    out = render_status(
        rows=many,
        show_all=False,
        channel_name="dev",
        repo="yousan/c-lord",
        session_name="c-lord",
        deleted_count=0,
        now=NOW,
        max_rows=25,
    )
    # truncation must be visible, never silent
    assert "25" in out and "40" in out
