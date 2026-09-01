"""Type definitions for Claude Code CLI stream-json output."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    import discord


class MessageType(Enum):
    """Top-level message types in stream-json output."""

    SYSTEM = "system"
    ASSISTANT = "assistant"
    USER = "user"
    RESULT = "result"
    PROGRESS = "progress"


class ContentBlockType(Enum):
    """Content block types within assistant messages."""

    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"


class ToolCategory(Enum):
    """Categories for tool use, used for status emoji selection."""

    READ = "read"
    EDIT = "edit"
    COMMAND = "command"
    WEB = "web"
    THINK = "think"
    ASK = "ask"
    TASK = "task"
    PLAN = "plan"
    OTHER = "other"


# Map tool names to categories
TOOL_CATEGORIES: dict[str, ToolCategory] = {
    "Read": ToolCategory.READ,
    "Glob": ToolCategory.READ,
    "Grep": ToolCategory.READ,
    "LS": ToolCategory.READ,
    "Write": ToolCategory.EDIT,
    "Edit": ToolCategory.EDIT,
    "NotebookEdit": ToolCategory.EDIT,
    "Bash": ToolCategory.COMMAND,
    "WebFetch": ToolCategory.WEB,
    "WebSearch": ToolCategory.WEB,
    "Task": ToolCategory.OTHER,
    "AskUserQuestion": ToolCategory.ASK,
    "TodoWrite": ToolCategory.TASK,
    "ExitPlanMode": ToolCategory.PLAN,
}


#: How the open TUI menu lets a free-text answer be typed (#650).  The two
#: layouts Claude Code draws take *different* keystrokes, and sending the wrong
#: ones answers the tool with "(No answer provided)" — losing what the user
#: wrote.  Read off the pane, never assumed.
#:
#: - ``row``   — classic layout: a "Type something." row below the options.
#:               Type onto the highlighted row (``Down`` × N → text → Enter).
#: - ``notes`` — preview layout (any option carries a ``preview``): no such row;
#:               a ``Notes:`` field opened with ``n`` (``n`` → text → Enter).
#: - ``none``  — neither affordance is on screen: the menu takes no free text.
FREE_TEXT_ROW = "row"
FREE_TEXT_NOTES = "notes"
FREE_TEXT_NONE = "none"

FreeTextMode = Literal["row", "notes", "none"]


@dataclass
class AskOption:
    """A single selectable option in an AskUserQuestion prompt."""

    label: str
    description: str = ""


@dataclass
class AskQuestion:
    """A single question from an AskUserQuestion tool call."""

    question: str
    header: str = ""
    multi_select: bool = False
    options: list[AskOption] = field(default_factory=list)
    # Whether to offer the free-text "✏️ Other" affordance in AskView.
    # True for AskUserQuestion (which has a "Type something." row to type onto).
    # Plan-approval menus (#251) set this False: their free-text option ("Tell
    # Claude what to change") uses a different keystroke flow, so a generic
    # Other modal would mis-send keys into the open TUI menu.
    allow_other: bool = True
    # #650: which keystrokes deliver free text on THIS menu — see FreeTextMode.
    # ``allow_other`` says whether to offer the ✏️ Other button at all; this says
    # how to type the answer once it arrives.  Defaults to the classic row so a
    # hand-built AskQuestion keeps the pre-#650 behaviour.
    free_text_mode: FreeTextMode = FREE_TEXT_ROW
    # #399: the assistant prose spoken directly above the menu (経緯・推し),
    # extracted from the pane. The CLI buffers the jsonl chunk containing the
    # menu until resolution, so without this the question reaches Discord with
    # zero decision context. Empty when no clean prose block sits above the
    # menu (tool blocks / chrome are never carried — see _extract_pane_context).
    context: str = ""


@dataclass
class TodoItem:
    """A single item in a TodoWrite task list."""

    content: str
    status: str  # "pending", "in_progress", "completed"
    active_form: str = ""  # Present-continuous label shown while in_progress


@dataclass
class PermissionRequest:
    """A permission request from Claude Code for a tool execution."""

    request_id: str
    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ElicitationRequest:
    """An elicitation request from an MCP server."""

    request_id: str
    server_name: str
    mode: str  # "form-mode" or "url-mode"
    message: str = ""
    url: str = ""  # url-mode only
    schema: dict[str, Any] = field(default_factory=dict)  # form-mode only


@dataclass
class ToolUseEvent:
    """Parsed tool use event from stream-json."""

    tool_id: str
    tool_name: str
    tool_input: dict[str, Any]
    category: ToolCategory

    @property
    def display_name(self) -> str:
        """Human-readable description of what this tool is doing."""
        name = self.tool_name
        inp = self.tool_input

        if name == "Read":
            return f"Reading: {inp.get('file_path', 'unknown')}"
        if name == "Write":
            return f"Writing: {inp.get('file_path', 'unknown')}"
        if name == "Edit":
            return f"Editing: {inp.get('file_path', 'unknown')}"
        if name in ("Glob", "Grep"):
            pattern = inp.get("pattern", inp.get("glob", ""))
            return f"Searching: {pattern}"
        if name == "Bash":
            cmd = inp.get("command", "")
            # Truncate long commands
            if len(cmd) > 60:
                cmd = cmd[:57] + "..."
            return f"Running: {cmd}"
        if name == "WebSearch":
            return f"Searching web: {inp.get('query', '')}"
        if name == "WebFetch":
            return f"Fetching: {inp.get('url', '')}"
        if name == "Task":
            return f"Spawning agent: {inp.get('description', '')}"
        return f"Using: {name}"


class UsageLimit(NamedTuple):
    """A plan limit Claude reported instead of answering (#631).

    ``scope`` is the limit's own name as the CLI printed it ("weekly limit",
    "session limit", "Opus limit", ...) and ``resets_at`` the recovery time it
    printed alongside, verbatim ("Aug 29, 4pm (Asia/Tokyo)"), or None when the
    banner carried none.  Kept verbatim on purpose: re-deriving a local time
    from a string the CLI already localised is a way to be confidently wrong
    about the one fact the user actually needs.
    """

    scope: str
    resets_at: str | None
    line: str


@dataclass
class StreamEvent:
    """A parsed event from the Claude Code stream-json output."""

    message_type: MessageType
    raw: dict = field(default_factory=dict)
    session_id: str | None = None
    text: str | None = None
    tool_use: ToolUseEvent | None = None
    tool_result_id: str | None = None
    tool_result_content: str | None = None
    thinking: str | None = None
    has_redacted_thinking: bool = False
    ask_questions: list[AskQuestion] | None = None
    # AskUserQuestion menu parsed from the tmux pane (jsonl/tmux mode, #166).
    # Distinct from ``ask_questions`` (SDK-mode resume-via-prompt): a pane_ask is
    # answered in-place by sending menu keystrokes back to the still-open TUI.
    pane_ask: AskQuestion | None = None
    todo_list: list[TodoItem] | None = None
    is_plan_approval: bool = False
    permission_request: PermissionRequest | None = None
    elicitation: ElicitationRequest | None = None
    unknown_tui_prompt: str | None = None
    is_compact: bool = False
    compact_trigger: str | None = None
    compact_pre_tokens: int | None = None
    is_complete: bool = False
    is_partial: bool = False
    # #631: the plan limit that ended this turn, when one did. Structured
    # rather than sniffed back out of ``error`` — the reset time is the one
    # fact the reader needs and it must survive to the embed intact.
    usage_limit: UsageLimit | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    context_window: int | None = None
    error: str | None = None


def _parse_ask_questions(tool_input: dict[str, Any]) -> list[AskQuestion]:
    """Parse AskUserQuestion tool input into a list of AskQuestion objects.

    The tool input is also where the menu's TUI *layout* is decided (#650):
    Claude Code draws the preview layout — no "Type something." row, free text
    typed into a ``Notes:`` field — exactly when the question is single-select
    and at least one option carries a ``preview``. Deriving
    ``free_text_mode`` here matters for the transcript-mirror bridge, which
    builds its menu from this input and never sees the pane; without it the
    answer keystrokes go to the wrong affordance and the user's typed sentence
    is answered as "(No answer provided)".
    """
    questions_raw = tool_input.get("questions", [])
    result: list[AskQuestion] = []
    for q in questions_raw:
        raw_options = q.get("options", [])
        options = [
            AskOption(
                label=o.get("label", ""),
                description=o.get("description", ""),
            )
            for o in raw_options
            if o.get("label")
        ]
        multi_select = bool(q.get("multiSelect", False))
        has_preview = any(o.get("preview") is not None for o in raw_options)
        result.append(
            AskQuestion(
                question=q.get("question", ""),
                header=q.get("header", ""),
                multi_select=multi_select,
                options=options,
                free_text_mode=(
                    FREE_TEXT_NOTES if has_preview and not multi_select else FREE_TEXT_ROW
                ),
            )
        )
    return result


def _parse_todo_items(tool_input: dict[str, Any]) -> list[TodoItem]:
    """Parse TodoWrite tool input into a list of TodoItem objects."""
    todos_raw = tool_input.get("todos", [])
    result: list[TodoItem] = []
    for t in todos_raw:
        content = t.get("content", "")
        if not content:
            continue
        result.append(
            TodoItem(
                content=content,
                status=t.get("status", "pending"),
                active_form=t.get("activeForm", ""),
            )
        )
    return result


@dataclass
class SessionState:
    """Tracks the state of a Claude Code session during a single run.

    active_tools maps tool_use_id -> Discord Message, enabling live embed
    updates when tool results arrive.

    active_timers maps tool_use_id -> asyncio.Task that periodically edits
    the in-progress embed to show elapsed execution time. Cancelled on result.
    """

    session_id: str | None = None
    thread_id: int = 0
    accumulated_text: str = ""
    partial_text: str = ""
    active_tools: dict[str, discord.Message] = field(default_factory=dict)
    active_timers: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    # TodoWrite: reference to the live todo embed message (edited in-place on each update)
    todo_message: discord.Message | None = None
