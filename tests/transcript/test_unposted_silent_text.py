"""A text that went out to nobody must still reach the thread once (#1224, #1225, #1226).

The mirror adopts "the text before the turn's last tool call" as that turn's answer
(#1125) and carries it across a restart only when `.clord-last-silent` proves the
old process posted it (#1187). Three ways that still went wrong for the user:

- #1224: the answer was written while the bot was down, so nobody posted it. It
  was left to the next restart's rescue — but the rescue only looks at the last
  finished turn, so once the user's next turn ended first, that answer never
  appeared in the thread at all.
- #1225: posting the intermediate text failed (Discord 503), yet the turn end
  still adopted it as the delivered answer and moved the cursor onto it — so it
  never appeared, and the rescue would not post it either.
- #1226: when `.clord-last-silent` could not be written, only a DEBUG line said
  so, and every restart of that thread posted the answer a second time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from c_lord.transcript.mirror import LAST_SILENT_FILENAME, TranscriptMirror
from c_lord.transcript.recovery import final_answer_needs_recovery

from .helpers import clord_transcript
from .test_answer_cursor_replay import (  # noqa: F401
    END,
    THREAD_ID,
    _clean,
    _replay,
    _restart_mid_turn,
    _result,
    _Run,
    _text,
    _tool,
    _user,
    log,
)


def _append(jsonl: Path, *rows: dict) -> None:
    with jsonl.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


async def test_answer_written_while_down_is_lost_once_the_next_turn_ends(tmp_path, log):
    """#1224: '次の再起動の救出に任せる' only works if a restart comes before the next turn ends.

    A (text → tool) written while the bot was down; restart; the turn ends with no
    text after the tool, so A is that turn's answer and nobody posted it. The user
    then sends another request and gets answer B. From then on the rescue looks at
    B's turn only — A never reaches the thread.
    Expected: A is posted at some point. main: never (not live, not by rescue).
    """
    project = tmp_path / "proj"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    _append(jsonl, _user("依頼", "u0"), _text("予約しました", "a1"), _tool("t1", "a2"))
    os.utime(jsonl, (1, 1))
    run = _Run()
    m = TranscriptMirror(
        thread_id=THREAD_ID,
        project_dir=project,
        sink=run.sink,
        reply_sink=run.reply_sink,
        file_sink=run.file_sink,
        reply_cursor_sink=run.cursor_sink,
        poll_interval=0.02,
        idle_flush_seconds=0,
        resumed=True,
    )
    m.start()
    try:
        await asyncio.sleep(0.15)
        _append(jsonl, _result("t1", "u1"), END)
        await asyncio.sleep(0.3)
        # the user's next request and its answer, with the mirror up
        end2 = dict(END, uuid="end-2")
        _append(jsonl, _user("次の依頼", "u2"), _text("次の答えB", "b1"), end2)
        await asyncio.sleep(0.3)
    finally:
        await m.stop()
    posted_texts = [t for _, t in run.posted]
    assert "次の答えB" in posted_texts
    rescue = final_answer_needs_recovery(project, run.cursor[-1] if run.cursor else None)
    delivered_somehow = "予約しました" in posted_texts or (
        rescue is not None and rescue.uuid == "a1"
    )
    assert delivered_somehow, (run.posted, run.cursor, rescue)


async def test_answer_written_while_down_waits_for_a_restart(tmp_path, log):
    """#1224: same start, no further turn: A is posted only by the *next* bot restart.

    Expected (W65's stated design): rescue returns A. This documents that while the
    bot keeps running, the thread shows nothing for that turn (posted == []).
    """
    run = await _restart_mid_turn(tmp_path, old_process_posted=False)
    assert run.posted == []  # nothing reaches the thread while this process lives
    rescue = final_answer_needs_recovery(run.project, run.cursor[-1] if run.cursor else None)
    assert rescue is not None and rescue.uuid == "a1"


async def test_unwritable_project_dir_is_silent_and_double_posts(tmp_path, log, caplog):
    """#1226: `.clord-last-silent` cannot be written (read-only dir): logged at DEBUG only,
    and the restart then cannot adopt A → the next restart re-posts it.
    Expected: a WARNING (or better) that the record failed.
    """
    project = tmp_path / "p"
    project.mkdir()
    jsonl = project / "s.jsonl"
    clord_transcript(jsonl)
    os.utime(jsonl, (1, 1))
    run = _Run()
    m = TranscriptMirror(
        thread_id=THREAD_ID,
        project_dir=project,
        sink=run.sink,
        reply_sink=run.reply_sink,
        file_sink=run.file_sink,
        reply_cursor_sink=run.cursor_sink,
        poll_interval=0.02,
        idle_flush_seconds=0,
    )
    os.chmod(project, 0o555)
    caplog.set_level(logging.DEBUG)
    m.start()
    try:
        await asyncio.sleep(0.1)
        _append(jsonl, _user("依頼", "u0"), _text("A", "a1"), _tool("t1", "a2"))
        await asyncio.sleep(0.3)
    finally:
        await m.stop()
        os.chmod(project, 0o755)
    assert ("silent", "A") in run.posted
    assert not (project / LAST_SILENT_FILENAME).exists()
    warned = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "silent" in r.getMessage().lower()
    ]
    assert warned, "record failure only at DEBUG"


async def test_failed_silent_post_is_adopted_and_never_delivered(tmp_path, log):
    """#1225 (pre-existing since #1125): A's post fails (sink raises →
    `_try_sink` returns None), the turn ends with no text after the tool, A is
    adopted as the delivered answer and the cursor moves onto it, so the rescue
    never re-posts it. Expected: rescue returns A (or cursor not on A).
    """
    project = tmp_path / "p"
    run = _Run()
    orig = run.sink

    async def failing_sink(text: str):
        if text == "A":
            raise RuntimeError("discord 503")
        return await orig(text)

    run.sink = failing_sink  # type: ignore[method-assign]
    # _replay builds the mirror from run.sink → patch via a subclass-free route
    import tests.transcript.test_answer_cursor_replay as mod

    real_run = mod._Run
    mod._Run = lambda: run  # type: ignore[assignment]
    try:
        out = await _replay(
            project,
            [
                (_user("依頼", "u0"), 0.05),
                (_text("A", "a1"), 0.05),
                (_tool("t1", "a2"), 0.05),
                (_result("t1", "u1"), 0.05),
                (END, 0.1),
            ],
        )
    finally:
        mod._Run = real_run  # type: ignore[assignment]
    assert ("silent", "A") not in out.posted  # the post really failed
    rescue = final_answer_needs_recovery(project, out.cursor[-1] if out.cursor else None)
    assert rescue is not None and rescue.uuid == "a1", (out.cursor, rescue)


async def test_failed_silent_post_reaches_the_thread_when_the_next_turn_starts(tmp_path, log):
    """#1225 + #1224: the failed text is not lost when the next turn ends before a restart.

    A's post fails, the turn ends (A not adopted), the user asks again. A goes out
    when that next turn starts, then B answers it; the cursor lands on A, then B.
    """
    project = tmp_path / "p"
    run = _Run()
    orig = run.sink

    async def failing_sink(text: str):
        if text == "A":
            raise RuntimeError("discord 503")
        return await orig(text)

    run.sink = failing_sink  # type: ignore[method-assign]
    import tests.transcript.test_answer_cursor_replay as mod

    real_run = mod._Run
    mod._Run = lambda: run  # type: ignore[assignment]
    try:
        out = await _replay(
            project,
            [
                (_user("依頼", "u0"), 0.05),
                (_text("A", "a1"), 0.05),
                (_tool("t1", "a2"), 0.05),
                (_result("t1", "u1"), 0.05),
                (END, 0.1),
                (_user("次の依頼", "u2"), 0.05),
                (_text("B", "b1"), 0.05),
                (dict(END, uuid="end-2"), 0.1),
            ],
        )
    finally:
        mod._Run = real_run  # type: ignore[assignment]
    replies = [t for kind, t in out.posted if kind == "reply"]
    assert replies == ["A", "B"], out.posted
    assert out.cursor == ["a1", "b1"]
    assert final_answer_needs_recovery(project, out.cursor[-1]) is None


async def _owed_then_pane_bang(tmp_path: Path, *, next_person_turn: bool) -> tuple[_Run, Path]:
    """A's post fails (A owed), then ``!ls`` typed in the pane and Claude's answer to it."""
    project = tmp_path / "p"
    run = _Run()
    orig = run.sink

    async def failing_sink(text: str):
        if text == "A":
            raise RuntimeError("discord 503")
        return await orig(text)

    run.sink = failing_sink  # type: ignore[method-assign]
    import tests.transcript.test_answer_cursor_replay as mod

    steps = [
        (_user("依頼", "u0"), 0.05),
        (_text("A", "a1"), 0.05),
        (_tool("t1", "a2"), 0.05),
        (_result("t1", "u1"), 0.05),
        (END, 0.1),
        # the pane's ``!ls`` — a harness record, not anyone's turn (#1013/#1052)
        (_user("<bash-input>ls</bash-input>", "bi1"), 0.05),
        (_user("<bash-stdout>a.txt</bash-stdout><bash-stderr></bash-stderr>", "bo1"), 0.05),
        (_text("BANG", "bang1"), 0.05),
        (dict(END, uuid="end-2"), 0.1),
    ]
    if next_person_turn:
        steps.append((_user("次の依頼", "u3"), 0.1))
    real_run = mod._Run
    mod._Run = lambda: run  # type: ignore[assignment]
    try:
        out = await _replay(project, steps)
    finally:
        mod._Run = real_run  # type: ignore[assignment]
    return out, project


def _body_order(run: _Run) -> list[str]:
    return [t.rsplit("\n", 1)[-1] for _, t in run.posted if t.rsplit("\n", 1)[-1] in ("A", "BANG")]


async def test_owed_answer_is_not_committed_behind_a_later_pane_bang_answer(tmp_path, log):
    """Review of #1224/#1225: A owed, ``!ls`` answered (cursor → bang1), then the next
    person turn posted A and moved the cursor *back* to a1 — a restart before that
    turn ended re-posted the ``!`` answer. A goes out before the later answer, and
    the cursor only ever moves forward."""
    run, project = await _owed_then_pane_bang(tmp_path, next_person_turn=True)
    assert _body_order(run) == ["A", "BANG"], run.posted
    assert run.cursor == ["a1", "bang1"], run.cursor
    assert final_answer_needs_recovery(project, run.cursor[-1]) is None


async def test_owed_answer_is_not_lost_behind_a_pane_bang_answer(tmp_path, log):
    """Same, but the restart comes before any person turn: A was owed in memory only
    and the cursor already sat past it — it never reached the thread."""
    run, project = await _owed_then_pane_bang(tmp_path, next_person_turn=False)
    assert _body_order(run) == ["A", "BANG"], run.posted
    assert run.cursor == ["a1", "bang1"], run.cursor
    assert final_answer_needs_recovery(project, run.cursor[-1]) is None
