"""A claim whose transcript never appears gives way to the one c-lord is really driving (#994).

Production, thread 1564444583767703562 (2026-10-29): a resume came up a second
after the verdict called it failed, so the fallback wrote a new claim
(``93b54394``) whose fresh start never ran, while the resumed claude kept
writing ``a07fc39e.jsonl``. With no ``93b54394.jsonl`` ever written, every
mirror since resolved nothing and the thread went silent for hours.

The resolver may now move the claim — but only to a transcript **provably
c-lord's**, never to "whatever is being written" (#627: a ``claude -p`` in the
same working copy is exactly that, and posting its conversation is the worse
bug). Two proofs count:

* the id was claimed by c-lord in this directory before (the claim history);
* a live, interactive claude whose working directory is this project's was
  started with that id (``--resume <id>`` / ``--session-id <id>``).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from c_lord.transcript import resolver as resolver_mod
from c_lord.transcript.claim import (
    claim_path,
    new_session_id,
    read_claim,
    read_claim_history,
    write_claim,
)
from c_lord.transcript.resolver import (
    CLAIM_GRACE_SECONDS,
    ThreadSessionResolver,
    live_session_ids,
)

LIVE = "a07fc39e-4c1c-4b0e-9f43-5f3f3c4c1e01"
ORPHAN = "93b54394-6ded-40bc-96b3-b979349d8f83"


@pytest.fixture(autouse=True)
def _no_live_processes(monkeypatch):
    monkeypatch.setattr(resolver_mod, "live_session_ids", lambda _project: set())


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "-home-u-sessions-1-2"
    project.mkdir()
    return project


def _age_claim(project: Path, seconds: float = CLAIM_GRACE_SECONDS + 5) -> float:
    old = time.time() - seconds
    os.utime(claim_path(project), (old, old))
    return old


def _transcript(project: Path, session_id: str, *, mtime: float | None = None) -> Path:
    path = project / f"{session_id}.jsonl"
    path.write_text('{"type":"user","message":{"content":"x"}}\n', encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_the_claim_history_proves_the_earlier_session(tmp_path: Path, caplog) -> None:
    """The #994 sequence with today's c-lord: claim a07, resume it, fallback claims 93b."""
    project = _project(tmp_path)
    write_claim(project, LIVE)
    write_claim(project, ORPHAN)  # the fallback's claim; its claude never ran
    claimed_at = _age_claim(project)
    live = _transcript(project, LIVE)  # kept growing after the claim
    assert live.stat().st_mtime > claimed_at

    resolver = ThreadSessionResolver(project)
    with caplog.at_level(logging.WARNING, logger="c_lord.transcript.resolver"):
        assert resolver.resolve() == live

    assert read_claim(project) == LIVE, "the claim must name the transcript being written"
    assert "(#994)" in caplog.text


def test_a_live_claude_proves_a_session_the_history_never_saw(tmp_path: Path, monkeypatch) -> None:
    """The production thread itself: its claims predate the history file."""
    project = _project(tmp_path)
    claim_path(project).write_text(f"{ORPHAN}\n", encoding="utf-8")  # no history
    _age_claim(project)
    live = _transcript(project, LIVE)
    monkeypatch.setattr(resolver_mod, "live_session_ids", lambda _project: {LIVE})

    assert ThreadSessionResolver(project).resolve() == live
    assert read_claim(project) == LIVE


def test_an_unproven_transcript_is_never_adopted(tmp_path: Path) -> None:
    """#627: a ``claude -p`` writing in the same working copy is not this thread."""
    project = _project(tmp_path)
    write_claim(project, ORPHAN)
    _age_claim(project)
    _transcript(project, new_session_id())

    assert ThreadSessionResolver(project).resolve() is None
    assert read_claim(project) == ORPHAN


def test_two_proven_candidates_are_not_guessed_between(tmp_path: Path) -> None:
    project = _project(tmp_path)
    other = new_session_id()
    write_claim(project, LIVE)
    write_claim(project, other)
    write_claim(project, ORPHAN)
    _age_claim(project)
    _transcript(project, LIVE)
    _transcript(project, other)

    assert ThreadSessionResolver(project).resolve() is None
    assert read_claim(project) == ORPHAN


def test_a_transcript_not_written_since_the_claim_is_not_followed(tmp_path: Path) -> None:
    """Written before the claim and silent since: that is the past, not the live session."""
    project = _project(tmp_path)
    write_claim(project, LIVE)
    stale = time.time() - 3 * CLAIM_GRACE_SECONDS
    _transcript(project, LIVE, mtime=stale)
    write_claim(project, ORPHAN)
    _age_claim(project, CLAIM_GRACE_SECONDS + 5)

    assert ThreadSessionResolver(project).resolve() is None
    assert read_claim(project) == ORPHAN


def test_within_the_grace_the_claim_is_still_waited_for(tmp_path: Path) -> None:
    """#926: a fresh claim may simply not be written yet."""
    project = _project(tmp_path)
    write_claim(project, LIVE)
    write_claim(project, ORPHAN)
    _transcript(project, LIVE)

    assert ThreadSessionResolver(project).resolve() is None
    assert read_claim(project) == ORPHAN


def test_the_history_is_bounded_and_ordered(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ids = [new_session_id() for _ in range(40)]
    for session_id in ids:
        write_claim(project, session_id)
    history = read_claim_history(project)
    assert history == ids[-len(history) :]
    assert 8 <= len(history) <= 32


class TestLiveSessionIds:
    """Reading ``/proc`` for the claude running in this project's working directory."""

    def _proc(self, root: Path, pid: int, argv: list[str], cwd: Path) -> None:
        entry = root / str(pid)
        entry.mkdir(parents=True)
        (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (entry / "cwd").symlink_to(cwd)

    def test_reads_resume_and_session_id_of_claude_in_this_cwd(self, tmp_path: Path) -> None:
        work = tmp_path / "home" / "u" / "sessions" / "1" / "2"
        work.mkdir(parents=True)
        other = tmp_path / "elsewhere"
        other.mkdir()
        projects = tmp_path / "projects"
        project = resolver_mod.derive_project_dir(str(work), projects_root=projects)
        proc = tmp_path / "proc"
        self._proc(proc, 10, ["claude", "--resume", LIVE, "--model", "opus", "--", "hi"], work)
        self._proc(proc, 11, ["node", "/x/claude", f"--session-id={ORPHAN}"], work)
        # a ``claude -p`` in the same working copy is not the thread's session
        self._proc(proc, 12, ["claude", "-p", "--session-id", new_session_id()], work)
        # the same id in another working copy is another thread's
        self._proc(proc, 13, ["claude", "--resume", new_session_id()], other)
        self._proc(proc, 14, ["zsh"], work)

        assert live_session_ids(project, proc_root=proc) == {LIVE, ORPHAN}

    def test_no_proc_is_no_proof(self, tmp_path: Path) -> None:
        assert live_session_ids(tmp_path / "p", proc_root=tmp_path / "missing") == set()
