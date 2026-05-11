"""ProgressBuffer — accumulate StreamEvents into a ``progress-*.txt`` attachment.

Issue #38: the streamed reply often contains tool-call headers and command
output inline because c-lord captures Claude Code's TUI via ``tmux
capture-pane`` (not stream-json). progress.txt gives the user a structured
record of the run that they can download from the final message, without
trying to clean up the streamed body (that is a follow-up — TUI parsing is
fragile and out of scope here).

Design choices (decided per Issue #38 discussion):

* **Content**: each StreamEvent is serialized as one JSON line.
* **Filename**: ``progress-YYYYMMDD-HHMMSS.txt`` so multiple replies in the
  same thread are easy to tell apart.
* **Skip condition**: ``tool_count == 0`` means a simple text-only Q&A —
  attaching an empty-looking progress file is just noise, so we skip.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from io import BytesIO
from typing import Any

import discord

from ..claude.types import MessageType, StreamEvent

# c-lord captures Claude Code's TUI via ``tmux capture-pane`` rather than
# parsing stream-json, so ``event.tool_use`` is rarely populated. We instead
# scan the captured text for ``ToolName(arg)`` patterns at the start of a
# line — that is how the TUI renders an in-flight tool call.
_TUI_TOOL_NAMES = (
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
_TUI_TOOL_PATTERN = re.compile(
    # Closing ``)`` is required so that partial captures growing into a tool
    # call (e.g. ``Bash(echo``) do not count until the call is complete.
    r"(?m)^(?P<name>" + "|".join(_TUI_TOOL_NAMES) + r")\((?P<arg>[^)\n]{0,200})\)",
)


class ProgressBuffer:
    """Accumulate StreamEvents and emit them as a ``progress-*.txt`` attachment.

    One instance per Claude Code run. Wire ``add(event)`` into the event
    dispatch loop, then call ``to_discord_file()`` once the run is complete.
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._tool_ids: set[str] = set()
        # Tool calls detected via TUI text scanning. Identified by their
        # ``ToolName(arg)`` signature so partial events that grow the same
        # capture do not double-count.
        self._tui_tool_signatures: set[str] = set()

    @property
    def tool_count(self) -> int:
        """Number of distinct tool calls seen during this run.

        Counts both parsed ``tool_use`` events (when available) and tool calls
        detected by scanning the TUI text capture (the common case under
        c-lord's tmux-based runner).
        """
        return len(self._tool_ids) + len(self._tui_tool_signatures)

    @property
    def should_attach(self) -> bool:
        """True iff the buffer is worth attaching as ``progress.txt``.

        Per design: attach only when at least one tool call was made. Pure
        text Q&A is left without an attachment to keep the thread clean.
        """
        return self.tool_count > 0

    def add(self, event: StreamEvent) -> None:
        """Record one StreamEvent in the buffer.

        PROGRESS events are skipped — they are stall-timer resets carrying no
        payload, and including them just bloats the file.
        """
        if event.message_type == MessageType.PROGRESS:
            return

        if event.tool_use is not None:
            self._tool_ids.add(event.tool_use.tool_id)
        if event.text:
            for m in _TUI_TOOL_PATTERN.finditer(event.text):
                # Use ``ToolName(arg-prefix)`` as a stable signature so growing
                # partial captures of the same call coalesce.
                self._tui_tool_signatures.add(f"{m.group('name')}({m.group('arg')}")

        self._events.append(_serialize_event(event))

    def to_jsonl(self) -> str:
        """Return all recorded events as a JSONL string (one event per line)."""
        return "\n".join(json.dumps(e, ensure_ascii=False) for e in self._events)

    def to_discord_file(self, *, now: datetime | None = None) -> discord.File | None:
        """Return a ``discord.File`` ready to attach, or ``None`` to skip.

        ``now`` is injectable for testing — defaults to ``datetime.now()``.
        """
        if not self.should_attach:
            return None
        when = now or datetime.now()
        filename = f"progress-{when.strftime('%Y%m%d-%H%M%S')}.txt"
        payload = self.to_jsonl().encode("utf-8")
        return discord.File(BytesIO(payload), filename=filename)


def _serialize_event(event: StreamEvent) -> dict[str, Any]:
    """Convert a StreamEvent to a JSON-serializable dict.

    Only fields that are non-None / non-empty are emitted, to keep each line
    short and readable. Nested dataclasses (ToolUseEvent, AskQuestion, etc.)
    are recursively converted; Enums are coerced to their ``.value``.
    """
    out: dict[str, Any] = {"type": event.message_type.value}
    fields = (
        "session_id",
        "text",
        "thinking",
        "has_redacted_thinking",
        "tool_result_id",
        "tool_result_content",
        "is_partial",
        "is_complete",
        "is_compact",
        "compact_trigger",
        "compact_pre_tokens",
        "cost_usd",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "error",
    )
    for name in fields:
        value = getattr(event, name)
        if value is None or value is False:
            continue
        out[name] = value
    if event.tool_use is not None:
        out["tool_use"] = _dataclass_to_dict(event.tool_use)
    if event.ask_questions:
        out["ask_questions"] = [_dataclass_to_dict(q) for q in event.ask_questions]
    if event.todo_list:
        out["todo_list"] = [_dataclass_to_dict(t) for t in event.todo_list]
    if event.permission_request is not None:
        out["permission_request"] = _dataclass_to_dict(event.permission_request)
    if event.elicitation is not None:
        out["elicitation"] = _dataclass_to_dict(event.elicitation)
    return out


def _dataclass_to_dict(obj: Any) -> Any:
    """Recursively convert a dataclass into JSON-serializable primitives."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _dataclass_to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_dataclass_to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, Enum):
        return obj.value
    return obj
