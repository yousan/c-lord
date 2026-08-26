"""Tests for c_lord.transcript.recovery — last completed turn final answer."""

from __future__ import annotations

import json
from pathlib import Path

from c_lord.transcript.recovery import FinalAnswer, last_completed_final_answer


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


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


def test_unknown_cursor_is_treated_as_delivered(tmp_path: Path) -> None:
    """A cursor from another session file says nothing about THIS file.

    Guessing "dropped" there is what spams; guessing "delivered" at worst skips
    a rescue in a corner case (a /clear between the delivery and the restart).
    Duplicates are the harm being fixed, so the tie goes to silence.
    """
    from c_lord.transcript.recovery import final_answer_needs_recovery

    _write_jsonl(tmp_path / "s.jsonl", [_assistant("u-final", "done"), _turn_end()])

    assert final_answer_needs_recovery(tmp_path, "u-from-another-session") is None


def test_no_cursor_yields_the_answer_for_seeding(tmp_path: Path) -> None:
    """With no cursor the caller seeds it instead of posting — it needs the uuid."""
    from c_lord.transcript.recovery import final_answer_needs_recovery

    _write_jsonl(tmp_path / "s.jsonl", [_assistant("u-final", "done"), _turn_end()])

    fa = final_answer_needs_recovery(tmp_path, None)
    assert fa is not None and fa.uuid == "u-final"
