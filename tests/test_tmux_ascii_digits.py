"""#1239: a ``²`` in a window name, a thread tag or the mapping file broke the window map.

``"²".isdigit()`` is True but ``int("²")`` raises. The tmux server is shared, so a
person renaming one window to ``w²`` (or a stray ``@thread_id`` tag) made rebuilding
the thread -> window map raise, and threads could not find their own windows. Only
ASCII digits count now (as for the env IDs, #1218); anything else is not a c-lord
window / row and is skipped.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import c_lord.tmux as T


def test_parse_work_number_superscript() -> None:
    """A window renamed to ``w²`` on the shared tmux server."""
    assert T.parse_work_number("w²") is None


def _lw(stdout: str) -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    "line",
    ["@1\tw²\t\t/x", "@1\tw1\t²\t/x"],
    ids=["window-name", "thread-tag"],
)
def test_rebuild_mapping_survives_superscript(line: str) -> None:
    mgr = T.TmuxSessionManager(session_name="clord", mapping_path="")
    with patch("c_lord.tmux._run", return_value=_lw(line + "\n")):
        mgr._rebuild_mapping()  # must not raise ValueError


def test_mapping_file_superscript_key(tmp_path: Path) -> None:
    """The thread -> window-name file c-lord writes itself (#113)."""
    p = tmp_path / "clord.json"
    p.write_text(json.dumps({"²": "w1", "5": "w2"}))
    mgr = T.TmuxSessionManager(session_name="clord", mapping_path=str(p))
    target: dict[int, str] = {}
    with patch.object(mgr, "_window_names", return_value={}):
        mgr._load_from_mapping_file(target)  # must not raise


# #1287: ASCII digits, but more than Python's 4300-digit ``int()`` limit. On the
# shared tmux server a person can set a 5000-digit ``@thread_id`` or name a window
# ``w`` + 5000 digits; ``int()`` raised ValueError and the window map could not be
# rebuilt. A Discord snowflake is at most 20 digits and work numbers are small, so a
# longer value is not a c-lord window / row.

HUGE = "9" * 5000


def test_window_number_survives_huge_suffix() -> None:
    assert T.parse_work_number("w" + HUGE) is None
    assert T.parse_work_number("work" + HUGE) is None


def test_claude_panes_listing_survives_huge_tag() -> None:
    out = f"{HUGE}\tclaude\t%1\n5\tclaude\t%2\n"
    done = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
    with (
        patch.object(T, "_tmux_available", return_value=True),
        patch.object(T, "_run", return_value=done),
    ):
        panes = T._claude_panes_listing()
    assert panes == {5: "%2"}


@pytest.mark.parametrize(
    "line",
    [f"@1\tw{HUGE}\t\t/x", f"@1\tw1\t{HUGE}\t/x", f"@1\tw1\t\t/s/{HUGE}"],
    ids=["window-name", "thread-tag", "pane-path"],
)
def test_rebuild_mapping_survives_huge_number(line: str) -> None:
    mgr = T.TmuxSessionManager(session_name="clord", mapping_path="")
    with patch("c_lord.tmux._run", return_value=_lw(line + "\n")):
        mgr._rebuild_mapping()  # must not raise ValueError


def test_mapping_file_huge_key(tmp_path: Path) -> None:
    p = tmp_path / "clord.json"
    p.write_text(json.dumps({HUGE: "w1", "5": "w2"}))
    mgr = T.TmuxSessionManager(session_name="clord", mapping_path=str(p))
    target: dict[int, str] = {}
    with patch.object(mgr, "_window_names", return_value={"w2": "@2"}):
        mgr._load_from_mapping_file(target)  # must not raise


def test_twenty_digit_snowflake_still_counts() -> None:
    assert T._is_ascii_number("1" * 20)
    assert not T._is_ascii_number("1" * 21)
