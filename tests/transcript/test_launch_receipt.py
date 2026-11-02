"""Review4 #1090 / #1104: boundaries of the launch-receipt check."""

from __future__ import annotations

from c_lord.transcript.mirror import _launched_agent_ids

LAUNCH = (
    "Async agent launched successfully. (This tool result is internal metadata)\n"
    "agentId: a6cf3f7a95d208c56 (internal ID - do not mention to user.)"
)


def _result(content, **extra) -> dict:
    ev = {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": content}]
        },
    }
    ev.update(extra)
    return ev


def test_real_receipt_string_and_list():
    assert _launched_agent_ids(_result(LAUNCH)) == ["a6cf3f7a95d208c56"]
    assert _launched_agent_ids(_result([{"type": "text", "text": LAUNCH}])) == ["a6cf3f7a95d208c56"]
    assert _launched_agent_ids(_result("\n  " + LAUNCH)) == ["a6cf3f7a95d208c56"]


def test_real_receipt_with_tool_use_result_counts():
    """The row an ``Agent`` call with ``run_in_background`` really writes (#1104)."""
    ev = _result(
        [{"type": "text", "text": LAUNCH}],
        toolUseResult={
            "isAsync": True,
            "status": "async_launched",
            "agentId": "a6cf3f7a95d208c56",
        },
    )
    assert _launched_agent_ids(ev) == ["a6cf3f7a95d208c56"]


def test_assistant_text_with_the_wording_is_not_a_launch():
    ev = {"type": "assistant", "message": {"content": [{"type": "text", "text": LAUNCH}]}}
    assert _launched_agent_ids(ev) == []


def test_read_tool_output_is_not_a_launch():
    shown = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(LAUNCH.split("\n"), 1))
    assert _launched_agent_ids(_result(shown)) == []
    assert _launched_agent_ids(_result([{"type": "text", "text": shown}])) == []


def test_grep_content_output_is_not_a_launch():
    shown = 'tests/transcript/test_background_agents.py:30:    "' + LAUNCH
    assert _launched_agent_ids(_result(shown)) == []


def test_sidechain_is_not_a_launch():
    assert _launched_agent_ids(_result(LAUNCH, isSidechain=True)) == []


def test_bash_output_that_begins_with_the_wording_is_not_a_launch():
    """jq -r over a transcript / grep -oh / echo: Bash output that *starts* with the
    text. The row carries toolUseResult of a Bash call (stdout/stderr), not an
    Agent receipt (status: async_launched)."""
    ev = _result(LAUNCH, toolUseResult={"stdout": LAUNCH, "stderr": "", "interrupted": False})
    assert _launched_agent_ids(ev) == [], "Bash output counted as a background agent"
