"""Issue #593: the thread name keeps the Issue the thread was opened for.

``sessions.issue_ref`` answers "what is this thread working on **right now**"
(#414) and is re-read from the git branch every turn, so switching to a spun-off
Issue's branch silently overwrites it.  The number that identified the thread in
the sidebar then disappears — yousan lost track of his own request ("540 って
何だっけ？").

The fix separates the two roles: ``origin_issue_ref`` is written once and never
changes, ``issue_ref`` keeps meaning "currently working on", and the name shows
``W1 │ #<origin> <topic> →#<current>`` when they differ.
"""

from __future__ import annotations

import aiosqlite
import pytest

from c_lord.database.models import init_db
from c_lord.database.repository import SessionRepository
from c_lord.thread_name import MAX_NAME_LEN, build_name, parse_topic_from_name

# ── AC1/AC3: the name carries both numbers ───────────────────────────────


def test_name_keeps_origin_and_appends_current_when_they_differ() -> None:
    """AC3: origin leads, current follows — `W131 │ #540 メモリ →#588`."""
    out = build_name("メモリ", "alive", 131, lamp=False, issue_ref="588", origin_issue_ref="540")
    assert out == "W131 │ #540 メモリ →#588"


def test_name_shows_one_number_when_origin_and_current_match() -> None:
    """No `→#` noise while the thread is still on its original Issue."""
    out = build_name("メモリ", "alive", 131, lamp=False, issue_ref="540", origin_issue_ref="540")
    assert out == "W131 │ #540 メモリ"


def test_origin_ref_accepts_a_leading_hash() -> None:
    """Callers may pass ``#540`` or ``540`` — both normalise to one ``#``."""
    out = build_name("t", "alive", 1, lamp=False, issue_ref="#588", origin_issue_ref="#540")
    assert out == "W1 │ #540 t →#588"


# ── AC5: threads with no origin recorded behave exactly as before ────────


def test_missing_origin_falls_back_to_the_current_ref() -> None:
    """AC5: rows predating the column render the old way — no `→#`, no blank."""
    out = build_name("t", "alive", 1, lamp=False, issue_ref="588", origin_issue_ref=None)
    assert out == "W1 │ #588 t"


def test_no_numbers_at_all_is_unchanged() -> None:
    out = build_name("t", "alive", 1, lamp=False)
    assert out == "W1 │ t"


def test_origin_only_renders_without_an_arrow() -> None:
    """A thread whose branch stopped carrying a number keeps its origin."""
    out = build_name("t", "alive", 1, lamp=False, issue_ref=None, origin_issue_ref="540")
    assert out == "W1 │ #540 t"


# ── AC4: the cap sheds the *current* number first ────────────────────────


def test_cap_is_45() -> None:
    """#593: raised from 30 — 46 of 195 live threads were already truncated at
    30, so adding ` →#NNN` there would eat the topic instead of the slack."""
    assert MAX_NAME_LEN == 45


def test_long_topic_drops_the_current_ref_and_keeps_the_origin() -> None:
    """AC4: when it does not fit, `→#current` goes — `#origin` never does."""
    out = build_name("あ" * 100, "alive", 131, lamp=False, issue_ref="588", origin_issue_ref="540")
    assert len(out) <= MAX_NAME_LEN
    assert out.startswith("W131 │ #540 ")
    assert "→#588" not in out


def test_current_ref_never_shortens_the_topic() -> None:
    """The suffix is additive only: a topic that fits without it still fits."""
    topic = "あ" * 20
    without = build_name(topic, "alive", 131, lamp=False, issue_ref="540", origin_issue_ref="540")
    with_cur = build_name(topic, "alive", 131, lamp=False, issue_ref="588", origin_issue_ref="540")
    assert with_cur.startswith(without)
    assert with_cur.endswith("→#588")


def test_real_world_case_fits_without_losing_the_topic() -> None:
    """The three cases from the Issue must survive intact at the new cap."""
    out = build_name(
        "セッション復元案内の実装を統一",
        "alive",
        133,
        lamp=False,
        issue_ref="556",
        origin_issue_ref="551",
    )
    assert out == "W133 │ #551 セッション復元案内の実装を統一 →#556"
    assert len(out) <= MAX_NAME_LEN


# ── the parser must not absorb the new suffix ────────────────────────────


def test_parse_strips_the_trailing_current_ref() -> None:
    """A manual rename of a name carrying `→#588` must not fold it into the
    topic — otherwise the next rebuild doubles it (`… →#588 →#588`)."""
    assert parse_topic_from_name("W131 │ #540 メモリ →#588") == "メモリ"
    assert parse_topic_from_name("🟢 W131 │ #540 メモリ →#588") == "メモリ"


# ── AC1: the column is written once and never moves ──────────────────────


async def test_set_issue_ref_seeds_origin_once(tmp_path) -> None:
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.save(11, "sess-1", working_dir="/w")

    await repo.set_issue_ref(11, "540")
    rec = await repo.get(11)
    assert rec is not None
    assert (rec.issue_ref, rec.origin_issue_ref) == ("540", "540")

    # A branch switch moves `issue_ref` only.
    await repo.set_issue_ref(11, "588")
    rec = await repo.get(11)
    assert rec is not None
    assert (rec.issue_ref, rec.origin_issue_ref) == ("588", "540")


async def test_clearing_issue_ref_leaves_origin_intact(tmp_path) -> None:
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.save(11, "sess-1")
    await repo.set_issue_ref(11, "540")
    await repo.set_issue_ref(11, None)
    rec = await repo.get(11)
    assert rec is not None
    assert rec.issue_ref is None
    assert rec.origin_issue_ref == "540"


async def test_migration_backfills_origin_from_existing_issue_ref(tmp_path) -> None:
    """Decision 3: existing threads copy their current ref into origin, so the
    display does not change today and the *next* branch switch is tracked."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    async with aiosqlite.connect(db) as conn:
        # Simulate a row written before the column existed.
        await conn.execute(
            "INSERT INTO sessions (thread_id, session_id, issue_ref, origin_issue_ref) "
            "VALUES (?, ?, ?, NULL)",
            (22, "sess-2", "540"),
        )
        await conn.commit()

    await init_db(db)  # migrations are idempotent and run on every startup

    rec = await SessionRepository(db).get(22)
    assert rec is not None
    assert rec.origin_issue_ref == "540"


async def test_migration_leaves_numberless_rows_alone(tmp_path) -> None:
    """Decision 4: a thread that never had a number does not gain a fake one."""
    db = str(tmp_path / "s.db")
    await init_db(db)
    repo = SessionRepository(db)
    await repo.save(33, "sess-3")
    await init_db(db)
    rec = await repo.get(33)
    assert rec is not None
    assert rec.issue_ref is None
    assert rec.origin_issue_ref is None


@pytest.mark.parametrize("closed", [False, True])
def test_stopped_threads_also_keep_the_origin(closed: bool) -> None:
    """#512's `[停止]` name is built by a different call site — it must carry the
    origin too, or stopping a thread would erase the number it was opened for."""
    out = build_name(
        "t", "dead", None, lamp=False, issue_ref="588", origin_issue_ref="540", closed=closed
    )
    assert "#540" in out


# ── every naming call site must forward the origin ───────────────────────


def test_every_build_name_call_site_passes_the_origin() -> None:
    """The thread name is rebuilt from three places (the per-turn naming pass,
    the 60s sidebar sync, and stop/start). A site that forgets
    ``origin_issue_ref`` silently repaints the name back to the current-only
    form — the exact regression #593 fixes — and only shows up in production.
    """
    import re
    from pathlib import Path

    root = Path(__file__).parent.parent / "c_lord"
    call_re = re.compile(r"build_name\((.*?)\n\s*\)", re.DOTALL)
    offenders: list[str] = []
    sites = 0
    for path in sorted(root.rglob("*.py")):
        if path.name == "thread_name.py":  # the definition itself
            continue
        for match in call_re.finditer(path.read_text(encoding="utf-8")):
            sites += 1
            if "origin_issue_ref" not in match.group(1):
                offenders.append(f"{path.relative_to(root)}: {match.group(0).splitlines()[0]}")
    assert sites >= 3, f"expected the three known call sites, found {sites}"
    assert not offenders, "build_name() call sites missing origin_issue_ref:\n" + "\n".join(
        offenders
    )


# ── AC2: the reported bug, end to end through the naming pass ────────────


async def test_branch_switch_keeps_the_origin_in_the_rendered_name() -> None:
    """AC2: the #593 repro. A thread opened for #540 whose owner files #588 and
    switches to ``feature/588-…`` must not lose ``#540`` from its name."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import discord

    from tests.test_claude_chat import _make_cog

    record = MagicMock()
    record.topic = "メモリ"
    record.auto_topic_locked = 0
    record.state = "running"
    record.tmux_window_id = "@1"
    record.issue_ref = "540"
    record.origin_issue_ref = "540"
    record.closed_at = None

    cog = _make_cog()
    cog._thread_lamp = False
    cog.repo.get = AsyncMock(return_value=record)
    cog.repo.set_topic = AsyncMock()
    cog.repo.set_tmux_window_id = AsyncMock()
    cog.repo.set_issue_ref = AsyncMock()

    thread = MagicMock(spec=discord.Thread)
    thread.id = 55555
    thread.name = "W131 │ #540 メモリ"
    thread.edit = AsyncMock()

    tmux = MagicMock()
    tmux.get_window_info = MagicMock(return_value=("@1", 131))

    with patch.object(cog, "_git_current_branch", return_value="feature/588-memory-store"):
        await cog._apply_thread_naming(
            thread=thread, tmux_manager=tmux, first_message="続きお願いします", working_dir="/w"
        )

    # The branch switch is recorded as the *current* work...
    cog.repo.set_issue_ref.assert_awaited_once_with(55555, "588")
    # ...and the name keeps the number the thread was opened for.
    name = thread.edit.await_args.kwargs.get("name", "")
    assert name == "W131 │ #540 メモリ →#588", name
