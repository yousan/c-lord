"""Reading an AskUserQuestion's real outcome back out of the transcript (#651).

c-lord used to call an answer "delivered" as soon as the keystrokes were
accepted by tmux. That is not the same question as *did Claude receive the
answer* — on 2026-09-01 the keys were sent fine and Claude still recorded
"(No answer provided)" (#650). The transcript is where the truth is: Claude
Code writes the tool_result for the menu, and it says in plain text whether the
user answered or the tool was rejected.
"""

from __future__ import annotations

import json
from pathlib import Path

from c_lord.transcript.ask_result import (
    ASK_ANSWERED,
    ASK_NOT_ANSWERED,
    ASK_UNKNOWN,
    classify_ask_result,
    latest_ask_tool_use,
    latest_ask_tool_use_id,
    read_ask_result,
)

_ANSWERED = (
    'The user answered: "どの配色にしますか？"=(no option selected) '
    "notes: どれでもいい。動くやつを選んで進めて. Read the answers carefully — "
    "they may request clarification, changes, or that you not proceed."
)
_REJECTED = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). "
    "To tell you how to proceed, the user said:\n"
    "The user wants to clarify these questions.\n"
    "    Questions asked:\n"
    '- "どの配色にしますか？"\n'
    "  (No answer provided)"
)


def _write(project_dir: Path, name: str, events: list[dict]) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / name).write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )


def _ask_use(tool_use_id: str, ts: str) -> dict:
    return {
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "AskUserQuestion",
                    "input": {"questions": [{"question": "どの配色にしますか？", "options": []}]},
                }
            ]
        },
    }


def _ask_result(tool_use_id: str, ts: str, text: str) -> dict:
    return {
        "timestamp": ts,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]},
    }


class TestLatestAskToolUseId:
    def test_finds_the_menu_that_is_open_now(self, tmp_path: Path) -> None:
        """The menu being answered is the newest AskUserQuestion in the dir.

        The bridge never carries the tool_use_id (three of its four callers read
        the pane, not the jsonl), so it is recovered here instead of threaded
        through every call site.
        """
        _write(
            tmp_path,
            "a.jsonl",
            [
                _ask_use("toolu_old", "2026-09-01T03:00:00.000Z"),
                _ask_use("toolu_new", "2026-09-01T03:50:00.000Z"),
            ],
        )
        assert latest_ask_tool_use_id(tmp_path) == "toolu_new"

    def test_looks_across_every_session_file(self, tmp_path: Path) -> None:
        """One cwd holds many session jsonl files (see transcript/resolver)."""
        _write(tmp_path, "a.jsonl", [_ask_use("toolu_a", "2026-09-01T03:00:00.000Z")])
        _write(tmp_path, "b.jsonl", [_ask_use("toolu_b", "2026-09-01T04:00:00.000Z")])
        assert latest_ask_tool_use_id(tmp_path) == "toolu_b"

    def test_no_menu_at_all(self, tmp_path: Path) -> None:
        assert latest_ask_tool_use_id(tmp_path) is None

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert latest_ask_tool_use_id(tmp_path / "nope") is None


class TestSessionFileIsCarried:
    """Polling must not re-read the whole directory twice a second.

    A project dir can hold 182 session files, some megabytes each (#627). The
    tool_result always lands in the same file as its tool_use, so the file is
    captured alongside the id and the poll reads only that one.
    """

    def test_the_owning_session_file_comes_back_with_the_id(self, tmp_path: Path) -> None:
        _write(tmp_path, "other.jsonl", [_ask_use("toolu_old", "2026-09-01T02:00:00.000Z")])
        _write(tmp_path, "mine.jsonl", [_ask_use("toolu_new", "2026-09-01T03:50:00.000Z")])
        found = latest_ask_tool_use(tmp_path)
        assert found is not None
        assert found == ("toolu_new", tmp_path / "mine.jsonl")

    def test_reading_is_scoped_to_that_file(self, tmp_path: Path) -> None:
        """A same-id result in a file we were not pointed at is not consulted."""
        _write(tmp_path, "mine.jsonl", [_ask_use("toolu_x", "2026-09-01T03:50:00.000Z")])
        _write(tmp_path, "other.jsonl", [_ask_result("toolu_x", "2026-09-01T03:50:05.000Z", _ANSWERED)])
        assert read_ask_result(tmp_path, "toolu_x", tmp_path / "mine.jsonl") is None
        assert read_ask_result(tmp_path, "toolu_x") == _ANSWERED


class TestReadAskResult:
    def test_returns_the_result_text(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "a.jsonl",
            [
                _ask_use("toolu_x", "2026-09-01T03:50:00.000Z"),
                _ask_result("toolu_x", "2026-09-01T03:50:05.000Z", _ANSWERED),
            ],
        )
        assert read_ask_result(tmp_path, "toolu_x") == _ANSWERED

    def test_none_while_the_menu_is_still_open(self, tmp_path: Path) -> None:
        """No tool_result yet = the question has not been resolved."""
        _write(tmp_path, "a.jsonl", [_ask_use("toolu_x", "2026-09-01T03:50:00.000Z")])
        assert read_ask_result(tmp_path, "toolu_x") is None

    def test_result_stored_as_content_blocks(self, tmp_path: Path) -> None:
        """Some writers store the result as blocks rather than a bare string."""
        _write(
            tmp_path,
            "a.jsonl",
            [
                {
                    "timestamp": "2026-09-01T03:50:05.000Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_x",
                                "content": [{"type": "text", "text": _ANSWERED}],
                            }
                        ]
                    },
                }
            ],
        )
        assert read_ask_result(tmp_path, "toolu_x") == _ANSWERED


class TestClassifyAskResult:
    def test_a_real_answer(self) -> None:
        assert classify_ask_result(_ANSWERED) == ASK_ANSWERED

    def test_the_rejection_that_ate_the_answer(self) -> None:
        """#650's exact payload must classify as 'did not reach Claude'."""
        assert classify_ask_result(_REJECTED) == ASK_NOT_ANSWERED

    def test_nothing_yet(self) -> None:
        assert classify_ask_result(None) == ASK_UNKNOWN

    def test_unrecognised_text_is_not_guessed(self) -> None:
        """Claiming ✅ on text we do not understand is the bug we are fixing."""
        assert classify_ask_result("something else entirely") == ASK_UNKNOWN
