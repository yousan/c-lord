"""#978: how a restart-crossed turn ended, read from its transcript."""

from __future__ import annotations

import json
from pathlib import Path

from c_lord.host_restart_notice import TurnEnding, transcript_turn_ending


def _write(path: Path, *events: dict) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


_PROMPT = {"type": "user", "message": {"role": "user", "content": "やって"}}
_TURN_END = {"type": "system", "subtype": "turn_duration", "durationMs": 1000}


def _assistant(text: str, **extra) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        **extra,
    }


def test_an_ordinary_answer_is_a_plain_end(tmp_path) -> None:
    path = _write(tmp_path / "t.jsonl", _PROMPT, _assistant("できました"), _TURN_END)
    assert transcript_turn_ending(path) == TurnEnding()


def test_an_api_error_message_is_read_past_the_turn_end_marker(tmp_path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        _PROMPT,
        _assistant("途中"),
        _assistant("API Error: 500 Internal server error", isApiErrorMessage=True),
        _TURN_END,
    )
    assert transcript_turn_ending(path).api_error == "API Error: 500 Internal server error"


def test_a_rate_limit_is_a_usage_limit_not_an_api_error(tmp_path) -> None:
    banner = "You've hit your weekly limit · resets 3am (Asia/Tokyo)"
    path = _write(
        tmp_path / "t.jsonl",
        _PROMPT,
        _assistant(banner, isApiErrorMessage=True, error="rate_limit"),
    )
    ending = transcript_turn_ending(path)
    assert ending.api_error is None
    assert ending.usage_limit is not None


def test_an_error_claude_answered_past_does_not_count(tmp_path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        _PROMPT,
        _assistant("API Error: overloaded", isApiErrorMessage=True),
        _assistant("再試行して終わりました"),
    )
    assert transcript_turn_ending(path) == TurnEnding()


def test_sidechain_rows_are_not_the_turn(tmp_path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        _PROMPT,
        _assistant("できました"),
        _assistant("API Error: 500", isApiErrorMessage=True, isSidechain=True),
    )
    assert transcript_turn_ending(path) == TurnEnding()


def test_an_unreadable_file_is_a_plain_end(tmp_path) -> None:
    assert transcript_turn_ending(tmp_path / "missing.jsonl") == TurnEnding()
