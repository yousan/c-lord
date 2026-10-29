"""#1059: banking the read position must never go negative.

The tail reads in text mode with ``errors="replace"`` (until #1049), so a first
line stopped inside a multi-byte character sits in the buffer as U+FFFD — three
bytes for the one or two actually read. ``_drop_current`` banked
``offset - len(buffer.encode())``, which then came out as ``-1`` / ``-2``; the
next read did ``f.read(<negative>)`` / ``f.seek(-1)``, raised ``ValueError``
(not ``OSError``) and ended ``tail_events`` — the thread went silent with
nothing in the log.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import mock

from c_lord.transcript import tail
from c_lord.transcript.claim import write_claim

SESSION = "0b4f3c2e-6d1a-4c8e-9f57-2a1b3c4d5e6f"
LINE = '{"type":"user","uuid":"u1","message":"あ"}'.encode()
CUT = LINE.index("あ".encode()) + 1  # one byte into "あ"


async def test_tail_events_survives_open_error_on_first_line_split_inside_char(
    tmp_path: Path,
) -> None:
    write_claim(tmp_path, SESSION)
    jsonl = tmp_path / f"{SESSION}.jsonl"
    jsonl.write_bytes(b"")
    real_open = Path.open
    fail = {"on": False}

    def maybe_emfile(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        # Only the follow loop's own read fails (not ``_signature``'s), once.
        caller = sys._getframe(1).f_code.co_name
        if fail["on"] and self == jsonl and caller == "_poll_once":
            fail["on"] = False
            raise OSError(24, "Too many open files")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    got: list[dict] = []

    async def consume() -> None:
        async for ev in tail.tail_events(tmp_path, poll_interval=0.01):
            got.append(ev)

    with mock.patch.object(Path, "open", maybe_emfile):
        task = asyncio.create_task(consume())
        try:
            await asyncio.sleep(0.1)
            with real_open(jsonl, "ab") as fh:
                fh.write(LINE[:CUT])
            await asyncio.sleep(0.1)
            with real_open(jsonl, "ab") as fh:
                fh.write(LINE[CUT:] + b"\n")
            fail["on"] = True
            await asyncio.sleep(0.3)
            assert not task.done(), f"tail_events ended: {task.exception()!r}"
            assert [e.get("uuid") for e in got] == ["u1"]
        finally:
            task.cancel()


def test_switching_away_with_first_line_split_inside_char_and_back(tmp_path: Path) -> None:
    """Switch away holding a half first line, then back: no ValueError (AC2).

    Green on main by construction — main's switch path banks ``state.offset``,
    not the buffer start. It guards #1002, which copies the #1004 formula into
    this path; that the half line is then *delivered* is #1002's own test.
    """
    write_claim(tmp_path, SESSION)
    jsonl = tmp_path / f"{SESSION}.jsonl"
    other = tmp_path / "other.jsonl"
    jsonl.write_bytes(b"")
    other.write_bytes(b"")
    state = tail._open_initial_state(tmp_path, False)
    assert tail._poll_once(state) == []
    with jsonl.open("ab") as fh:
        fh.write(LINE[:CUT])
    assert tail._poll_once(state) == []
    assert state.current_path == jsonl

    real_resolve = state.resolver.resolve
    with mock.patch.object(state.resolver, "resolve", return_value=other):
        assert tail._poll_once(state) == []
    assert all(v >= 0 for v in state.offsets.values()), state.offsets

    with jsonl.open("ab") as fh:
        fh.write(LINE[CUT:] + b"\n")
    with mock.patch.object(state.resolver, "resolve", real_resolve):
        tail._poll_once(state)  # must not raise ValueError
