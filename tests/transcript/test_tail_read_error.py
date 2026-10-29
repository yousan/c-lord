"""#987: a transient read error must not make the tail re-deliver what it read.

``_poll_once`` drops ``current_path`` when ``stat()`` or ``open()`` fails (the
file vanished for a moment, EMFILE with a few hundred mirrors). It used to do
so without banking the read position, so the next poll re-adopted the file at
the offset the *tail started* with and read everything since then again. uuid
events are deduplicated; uuid-less ones — the ``api-request-blob`` copies whose
tool blocks wake the progress line (#906) — came out a second time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

from c_lord.transcript import tail
from c_lord.transcript.claim import write_claim

SESSION = "0b4f3c2e-6d1a-4c8e-9f57-2a1b3c4d5e6f"


def _api_request_blob(tool_id: str) -> dict:
    """CLI 2.1.29x+'s copy of the API request — no uuid, but tool blocks inside (#906)."""
    return {
        "type": "api-request-blob",
        "sessionId": SESSION,
        "hash": "h",
        "message": {
            "role": "user",
            "content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {}}],
        },
    }


def _append(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _started_tail(tmp_path: Path) -> tuple[Path, tail._FollowState]:
    write_claim(tmp_path, SESSION)
    jsonl = tmp_path / f"{SESSION}.jsonl"
    jsonl.write_text(json.dumps({"type": "user", "uuid": "u0"}) + "\n")
    state = tail._open_initial_state(tmp_path, False)
    _append(jsonl, _api_request_blob("toolu_1"))
    assert [e["type"] for e in tail._poll_once(state)] == ["api-request-blob"]
    _append(jsonl, {"type": "assistant", "uuid": "u2"})
    return jsonl, state


def test_open_error_does_not_replay_uuidless_events(tmp_path: Path) -> None:
    jsonl, state = _started_tail(tmp_path)
    real_open = Path.open

    def emfile(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if self == jsonl and args and args[0] == "r":
            raise OSError(24, "Too many open files")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    with mock.patch.object(Path, "open", emfile):
        assert tail._poll_once(state) == []

    assert [e["type"] for e in tail._poll_once(state)] == ["assistant"]


def test_vanished_file_that_returns_does_not_replay_uuidless_events(tmp_path: Path) -> None:
    jsonl, state = _started_tail(tmp_path)
    real_stat = Path.stat
    calls = {"n": 0}

    def gone_once(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        # Only the follow loop's own stat fails, once: the file vanished between
        # the resolver picking it and ``_poll_once`` reading its size.
        caller = sys._getframe(1).f_code.co_name
        if self == jsonl and caller == "_poll_once" and calls["n"] == 0:
            calls["n"] += 1
            raise FileNotFoundError(2, "No such file or directory")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    with mock.patch.object(Path, "stat", gone_once):
        assert tail._poll_once(state) == []
    assert calls["n"] == 1

    assert [e["type"] for e in tail._poll_once(state)] == ["assistant"]


def test_open_error_mid_line_still_delivers_that_line_once(tmp_path: Path) -> None:
    """#1004: the half line held in the buffer must be read again, not dropped.

    The banked offset already counted the first half; resuming there with an
    empty buffer read only the second half, which does not parse, so the event
    — possibly the reply itself — was lost.
    """
    jsonl, state = _started_tail(tmp_path)
    assert [e["type"] for e in tail._poll_once(state)] == ["assistant"]
    line = json.dumps({"type": "assistant", "uuid": "u3", "message": {"content": "hello"}})
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(line[:20])
    assert tail._poll_once(state) == []
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(line[20:] + "\n")
    real_open = Path.open

    def emfile(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if self == jsonl and args and args[0] == "r":
            raise OSError(24, "Too many open files")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    with mock.patch.object(Path, "open", emfile):
        assert tail._poll_once(state) == []

    assert [e.get("uuid") for e in tail._poll_once(state)] == ["u3"]
    assert tail._poll_once(state) == []
