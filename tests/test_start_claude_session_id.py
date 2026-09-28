"""``start_claude`` names the session it starts, and records the name (#773).

The mirror used to recognise this thread's transcript by a zero-width space
inside it.  Claude Code 2.1.278 strips that character from interactive input
("Removed 1 invisible character from the launch prompt before sending it"), so
every thread's transcript became unrecognisable and the whole fleet delivered
nothing for three days.

The fix is to stop reading ownership out of the file: c-lord passes
``--session-id <uuid>``, so the CLI writes the session to ``<uuid>.jsonl`` — a
name c-lord picked — and :mod:`c_lord.transcript.claim` records it beside the
transcripts for the mirror to read.

Verified against the real CLI on 2026-09-23 (2.1.280, isolated tmux socket):
the transcript appeared as ``<the uuid we passed>.jsonl`` and contained **zero**
zero-width spaces.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager
from c_lord.transcript.claim import is_session_id, read_claim, write_claim
from c_lord.transcript.resolver import derive_project_dir

PANE_PATH = "/home/u/c-lord-sessions/1/2"


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]
    return mgr


def _project_dir(home: Path) -> Path:
    return derive_project_dir(PANE_PATH, projects_root=home / ".claude" / "projects")


def _start(projects_root: Path, **kwargs) -> str:
    """Run ``start_claude`` against a fake tmux and return the typed command.

    ``projects_root`` stands in for ``$HOME`` so the claim lands under ``tmp_path``.
    """
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        if "display-message" in args:
            return MagicMock(returncode=0, stdout=f"{PANE_PATH}\n")
        return MagicMock(returncode=0, stdout="")

    with (
        patch("c_lord.tmux._run", side_effect=fake_run),
        patch("pathlib.Path.home", return_value=projects_root),
    ):
        assert _mgr().start_claude(12345, "やって", "sonnet", **kwargs) is True
    cmd = "".join(c[-1] for c in calls if "send-keys" in c and "-l" in c)
    for match in re.finditer(r'"\$\(cat (\S+)\)"', cmd):
        Path(match.group(1)).unlink(missing_ok=True)
    return cmd


def _session_id_on(cmd: str) -> str | None:
    match = re.search(r"--session-id (\S+)", cmd)
    return match.group(1) if match else None


def test_a_cold_start_names_its_own_session(tmp_path: Path) -> None:
    cmd = _start(tmp_path)
    session_id = _session_id_on(cmd)
    assert session_id is not None, f"--session-id missing from: {cmd!r}"
    assert is_session_id(session_id), session_id


def test_the_name_is_recorded_where_the_mirror_looks(tmp_path: Path) -> None:
    """The claim has to be readable from the project dir alone — that is all the
    resolver has."""
    cmd = _start(tmp_path)
    project_dir = _project_dir(tmp_path)
    assert read_claim(project_dir) == _session_id_on(cmd)


def test_every_start_gets_a_fresh_name(tmp_path: Path) -> None:
    """Reusing a session id would make ``--session-id`` *resume* it, so ``/clear``
    would quietly recover the conversation it just threw away."""
    assert _session_id_on(_start(tmp_path)) != _session_id_on(_start(tmp_path))


def test_a_resume_keeps_the_claimed_session_instead_of_forking_it(tmp_path: Path) -> None:
    """The restart-resume path must land back in the transcript we already claim.

    ``--continue`` picks whatever wrote last in the working copy, which may be a
    ``claude -p`` sub-invocation (#627's hazard in a new place).  ``--resume
    <claimed id>`` is deterministic and appends to the very file the mirror is
    already following — verified against CLI 2.1.280.
    """
    project_dir = _project_dir(tmp_path)
    write_claim(project_dir, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    cmd = _start(tmp_path, try_continue=True)

    assert "--resume aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in cmd, cmd
    assert "--continue" not in cmd
    assert "--session-id" not in cmd, "--session-id with --resume needs --fork-session; do not fork"
    assert read_claim(project_dir) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_a_resume_with_nothing_claimed_still_uses_continue(tmp_path: Path) -> None:
    """Sessions that started before #773 have no claim — they must still resume."""
    cmd = _start(tmp_path, try_continue=True)
    assert "--continue" in cmd
    assert "--resume" not in cmd
