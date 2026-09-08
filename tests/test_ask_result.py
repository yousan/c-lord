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
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]
        },
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
        _write(
            tmp_path, "other.jsonl", [_ask_result("toolu_x", "2026-09-01T03:50:05.000Z", _ANSWERED)]
        )
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


# ── #707: the CLI changed its wording and the check went quietly blind ────────

# Verbatim from Claude Code v2.1.263, staging 2026-09-08 (thread 1546774382209400873,
# tool_use toolu_01LG5wJaa9UssJTsytdLHUAp). The answer DID reach Claude — it replied
# "B案を受け取りました" in the same turn — yet c-lord classified this as `unknown`
# and told the user "Claude が受け取ったかどうかを確認できませんでした".
_ANSWERED_NEW_WORDING = (
    'Your questions have been answered: "どの案で進めますか?"="B案". '
    "You can now continue with these answers in mind."
)


class TestTheCurrentCliWordingIsRecognised:
    """#707: `The user answered:` is not what the CLI writes any more.

    The marker was documented as "stable"; it was not. Every answered menu has
    been reading as `unknown` since the wording changed, so the ✅ that #651 was
    built to earn has effectively never been shown — production logs are a wall
    of `ask answer outcome=unknown`.
    """

    def test_the_wording_the_cli_actually_writes_counts_as_answered(self) -> None:
        assert classify_ask_result(_ANSWERED_NEW_WORDING) == ASK_ANSWERED

    def test_the_older_wording_still_counts(self) -> None:
        """Transcripts written by an older CLI are still on disk and still read."""
        assert classify_ask_result(_ANSWERED) == ASK_ANSWERED

    def test_a_rejected_menu_is_still_not_answered(self) -> None:
        """The guard that matters: never turn a real failure into a ✅."""
        assert classify_ask_result(_REJECTED) == ASK_NOT_ANSWERED


class TestUnknownWordingIsAudible:
    """#707 AC3: the next wording change must not be silent.

    Adding one more literal fixes today and rebuilds the same trap for tomorrow.
    What actually failed here is that c-lord could not tell "no result yet" from
    "a result I do not understand" — both were `unknown`, and `unknown` is
    normal while polling, so nothing ever looked wrong.
    """

    def test_an_unrecognised_result_is_logged(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            assert classify_ask_result("Some brand new wording nobody has seen") == ASK_UNKNOWN

        assert any("707" in r.message or "unrecognised" in r.message for r in caplog.records), (
            "a tool_result we cannot classify is the early warning that the CLI "
            "changed — swallowing it is what cost weeks of false ❔"
        )

    def test_no_result_yet_is_not_logged(self, caplog) -> None:
        """While polling, "nothing written yet" is the normal case, not a problem."""
        import logging

        with caplog.at_level(logging.WARNING):
            assert classify_ask_result(None) == ASK_UNKNOWN
            assert classify_ask_result("") == ASK_UNKNOWN

        assert not caplog.records, "polling before the result exists must stay quiet"

    def test_the_same_unknown_wording_does_not_flood_the_log(self, caplog) -> None:
        """The confirm poll re-reads the same text every 0.5s for up to 12s."""
        import logging

        with caplog.at_level(logging.WARNING):
            for _ in range(20):
                classify_ask_result("Some brand new wording nobody has seen twice")

        assert len(caplog.records) == 1, (
            "one line per distinct wording — see LogSampler (#678): audible, not noisy"
        )
