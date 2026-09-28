"""Issue #773: the mirror must find its transcript without an invisible marker.

#627 decided ownership by looking for c-lord's zero-width-space prefix inside
the transcript.  Claude Code 2.1.278 started **stripping** that character from
interactive input before writing the transcript ("Removed 1 invisible character
from the launch prompt before sending it"), so from 2026-09-20 every thread's
jsonl read as "somebody else's conversation" and the mirror posted nothing.
Seventeen production threads ran a full day's work into a silent Discord.

The replacement does not look inside the file at all: c-lord passes
``--session-id <uuid>`` when it starts Claude, so it **names its own
transcript**, and records that name next to it.  A flag the CLI documents
cannot be sanitised away by the CLI's input handling.

These tests pin:

* the claim round-trips and only accepts a well-formed session id;
* a transcript with **no marker at all** is resolved when it is the claimed one
  (the #773 regression);
* a ``claude -p`` sub-invocation in the same working copy is still refused
  (#627 must not come back);
* the claim wins over a marked leftover from an earlier session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from c_lord.transcript.claim import (
    CLAIM_FILENAME,
    claimed_transcript,
    new_session_id,
    read_claim,
    write_claim,
)
from c_lord.transcript.resolver import ThreadSessionResolver, is_clord_driven_jsonl

ZWSP = "​"


def _plain_user_line(text: str, uuid: str, session_id: str = "s") -> str:
    """A user event as Claude Code 2.1.278+ stores it: the ZWSP is gone."""
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "sessionId": session_id,
            "message": {"role": "user", "content": text},
        },
        ensure_ascii=False,
    )


def _marked_user_line(text: str, uuid: str) -> str:
    """A user event as Claude Code <= 2.1.275 stored it — marker intact."""
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "sessionId": "s",
            "message": {"role": "user", "content": ZWSP + text},
        },
        ensure_ascii=False,
    )


def _write(path: Path, *lines: str, mtime: float | None = None) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ── The claim file ───────────────────────────────────────────────────────


def test_new_session_id_is_a_uuid_the_cli_accepts() -> None:
    sid = new_session_id()
    assert len(sid) == 36
    assert sid.count("-") == 4
    assert sid != new_session_id()


def test_claim_round_trips(tmp_path: Path) -> None:
    sid = new_session_id()
    assert write_claim(tmp_path, sid) is True
    assert (tmp_path / CLAIM_FILENAME).is_file()
    assert read_claim(tmp_path) == sid


def test_claim_is_written_even_when_the_project_dir_does_not_exist_yet(
    tmp_path: Path,
) -> None:
    """Claude Code creates the project dir lazily, on its first write.

    c-lord claims the transcript *before* starting Claude — that is the whole
    point of naming it — so the directory routinely does not exist yet.
    """
    project_dir = tmp_path / "-home-u-repo"
    sid = new_session_id()
    assert write_claim(project_dir, sid) is True
    assert read_claim(project_dir) == sid


def test_a_corrupt_claim_reads_as_no_claim(tmp_path: Path) -> None:
    """Garbage must not become a filename we go looking for."""
    (tmp_path / CLAIM_FILENAME).write_text("../../etc/passwd\n", encoding="utf-8")
    assert read_claim(tmp_path) is None
    (tmp_path / CLAIM_FILENAME).write_text("", encoding="utf-8")
    assert read_claim(tmp_path) is None


def test_claimed_transcript_is_none_until_claude_writes_it(tmp_path: Path) -> None:
    sid = new_session_id()
    write_claim(tmp_path, sid)
    assert claimed_transcript(tmp_path) is None
    jsonl = _write(tmp_path / f"{sid}.jsonl", _plain_user_line("やって", "u1"))
    assert claimed_transcript(tmp_path) == jsonl


# ── The resolver ─────────────────────────────────────────────────────────


def test_resolves_a_transcript_that_carries_no_marker_at_all(tmp_path: Path) -> None:
    """#773 RED: this is exactly what CLI 2.1.280 writes, and it went unread.

    Nothing in the file identifies it — the identification is its *name*.
    """
    sid = new_session_id()
    write_claim(tmp_path, sid)
    ours = _write(tmp_path / f"{sid}.jsonl", _plain_user_line("やって", "u1"))
    assert is_clord_driven_jsonl(ours) is False, "precondition: the old rule finds no marker"

    assert ThreadSessionResolver(tmp_path).resolve() == ours


def test_a_sub_invocation_is_still_refused(tmp_path: Path) -> None:
    """#627 must not come back: only the claimed name (or the marker) is ours.

    A ``claude -p`` started by this thread's own Claude writes into the same
    project dir and is often the mtime-latest.  It carries neither the claim's
    name nor the marker.
    """
    sid = new_session_id()
    write_claim(tmp_path, sid)
    ours = _write(tmp_path / f"{sid}.jsonl", _plain_user_line("やって", "u1"), mtime=1000)
    _write(
        tmp_path / "99999999-0000-0000-0000-000000000000.jsonl",
        _plain_user_line("sub", "u2"),
        mtime=2000,
    )

    assert ThreadSessionResolver(tmp_path).resolve() == ours


def test_nothing_is_resolved_when_only_a_sub_invocation_exists(tmp_path: Path) -> None:
    """No claim, no marker → post nothing (#627 rule 3) rather than guess."""
    _write(tmp_path / "99999999-0000-0000-0000-000000000000.jsonl", _plain_user_line("sub", "u2"))
    assert ThreadSessionResolver(tmp_path).resolve() is None


def test_the_claim_wins_over_a_marked_transcript_from_an_earlier_session(
    tmp_path: Path,
) -> None:
    """A thread that ran on 2.1.275 and was then restarted on 2.1.280.

    The old transcript still carries the marker and may even be the newer file
    on disk (a resume, a backup tool, an editor touches it).  The session
    c-lord just started is the one the reader is waiting on.
    """
    old = _write(
        tmp_path / "11111111-0000-0000-0000-000000000000.jsonl",
        _marked_user_line("前のセッション", "u1"),
        mtime=5000,
    )
    sid = new_session_id()
    write_claim(tmp_path, sid)
    ours = _write(tmp_path / f"{sid}.jsonl", _plain_user_line("いまのセッション", "u2"), mtime=1000)
    assert is_clord_driven_jsonl(old) is True

    assert ThreadSessionResolver(tmp_path).resolve() == ours


def test_falls_back_to_the_marker_while_the_claimed_file_does_not_exist(
    tmp_path: Path,
) -> None:
    """Claude takes seconds to write its first line; the thread is not blind meanwhile.

    A session that predates #773 (no claim of its own) must keep working, so the
    marker rule stays as the fallback.
    """
    marked = _write(
        tmp_path / "11111111-0000-0000-0000-000000000000.jsonl",
        _marked_user_line("前のターン", "u1"),
    )
    write_claim(tmp_path, new_session_id())

    resolver = ThreadSessionResolver(tmp_path)
    assert resolver.resolve() == marked


def test_follows_the_claim_when_it_is_rewritten_by_a_restart(tmp_path: Path) -> None:
    """``/clear`` and ``/claude-restart`` start a new session — and a new claim."""
    first = new_session_id()
    write_claim(tmp_path, first)
    a = _write(tmp_path / f"{first}.jsonl", _plain_user_line("一回目", "u1"), mtime=1000)
    resolver = ThreadSessionResolver(tmp_path)
    assert resolver.resolve() == a

    second = new_session_id()
    write_claim(tmp_path, second)
    b = _write(tmp_path / f"{second}.jsonl", _plain_user_line("二回目", "u2"), mtime=2000)
    assert resolver.resolve() == b
