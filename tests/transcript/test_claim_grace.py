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


def test_a_new_empty_directory_waits_for_the_claim_too(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#926 reopened: the mirror starts before ``start_claude`` writes the claim.

    ``write_claim`` makes the project dir and then moves the claim in; a resolve
    in between saw an empty dir with no claim and warned at once — prod
    2026-10-29 09:28:13, the same second the claim landed. RED before the fix.
    """
    project = _project(tmp_path)
    resolver = ThreadSessionResolver(project)
    with caplog.at_level(logging.INFO, logger="c_lord.transcript.resolver"):
        assert resolver.resolve() is None
        write_claim(project, new_session_id())
        assert resolver.resolve() is None
    assert _warnings(caplog) == []


def test_an_old_empty_directory_without_a_claim_warns_at_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing named and nothing coming: the #773 warning is unchanged."""
    project = _project(tmp_path)
    old = time.time() - CLAIM_GRACE_SECONDS - 5
    os.utime(project, (old, old))
    resolver = ThreadSessionResolver(project)
    with caplog.at_level(logging.WARNING, logger="c_lord.transcript.resolver"):
        assert resolver.resolve() is None
    assert len(_warnings(caplog)) == 1


def test_without_a_claim_other_transcripts_still_warn_at_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A dir holding someone else's transcripts is the #773 case, young or not."""
    project = _project(tmp_path)
    (project / "someone-else.jsonl").write_text('{"type":"user"}\n')
    resolver = ThreadSessionResolver(project)
    with caplog.at_level(logging.WARNING, logger="c_lord.transcript.resolver"):
        assert resolver.resolve() is None
    assert len(_warnings(caplog)) == 1
