"""Tests for c_lord.transcript.recovery — last completed turn final answer."""

from __future__ import annotations

import json
import os
from pathlib import Path

from c_lord.transcript.recovery import FinalAnswer, last_completed_final_answer

from .helpers import clord_marker_event


def _write_jsonl(path: Path, events: list[dict]) -> None:
    # #627: the rescue scans the transcript **c-lord itself drove**, so every
    # fixture opens with one of c-lord's marked prompts, as a real one does.
    # ensure_ascii=False because Claude Code writes non-ASCII raw — escaping
    # would hide the marker behind a ``\\u200b``.
    events = [clord_marker_event(), *events]
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )


def _assistant(uuid: str, text: str) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _tool_use(uuid: str, name: str = "Bash") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"content": [{"type": "tool_use", "name": name, "input": {"command": "ls"}}]},
    }


def _turn_end() -> dict:
    return {"type": "system", "subtype": "turn_duration"}


def test_returns_none_when_no_jsonl(tmp_path: Path) -> None:
    assert last_completed_final_answer(tmp_path) is None


def test_returns_none_without_turn_end(tmp_path: Path) -> None:
    _write_jsonl(tmp_path / "s.jsonl", [_assistant("u1", "in-progress, no turn end yet")])
    assert last_completed_final_answer(tmp_path) is None


def test_returns_final_answer_of_completed_turn(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "s.jsonl",
        [
            _assistant("u1", "intermediate narration"),
            _tool_use("u2"),
            _assistant("u3", "the final answer"),
            _turn_end(),
        ],
    )
    fa = last_completed_final_answer(tmp_path)
    assert fa == FinalAnswer(uuid="u3", text="the final answer")


def test_ignores_events_after_last_turn_end(tmp_path: Path) -> None:
    # A new (incomplete) turn started after the last completed turn: its text
    # must NOT be treated as a completed final answer.
    _write_jsonl(
        tmp_path / "s.jsonl",
        [
            _assistant("u1", "answer of turn 1"),
            _turn_end(),
            _assistant("u2", "turn 2 still generating"),
        ],
    )
    fa = last_completed_final_answer(tmp_path)
    assert fa is not None
    assert fa.uuid == "u1"


def test_picks_latest_turn_when_multiple(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "s.jsonl",
        [
            _assistant("u1", "answer 1"),
            _turn_end(),
            _assistant("u2", "answer 2"),
            _turn_end(),
        ],
    )
    fa = last_completed_final_answer(tmp_path)
    assert fa is not None
    assert fa.uuid == "u2"
    assert fa.text == "answer 2"


# ----------------------------------------------------------------------
# #553: the #215 recovery re-posted answers that had NOT been dropped.
#
# The cursor can point *past* the last completed turn's final answer — a turn
# that was still running when the bot went down leaves an intermediate message's
# uuid there. Comparing the two for equality then reads "different, therefore
# dropped" and re-posts a 2000-character answer the user read minutes ago.
# The question is not "are they equal" but "has the cursor already passed it".
# ----------------------------------------------------------------------


def _user_input(uuid: str, text: str = "next") -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def test_no_recovery_when_the_cursor_is_past_the_final_answer(tmp_path: Path) -> None:
    """The exact #553 shape: shutdown mid-turn, cursor on an intermediate line.

    Transcript: …final answer (delivered) → turn end → new turn starts →
    intermediate text (posted silently, uuid lands on the cursor) → SHUTDOWN.
    ``last_completed_final_answer`` looks only up to the turn end, so it returns
    the OLD answer — which was already delivered.
    """
    from c_lord.transcript.recovery import final_answer_needs_recovery

    _write_jsonl(
        tmp_path / "s.jsonl",
        [
            _assistant("u-final", "4点とも答えます。うち2つは僕が間違っていました。"),
            _turn_end(),
            _user_input("u-task", "task-notification"),
            _assistant("u-mid", "#534 も CI 全 pass。"),
        ],
    )

    assert final_answer_needs_recovery(tmp_path, "u-mid") is None


def test_no_recovery_when_the_cursor_is_the_final_answer(tmp_path: Path) -> None:
    """The ordinary delivered case still counts as delivered."""
    from c_lord.transcript.recovery import final_answer_needs_recovery

    _write_jsonl(tmp_path / "s.jsonl", [_assistant("u-final", "done"), _turn_end()])

    assert final_answer_needs_recovery(tmp_path, "u-final") is None


def test_recovers_an_answer_written_while_the_mirror_was_down(tmp_path: Path) -> None:
    """AC4: the real drop must still be rescued — that is what #215 is for.

    The cursor sits on an EARLIER turn's answer; a later turn completed while
    nothing was tailing, so its final answer never reached Discord.
    """
    from c_lord.transcript.recovery import final_answer_needs_recovery

    _write_jsonl(
        tmp_path / "s.jsonl",
        [
            _assistant("u-old", "first turn answer"),
            _turn_end(),
            _user_input("u-ask"),
            _assistant("u-new", "second turn answer — never delivered"),
            _turn_end(),
        ],
    )

    fa = final_answer_needs_recovery(tmp_path, "u-old")
    assert fa is not None
    assert fa.uuid == "u-new"
    assert fa.text == "second turn answer — never delivered"


def test_cursor_from_another_transcript_still_recovers(tmp_path: Path) -> None:
    """A cursor that is missing here means nothing was ever delivered from here.

    The mirror commits the cursor as it delivers, so a delivery out of *this*
    file would have left its uuid in *this* file. A cursor pointing elsewhere
    (a ``/clear`` started a fresh transcript) therefore describes a mirror that
    was down for the whole file — the #215 rescue case, not the #553 one.
    """
    from c_lord.transcript.recovery import final_answer_needs_recovery

    _write_jsonl(tmp_path / "s.jsonl", [_assistant("u-final", "done"), _turn_end()])

    fa = final_answer_needs_recovery(tmp_path, "u-from-another-session")
    assert fa is not None and fa.uuid == "u-final"


def test_no_cursor_yields_the_answer_for_seeding(tmp_path: Path) -> None:
    """With no cursor the caller seeds it instead of posting — it needs the uuid."""
    from c_lord.transcript.recovery import final_answer_needs_recovery

    _write_jsonl(tmp_path / "s.jsonl", [_assistant("u-final", "done"), _turn_end()])

    fa = final_answer_needs_recovery(tmp_path, None)
    assert fa is not None and fa.uuid == "u-final"


def test_does_not_recover_a_sub_invocations_answer(tmp_path: Path) -> None:
    """#627: the rescue must not post another Claude's last answer to the thread.

    The #215 rescue re-delivers "the final answer written while the mirror was
    down".  Picking the mtime-latest jsonl made that "whatever ran last in this
    working copy" — so a ``claude -p`` sub-invocation finishing during a bot
    restart would have its answer posted into the user's thread as if it were
    Claude replying to them.
    """
    _write_jsonl(
        tmp_path / "ours.jsonl",
        [_assistant("u1", "私への返事"), _turn_end()],
    )
    os.utime(tmp_path / "ours.jsonl", (100, 100))

    # A sub-invocation that finished later, with no c-lord marker in it.
    (tmp_path / "sub.jsonl").write_text(
        "\n".join(
            json.dumps(e, ensure_ascii=False)
            for e in [_assistant("s1", '{"is_flyer":false}'), _turn_end()]
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(tmp_path / "sub.jsonl", (900, 900))

    fa = last_completed_final_answer(tmp_path)
    assert fa == FinalAnswer(uuid="u1", text="私への返事")


def test_returns_none_when_no_transcript_is_ours(tmp_path: Path) -> None:
    """#627 AC4: silence beats recovering somebody else's answer."""
    (tmp_path / "sub.jsonl").write_text(
        "\n".join(
            json.dumps(e, ensure_ascii=False)
            for e in [_assistant("s1", "not for this thread"), _turn_end()]
        )
        + "\n",
        encoding="utf-8",
    )
    assert last_completed_final_answer(tmp_path) is None
