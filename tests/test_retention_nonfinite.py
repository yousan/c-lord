"""#1240: ``cleanupPeriodDays`` of ``1e999`` or ``NaN`` made the retention read raise.

``~/.claude/settings.json`` is hand-written. JSON ``1e999`` loads as infinity and
``NaN`` as NaN; ``int()`` of either raises, so deciding how long workspaces are kept
(#575) stopped with an exception instead of following the docstring ("every failure
is None"). Like any other bad value: WARNING and None. An integer of more than
4300 digits made ``json.loads`` itself raise ``ValueError`` — the same promise.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "raw",
    [
        '{"cleanupPeriodDays": 1e999}',
        '{"cleanupPeriodDays": NaN}',
        '{"cleanupPeriodDays": -Infinity}',
        '{"cleanupPeriodDays": ' + "9" * 5000 + "}",  # json.loads itself raises ValueError
    ],
    ids=["1e999", "NaN", "-Infinity", "5000-digits"],
)
def test_retention_setting_inf_nan_is_none(tmp_path: Path, raw: str) -> None:
    from c_lord.retention import _read_setting

    p = tmp_path / "settings.json"
    p.write_text(raw)
    assert _read_setting(p) is None


@pytest.mark.parametrize(
    "raw",
    ['{"cleanupPeriodDays": 99999999999999999999}', '{"cleanupPeriodDays": 1e300}'],
    ids=["20-digits", "1e300"],
)
async def test_huge_finite_retention_does_not_break_the_sweep(tmp_path: Path, raw: str) -> None:
    """#1240 (review): a finite but huge value was returned as is, and the startup
    sweep's ``cleanup_old(days=…)`` raised ``OverflowError`` in SQLite — the bot did
    not start. A huge keep-period means "keep": it is capped, never refused (None
    would fall back to the default and sweep *sooner* than the setting asked)."""
    from c_lord.database.models import init_db
    from c_lord.database.repository import SessionRepository
    from c_lord.retention import MAX_RETENTION_DAYS, _read_setting

    p = tmp_path / "settings.json"
    p.write_text(raw)
    days = _read_setting(p)
    assert days == MAX_RETENTION_DAYS
    db = str(tmp_path / "s.db")
    await init_db(db)
    assert await SessionRepository(db).cleanup_old(days=days) == []
