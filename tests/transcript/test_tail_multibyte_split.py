"""A line half-written inside a multi-byte character arrives intact (#1044).

The tail used to read in text mode with ``errors="replace"``: the one or two
bytes of ``返`` already on disk became U+FFFD, sat in the buffer, and were glued
to the rest of the line once it was finished — the reply arrived as
``���事です``.  The buffer also measured itself in characters while the read
position counted bytes, so the position banked after a failed read could land
before the line (or below zero).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from c_lord.transcript import tail
from c_lord.transcript.claim import write_claim

SESSION = "0b4f3c2e-6d1a-4c8e-9f57-2a1b3c4d5e6f"


def _setup(tmp_path: Path) -> tuple[Path, tail._FollowState]:
    write_claim(tmp_path, SESSION)
    jsonl = tmp_path / f"{SESSION}.jsonl"
    jsonl.write_bytes((json.dumps({"type": "user", "uuid": "u0"}) + "\n").encode())
    return jsonl, tail._open_initial_state(tmp_path, False)


def _reply_line() -> bytes:
    return json.dumps(
        {"type": "assistant", "uuid": "u3", "message": "返事です"}, ensure_ascii=False
    ).encode()


def _append(jsonl: Path, data: bytes) -> None:
    with jsonl.open("ab") as fh:
        fh.write(data)


def test_line_split_inside_a_character_is_not_mangled(tmp_path: Path) -> None:
    jsonl, state = _setup(tmp_path)
    line = _reply_line()
    cut = line.index("返".encode()) + 1
    _append(jsonl, line[:cut])
    assert tail._poll_once(state) == []
    _append(jsonl, line[cut:] + b"\n")
    got = tail._poll_once(state)
    assert [e["message"] for e in got] == ["返事です"]


def test_every_cut_inside_the_line_arrives_intact(tmp_path: Path) -> None:
    """Whatever byte the writer stops at, the finished line reads the same."""
    line = _reply_line()
    for cut in range(1, len(line)):
        sub = tmp_path / f"cut{cut}"
        sub.mkdir()
        jsonl, state = _setup(sub)
        _append(jsonl, line[:cut])
        tail._poll_once(state)
        _append(jsonl, line[cut:] + b"\n")
        got = tail._poll_once(state)
        assert [e["message"] for e in got] == ["返事です"], cut


def test_banked_offset_is_in_bytes_at_the_line_start(tmp_path: Path) -> None:
    """After a failed read the bank points at the half line's first byte (AC2)."""
    jsonl, state = _setup(tmp_path)
    line_start = jsonl.stat().st_size
    line = _reply_line()
    cut = line.index("事".encode()) + 2  # inside 事, after a whole 返
    _append(jsonl, line[:cut])
    assert tail._poll_once(state) == []
    assert state.offset == line_start + cut

    # The next read fails: the tail lets go of the file and banks its place.
    real_open = Path.open

    def failing(self: Path, *args: object, **kwargs: object):
        if self == jsonl and args and args[0] in ("r", "rb"):
            raise OSError(24, "Too many open files")
        return real_open(self, *args, **kwargs)

    _append(jsonl, line[cut : cut + 1])  # more to read, so the next poll reads
    with mock.patch.object(Path, "open", failing):
        assert tail._poll_once(state) == []
    assert state.offsets[jsonl] == line_start

    _append(jsonl, line[cut + 1 :] + b"\n")
    got = tail._poll_once(state)
    assert [e["message"] for e in got] == ["返事です"]
