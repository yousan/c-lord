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
