"""Strip tool-call noise from a streamed assistant message (Issue #38 Step 2).

c-lord captures Claude Code's TUI via ``tmux capture-pane``, so the streamed
text in Discord ends up containing tool-call headers (``Bash(...)``) and the
inline command output. The user only cares about Claude's final response.

This module removes the tool-call blocks while preserving conversational text
before/between/after them. A "tool block" is the ``ToolName(arg)`` header line
plus the indented output lines that immediately follow it, ending at the next
blank line or non-indented line.

Markers like ``●`` and ``⎿`` from the raw TUI are already stripped upstream by
``tmux_runner``; what reaches us looks like::

    Bash(echo green-2)
      green-2

    出力: green-2
"""

from __future__ import annotations

import re

_TOOL_NAMES = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "LS",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
    "NotebookEdit",
    "ExitPlanMode",
    "AskUserQuestion",
)
_TOOL_LINE_RE = re.compile(r"^(?:" + "|".join(_TOOL_NAMES) + r")\(.*$")
_MANY_BLANKS_RE = re.compile(r"\n{3,}")


def strip_tool_noise(text: str) -> str:
    """Remove ``ToolName(...)`` blocks from a streamed assistant message.

    The original text is returned unchanged when no tool pattern is found, and
    also when stripping would yield an empty body (a defensive fallback so the
    user never sees a blank reply if our heuristic misfires).
    """
    if not text:
        return text

    lines = text.split("\n")
    result: list[str] = []
    in_tool_output = False

    for line in lines:
        if _TOOL_LINE_RE.match(line):
            # Start of a tool block — drop the header and prepare to drop the
            # indented output that follows.
            in_tool_output = True
            continue

        if in_tool_output:
            if line.strip() == "":
                # Blank line ends the tool output region. Keep the blank so we
                # do not glue surrounding text together; ``_MANY_BLANKS_RE`` will
                # collapse runs.
                in_tool_output = False
                result.append("")
                continue
            if line.startswith((" ", "\t")):
                # Still inside the indented output block — drop it.
                continue
            # Non-indented, non-blank line — the tool output is over and this
            # line belongs to Claude's response again.
            in_tool_output = False
            result.append(line)
            continue

        result.append(line)

    cleaned = "\n".join(result).strip()
    cleaned = _MANY_BLANKS_RE.sub("\n\n", cleaned)
    return cleaned or text
