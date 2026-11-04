"""A resume never opens a conversation that does not exist (#1171).

Production thread 1564444583767703562 (``fixtures/prod_logs/i1171_stale_claim.log``):
the claim names ``93b54394…``, a session whose transcript was never written; the
thread's real conversation is ``a07fc39e….jsonl`` beside it. Waking the slept
workspace typed ``claude --resume 93b54394…``, the CLI answered ``No conversation
found``, and c-lord fell back to a fresh start (#123) — Claude would have
answered the next message with the whole PR history forgotten.

A claimed id with no transcript is now replaced by the newest transcript in the
same place (the one ``--continue`` would pick), and the claim is moved there.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c_lord.tmux import TmuxSessionManager
from c_lord.transcript.claim import read_claim, write_claim
from c_lord.transcript.resolver import derive_project_dir

PROD_LOG = (Path(__file__).parent / "fixtures" / "prod_logs" / "i1171_stale_claim.log").read_text()
STALE = re.search(r"\.clord-session\s+(\S+)", PROD_LOG).group(1)  # type: ignore[union-attr]
REAL = re.search(r"(\S+)\.jsonl\s+\d+ bytes", PROD_LOG).group(1)  # type: ignore[union-attr]
PANE_PATH = "/home/yousan/c-lord-sessions/1504670029923352597/1564444583767703562"


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]  # noqa: ARG005
    return mgr


def _project_dir(home: Path) -> Path:
    return derive_project_dir(PANE_PATH, projects_root=home / ".claude" / "projects")


def _resume_cmd(home: Path) -> str:
    """What ``start_claude`` types for the restart-resume of this thread."""
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        if "display-message" in args:
            return MagicMock(returncode=0, stdout=f"{PANE_PATH}\n")
        return MagicMock(returncode=0, stdout="")

    with (
        patch("c_lord.tmux._run", side_effect=fake_run),
        patch("pathlib.Path.home", return_value=home),
    ):
        assert _mgr().start_claude(1564444583767703562, "続き", "opus", try_continue=True)
    cmd = "".join(c[-1] for c in calls if "send-keys" in c and "-l" in c)
    for match in re.finditer(r'"\$\(cat (\S+)\)"', cmd):
        Path(match.group(1)).unlink(missing_ok=True)
    return cmd


def _transcript(project_dir: Path, session_id: str, mtime: float) -> None:
    path = project_dir / f"{session_id}.jsonl"
    path.write_text('{"type":"user"}\n')
    os.utime(path, (mtime, mtime))


@pytest.fixture
def production(tmp_path: Path) -> Path:
    """The thread's project dir as it is in production."""
    project_dir = _project_dir(tmp_path)
    write_claim(project_dir, STALE)
    (project_dir / ".clord-session-history").unlink()  # production has none
    _transcript(project_dir, REAL, 1_761_714_420)  # 2026-10-29 14:07 JST
    return project_dir


def test_the_fixture_is_the_production_shape() -> None:
    assert STALE == "93b54394-6ded-40bc-96b3-b979349d8f83"
    assert REAL == "a07fc39e-f689-4d38-8b24-acdc9150539a"


def test_resumes_the_conversation_that_exists(tmp_path: Path, production: Path) -> None:
    """AC1: not ``--resume 93b54394…`` (No conversation found → fresh start)."""
    cmd = _resume_cmd(tmp_path)
    assert f"--resume {REAL}" in cmd, cmd
    assert STALE not in cmd


def test_the_claim_moves_and_says_so(tmp_path: Path, production: Path, caplog) -> None:  # noqa: ANN001
    """AC2: the mirror follows the resumed file; one WARNING names both ids."""
    with caplog.at_level("WARNING", logger="c_lord.tmux"):
        _resume_cmd(tmp_path)
    assert read_claim(production) == REAL
    (line,) = [r.getMessage() for r in caplog.records if "#1171" in r.getMessage()]
    assert STALE in line and REAL in line


def test_the_newest_of_several_is_chosen(tmp_path: Path, production: Path) -> None:
    older = "11111111-2222-3333-4444-555555555555"
    _transcript(production, older, 1_761_000_000)
    assert f"--resume {REAL}" in _resume_cmd(tmp_path)


def test_a_claim_whose_transcript_exists_is_kept(tmp_path: Path, production: Path) -> None:
    """AC3: unchanged — even when another transcript there is newer."""
    _transcript(production, STALE, 1_761_000_000)  # older than REAL
    assert f"--resume {STALE}" in _resume_cmd(tmp_path)
    assert read_claim(production) == STALE


def test_no_transcript_at_all_keeps_the_claim(tmp_path: Path) -> None:
    """AC4: nothing to resume instead — the claimed id is tried (then the #123 fresh start)."""
    project_dir = _project_dir(tmp_path)
    write_claim(project_dir, STALE)
    assert f"--resume {STALE}" in _resume_cmd(tmp_path)
