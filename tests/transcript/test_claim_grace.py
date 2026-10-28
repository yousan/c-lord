"""#926: a transcript c-lord named but Claude has not written yet is not "lost".

``start_claude`` names the transcript (``--session-id``) and the mirror starts
looking for it at once — but the CLI writes nothing until the folder-trust
dialog has been answered, a few seconds later. In that window the resolver
logged ``no c-lord-driven transcript … posting nothing`` at WARNING on the first
turn of every new workspace (18 of 18 in a week), burying the one WARNING that
means a thread really is not being forwarded (#773).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from c_lord.transcript.claim import claim_path, new_session_id, write_claim
from c_lord.transcript.resolver import CLAIM_GRACE_SECONDS, ThreadSessionResolver

_WARNING = "no c-lord-driven transcript"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    return project


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.levelno >= logging.WARNING and _WARNING in r.getMessage()
    ]


def test_freshly_claimed_transcript_not_yet_written_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = _project(tmp_path)
    write_claim(project, new_session_id())
    resolver = ThreadSessionResolver(project)
    with caplog.at_level(logging.INFO, logger="c_lord.transcript.resolver"):
        for _ in range(3):
            assert resolver.resolve() is None
    assert _warnings(caplog) == []
    # The wait itself is still visible in normal operation (#678), once.
    waiting = [r for r in caplog.records if "not written yet" in r.getMessage()]
    assert len(waiting) == 1


def test_claimed_transcript_that_never_appears_warns_after_the_grace(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = _project(tmp_path)
    write_claim(project, new_session_id())
    old = time.time() - CLAIM_GRACE_SECONDS - 5
    os.utime(claim_path(project), (old, old))
    resolver = ThreadSessionResolver(project)
    with caplog.at_level(logging.WARNING, logger="c_lord.transcript.resolver"):
        for _ in range(3):
            assert resolver.resolve() is None
    assert len(_warnings(caplog)) == 1


def test_warning_arrives_once_the_grace_runs_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = _project(tmp_path)
    write_claim(project, new_session_id())
    resolver = ThreadSessionResolver(project)
    with caplog.at_level(logging.WARNING, logger="c_lord.transcript.resolver"):
        assert resolver.resolve() is None
        assert _warnings(caplog) == []
        old = time.time() - CLAIM_GRACE_SECONDS - 5
        os.utime(claim_path(project), (old, old))
        assert resolver.resolve() is None
    assert len(_warnings(caplog)) == 1


def test_without_a_claim_the_warning_is_immediate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing was named, so there is nothing to wait for (#773 unchanged)."""
    project = _project(tmp_path)
    resolver = ThreadSessionResolver(project)
    with caplog.at_level(logging.WARNING, logger="c_lord.transcript.resolver"):
        assert resolver.resolve() is None
    assert len(_warnings(caplog)) == 1
