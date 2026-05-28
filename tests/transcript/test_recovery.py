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
