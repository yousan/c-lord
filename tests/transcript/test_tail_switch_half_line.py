"""Switching the file a tail reads must not drop a half-written line (#1043).

Same shape as #988 / #1004, on the other place the read position is banked:
switching to another transcript saved the position *after* a half-read line and
threw the half away, so coming back read only its second half, which does not
parse — and the event (possibly the reply itself) was lost. #1002 makes such
switches more frequent (a claim can now move to the transcript really written).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from c_lord.transcript import tail
from c_lord.transcript.claim import write_claim

SESSION = "0b4f3c2e-6d1a-4c8e-9f57-2a1b3c4d5e6f"


def test_file_switch_with_half_line_then_back(tmp_path: Path) -> None:
    write_claim(tmp_path, SESSION)
    a = tmp_path / f"{SESSION}.jsonl"
    a.write_text(json.dumps({"type": "user", "uuid": "u0"}) + "\n")
    state = tail._open_initial_state(tmp_path, False)
    line = json.dumps({"type": "assistant", "uuid": "uA", "message": "hello"})
    with a.open("a") as fh:
        fh.write(line[:20])
    assert tail._poll_once(state) == []
    with a.open("a") as fh:
        fh.write(line[20:] + "\n")
    b = tmp_path / "other.jsonl"
    with mock.patch.object(state.resolver, "resolve", return_value=b):
        b.write_text("")
        tail._poll_once(state)
    got = tail._poll_once(state)
    assert [e.get("uuid") for e in got] == ["uA"]


def test_a_multibyte_half_line_is_banked_by_bytes(tmp_path: Path) -> None:
    """The bank is a byte offset; Japanese text must not shift it."""
    write_claim(tmp_path, SESSION)
    a = tmp_path / f"{SESSION}.jsonl"
    a.write_text(json.dumps({"type": "user", "uuid": "u0"}) + "\n")
    state = tail._open_initial_state(tmp_path, False)
    line = json.dumps({"type": "assistant", "uuid": "uJ", "message": "返事の本文"}, ensure_ascii=False)
    cut = line.index("本")
    with a.open("a", encoding="utf-8") as fh:
        fh.write(line[:cut])
    assert tail._poll_once(state) == []
    with a.open("a", encoding="utf-8") as fh:
        fh.write(line[cut:] + "\n")
    b = tmp_path / "other.jsonl"
    with mock.patch.object(state.resolver, "resolve", return_value=b):
        b.write_text("")
        tail._poll_once(state)
    got = tail._poll_once(state)
    assert [e.get("uuid") for e in got] == ["uJ"]
    assert got[0]["message"] == "返事の本文"
