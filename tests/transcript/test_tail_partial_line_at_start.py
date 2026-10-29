"""#988: a record half-written when the tail starts is delivered once it is whole.

``from_start=False`` skips what is already on disk by starting at the file's
size. When Claude Code is in the middle of appending a line at that moment
(a mirror restored on bot restart mid-turn), that size lies *inside* the line:
the follow loop then read only its second half, failed to parse it and dropped
the record — although ``_seed_seen_uuids`` promised the partial line "is read
afresh by the follow loop". The start is now the end of the last whole line.
"""

from __future__ import annotations

import json
from pathlib import Path

from c_lord.transcript import tail
from c_lord.transcript.claim import write_claim

SESSION = "7d3e1f20-5b9c-4a6d-8e21-0c9b8a7f6e5d"

DONE = json.dumps({"type": "user", "uuid": "u0"}) + "\n"
REPLY = (
    json.dumps(
        {
            "type": "assistant",
            "uuid": "u-reply",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 200}]},
        }
    )
    + "\n"
)


def _transcript(tmp_path: Path, content: str) -> Path:
    write_claim(tmp_path, SESSION)
    jsonl = tmp_path / f"{SESSION}.jsonl"
    jsonl.write_text(content)
    return jsonl


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def test_line_half_written_at_start_is_delivered_when_finished(tmp_path: Path) -> None:
    jsonl = _transcript(tmp_path, DONE + REPLY[:60])
    state = tail._open_initial_state(tmp_path, False)
    assert tail._poll_once(state) == []

    _append(jsonl, REPLY[60:])

    assert [e.get("uuid") for e in tail._poll_once(state)] == ["u-reply"]


def test_lines_finished_before_start_are_still_not_delivered(tmp_path: Path) -> None:
    jsonl = _transcript(tmp_path, DONE + REPLY)
    state = tail._open_initial_state(tmp_path, False)
    assert tail._poll_once(state) == []

    _append(jsonl, json.dumps({"type": "assistant", "uuid": "u-new"}) + "\n")

    assert [e.get("uuid") for e in tail._poll_once(state)] == ["u-new"]


def test_pre_existing_file_adopted_later_keeps_its_half_written_line(tmp_path: Path) -> None:
    """Same rule for a transcript that existed at start but is adopted later (#627)."""
    write_claim(tmp_path, "00000000-0000-4000-8000-000000000000")  # another session first
    jsonl = tmp_path / f"{SESSION}.jsonl"
    jsonl.write_text(DONE + REPLY[:60])
    state = tail._open_initial_state(tmp_path, False)
    assert tail._poll_once(state) == []

    write_claim(tmp_path, SESSION)
    _append(jsonl, REPLY[60:])

    assert [e.get("uuid") for e in tail._poll_once(state)] == ["u-reply"]


def test_a_line_longer_than_one_backward_read_is_found_whole(tmp_path: Path) -> None:
    big = (
        json.dumps({"type": "assistant", "uuid": "u-big", "message": {"content": "y" * 300_000}})
        + "\n"
    )
    jsonl = _transcript(tmp_path, DONE + big[:250_000])
    state = tail._open_initial_state(tmp_path, False)
    assert tail._poll_once(state) == []

    _append(jsonl, big[250_000:])

    assert [e.get("uuid") for e in tail._poll_once(state)] == ["u-big"]
