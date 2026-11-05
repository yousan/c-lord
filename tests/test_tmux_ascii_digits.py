"""#1239: a ``²`` in a window name, a thread tag or the mapping file broke the window map.

``"²".isdigit()`` is True but ``int("²")`` raises. The tmux server is shared, so a
person renaming one window to ``w²`` (or a stray ``@thread_id`` tag) made rebuilding
the thread -> window map raise, and threads could not find their own windows. Only
ASCII digits count now (as for the env IDs, #1218); anything else is not a c-lord
window / row and is skipped.
"""

from __future__ import annotations

import json
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
