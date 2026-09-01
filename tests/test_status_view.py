"""Tests for the pure /clord-status rendering core (#363).

This module is the drift-prevention seam for the status command: the status
classification table and the rendered layout are exercised here without Discord,
tmux, or the filesystem.
"""

from __future__ import annotations

import unicodedata
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
        classify_status(has_window=has_window, db_state=db_state, dir_exists=dir_exists) == expected
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
            attach="c-lord:w1",
            status="run",
            topic="auth-bug-fix",
            size_bytes=412_000_000,
            last_used="2026-06-11 10:00:00",
            session_id="a1b2c3d4-1111",
        ),
        StatusRow(
            window_number=2,
            attach="c-lord:w2",
            status="wait",
            topic="readme-update",
            size_bytes=188_000_000,
            last_used="2026-06-11 11:55:00",
            session_id="e5f6a7b8-2222",
        ),
        StatusRow(
            window_number=None,
            attach=None,
            status="closed",
            topic="old-refactor",
            size_bytes=96_000_000,
            last_used="2026-06-08 12:00:00",
            session_id="c1d2e3f4-3333",
        ),
    ]


def _table_rows(out: str) -> list[str]:
    """Extract the data rows from the fenced *table* block (there is also a
    separate attach-command block now), dropping the column header and any
    truncation note."""
    blocks = out.split("```")
    # code blocks are odd indices; the table is the one carrying the header cols
    table = next(b for i, b in enumerate(blocks) if i % 2 == 1 and "status" in b and "topic" in b)
    lines = [ln for ln in table.splitlines() if ln.strip()]
    return [ln for ln in lines[1:] if not ln.lstrip().startswith("…")]


def _render(show_all: bool, **kw):
    defaults = dict(
        rows=_rows(),
        show_all=show_all,
        channel_name="dev-claude",
        repo="yousan/c-lord",
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
    assert "2 active" in out
    # live topics present, closed topic absent from the table
    assert "auth-bug-fix" in out
    assert "readme-update" in out
    assert "old-refactor" not in out
    # legend was removed from the output (now lives in docs, #363 feedback)
    assert "legend" not in out
    # attach pattern is attach-only (no resume hint) in a code block; #616 made
    # it point at the measured column instead of naming one session.
    assert "tmux attach -t" in out
    assert "work<#>" not in out
    assert "claude --resume" not in out
    # cc-session column is NOT shown in the default view
    assert "cc-session" not in out
    assert "a1b2c3d4" not in out  # the resume/session id is hidden by default
    # footer points to `all` with the closed + deleted summary
    assert "1 closed" in out
    assert "deleted 2" in out
    assert "/clord-status all" in out


def test_default_view_sorted_by_window_number_ascending():
    out = _render(show_all=False)
    # #616: the first column is the measured attach target; the ordering key is
    # still the window number behind it (#363 feedback: ascending by #).
    assert [ln.split()[0] for ln in _table_rows(out)] == ["c-lord:w1", "c-lord:w2"]


def test_all_view_shows_closed_rows_and_cc_session():
    out = _render(show_all=True)
    assert "(all)" in out
    assert "old-refactor" in out  # closed row now present
    assert "closed" in out
    assert "deleted: 2" in out
    # cc-session column appears only in the all view, at the right edge
    assert "cc-session" in out
    assert "c1d2e3f4" in out  # the closed session's id
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


def test_topic_with_backticks_and_newline_cannot_break_the_table():
    rows = [
        StatusRow(
            window_number=1,
            attach="c-lord:w1",
            status="run",
            topic="evil```\ntopic\nwith breaks",
            size_bytes=10_000_000,
            last_used="2026-06-11 11:00:00",
            session_id="dead-beef",
        )
    ]
    out = render_status(
        rows=rows,
        show_all=False,
        channel_name="dev",
        repo="yousan/c-lord",
        deleted_count=0,
        now=NOW,
    )
    # two fenced blocks (attach + table); a stray ``` in the topic would add
    # more fence delimiters than these 4 if not neutralised
    assert out.count("```") == 4
    # the single data row stayed a single line
    assert len(_table_rows(out)) == 1


def _dw(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def test_display_width_counts_fullwidth_as_two():
    from c_lord.status_view import _display_width

    assert _display_width("ab") == 2
    assert _display_width("作業") == 4
    assert _display_width("a作") == 3


def test_cjk_and_ascii_topics_align_by_display_width():
    # Two topics of equal *display* width but different char count must produce
    # rows of equal display width (columns aligned by display width, not len()).
    rows = [
        StatusRow(1, "c-lord:w1", "run", "あ", 4_000_000, "2026-06-11 11:00:00", "x1"),
        StatusRow(2, "c-lord:w2", "run", "ab", 4_000_000, "2026-06-11 11:00:00", "x2"),
    ]
    out = _render(show_all=False, rows=rows)
    l0, l1 = _table_rows(out)
    assert _dw(l0) == _dw(l1)


def test_row_cap_is_announced_not_silent():
    many = [
        StatusRow(
            window_number=i,
            attach=f"c-lord:w{i}",
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
        deleted_count=0,
        now=NOW,
        max_rows=25,
    )
    # truncation must be visible, never silent
    assert "25" in out and "40" in out


# ── #616: the attach target is measured, never derived ──────────────────────


def _attach_rows():
    """Rows whose windows live in three *different* tmux sessions.

    That is the real shape after #615: sessions follow the repository, so one
    channel's threads can sit in several sessions — and one of them still
    carries a legacy ``work{N}`` window name.
    """
    return [
        StatusRow(
            window_number=1,
            attach="qiita-article:w1",
            status="wait",
            topic="Qiita記事執筆",
            size_bytes=412_000_000,
            last_used="2026-06-11 10:00:00",
            session_id="a1b2c3d4-1111",
        ),
        StatusRow(
            window_number=5,
            attach="claude_base:work5",
            status="run",
            topic="2017.l2tp.org",
            size_bytes=120_000_000,
            last_used="2026-06-11 11:55:00",
            session_id="e5f6a7b8-2222",
        ),
        StatusRow(
            window_number=None,
            attach=None,
            status="closed",
            topic="old-refactor",
            size_bytes=96_000_000,
            last_used="2026-06-08 12:00:00",
            session_id="c1d2e3f4-3333",
        ),
    ]


class TestAttachColumn:
    def test_each_row_carries_its_own_session_and_window(self):
        out = _render(show_all=False, rows=_attach_rows())
        body = "\n".join(_table_rows(out))
        assert "qiita-article:w1" in body
        assert "claude_base:work5" in body, "a legacy work{N} name must print verbatim"

    def test_header_row_labels_the_column_attach(self):
        out = _render(show_all=False, rows=_attach_rows())
        table = next(
            b
            for i, b in enumerate(out.split("```"))
            if i % 2 == 1 and "status" in b and "topic" in b
        )
        header = table.strip().splitlines()[0]
        assert header.split()[0] == "attach"

    def test_no_derived_attach_template_anywhere(self):
        """The old header built ``<session>:work<#>`` by hand — that is the bug."""
        out = _render(show_all=False, rows=_attach_rows())
        assert "work<#>" not in out
        assert "w<#>" not in out

    def test_closed_row_offers_no_attach_target(self):
        out = _render(show_all=True, rows=_attach_rows())
        closed = [ln for ln in _table_rows(out) if "old-refactor" in ln]
        assert len(closed) == 1
        assert closed[0].split()[0] == "-"

    def test_attach_block_points_at_the_column(self):
        out = _render(show_all=False, rows=_attach_rows())
        attach_block = next(
            b for i, b in enumerate(out.split("```")) if i % 2 == 1 and "tmux attach" in b
        )
        assert "tmux attach -t" in attach_block
        # ...and it must not name one session, because rows disagree.
        assert "qiita-article" not in attach_block
        assert "claude_base" not in attach_block
