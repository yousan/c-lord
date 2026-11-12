"""Value types for what a Claude Code session reports and asks.

Nothing here talks to Discord or to the CLI: these are the shapes the runner,
the event processor and the transcript mirror hand to one another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    import asyncio

    import discord


class MessageType(Enum):
    """The kind of record a stream event was built from."""

    SYSTEM = "system"
    ASSISTANT = "assistant"
    USER = "user"
    RESULT = "result"
    PROGRESS = "progress"


class ContentBlockType(Enum):
    """The kind of block inside an assistant or user message."""

    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"


class ToolCategory(Enum):
    """What a tool call is for, as far as presentation cares."""

    READ = "read"
    EDIT = "edit"
    COMMAND = "command"
    WEB = "web"
    THINK = "think"
    ASK = "ask"
    TASK = "task"
    PLAN = "plan"
    OTHER = "other"


def _categorise(category: ToolCategory, *tool_names: str) -> dict[str, ToolCategory]:
    return dict.fromkeys(tool_names, category)


#: Tool name → category, for the tools c-lord knows by name.
TOOL_CATEGORIES: dict[str, ToolCategory] = {
    **_categorise(ToolCategory.READ, "Read", "Glob", "Grep", "LS"),
    **_categorise(ToolCategory.EDIT, "Write", "Edit", "NotebookEdit"),
    **_categorise(ToolCategory.COMMAND, "Bash"),
    **_categorise(ToolCategory.WEB, "WebFetch", "WebSearch"),
    **_categorise(ToolCategory.OTHER, "Task"),
    **_categorise(ToolCategory.ASK, "AskUserQuestion"),
    **_categorise(ToolCategory.TASK, "TodoWrite"),
    **_categorise(ToolCategory.PLAN, "ExitPlanMode"),
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
    """One choice on a question menu."""

    label: str
    description: str = ""


@dataclass
class AskQuestion:
    """A question Claude put to the user, with the choices it offered."""

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


def ask_question_to_dict(question: AskQuestion) -> dict[str, Any]:
    """Serialise *question* for the ``pending_asks`` restart ledger (#671).

    Everything an answer needs to reach the right place is carried, not just
    what a menu needs to be *drawn*: the option order (answers are typed as
    ``Down × index``, so a reordered list selects the wrong thing),
    ``allow_other`` (whether the ✏️ Other affordance exists at all) and
    ``free_text_mode`` (which keystrokes deliver typed text — sending the wrong
    one answered "(No answer provided)" in #650).

    ``context`` is deliberately dropped: the 経緯 prose was already posted as its
    own message when the menu went up, and it can be long. The restored menu
    re-uses the message that is still on screen, so nothing needs re-rendering.
    """
    return {
        "question": question.question,
        "header": question.header,
        "multi_select": question.multi_select,
        "allow_other": question.allow_other,
        "free_text_mode": question.free_text_mode,
        "options": [{"label": o.label, "description": o.description} for o in question.options],
    }


def ask_question_from_dict(raw: dict[str, Any]) -> AskQuestion:
    """Rebuild an :class:`AskQuestion` from :func:`ask_question_to_dict`.

    Tolerant of rows written by older versions (which stored only
    question/header/multi_select/options): the missing keys fall back to the
    dataclass defaults rather than raising, because a row that cannot be parsed
    would silently become an un-restored — that is, dead — menu.
    """
    mode = raw.get("free_text_mode") or FREE_TEXT_ROW
    return AskQuestion(
        question=raw.get("question", ""),
        header=raw.get("header") or "",
        multi_select=bool(raw.get("multi_select", False)),
        options=[
            AskOption(label=o.get("label", ""), description=o.get("description") or "")
            for o in raw.get("options", [])
        ],
        allow_other=bool(raw.get("allow_other", True)),
        free_text_mode=mode if mode in ("row", "notes", "none") else FREE_TEXT_ROW,
    )


@dataclass
class TodoItem:
    """One line of Claude's todo list."""

    content: str
    status: str  # "pending" | "in_progress" | "completed"
    active_form: str = ""


@dataclass
class PermissionRequest:
    """Claude asking whether it may run a tool."""

    request_id: str
    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ElicitationRequest:
    """An MCP server asking the user for input."""

    request_id: str
    server_name: str
    mode: str  # "form-mode" | "url-mode"
    message: str = ""
    url: str = ""
    schema: dict[str, Any] = field(default_factory=dict)


#: Bash commands longer than this are cut (with an ellipsis) in ``display_name``.
_COMMAND_DISPLAY_WIDTH = 60

#: ``display_name`` for the tools whose label is "<verb>: <one input value>".
#: The keys are tried in order; the last element is used when none is present.
_TOOL_LABELS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "Read": ("Reading", ("file_path",), "unknown"),
    "Write": ("Writing", ("file_path",), "unknown"),
    "Edit": ("Editing", ("file_path",), "unknown"),
    "Glob": ("Searching", ("pattern", "glob"), ""),
    "Grep": ("Searching", ("pattern", "glob"), ""),
    "WebSearch": ("Searching web", ("query",), ""),
    "WebFetch": ("Fetching", ("url",), ""),
    "Task": ("Spawning agent", ("description",), ""),
}


@dataclass
class ToolUseEvent:
    """A tool call Claude made."""

    tool_id: str
    tool_name: str
    tool_input: dict[str, Any]
    category: ToolCategory

    @property
    def display_name(self) -> str:
        """One line saying what this call is doing."""
        if self.tool_name == "Bash":
            command = self.tool_input.get("command", "")
            if len(command) > _COMMAND_DISPLAY_WIDTH:
                command = command[: _COMMAND_DISPLAY_WIDTH - 3] + "..."
            return f"Running: {command}"

        label = _TOOL_LABELS.get(self.tool_name)
        if label is None:
            return f"Using: {self.tool_name}"
        verb, keys, fallback = label
        subject = next((self.tool_input[k] for k in keys if k in self.tool_input), fallback)
        return f"{verb}: {subject}"


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
    """One thing a running session reported, with only the relevant fields set."""

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
    questions: list[AskQuestion] = []
    for q in tool_input.get("questions", []):
        raw_options = q.get("options", [])
        options = [
            AskOption(label=o.get("label", ""), description=o.get("description", ""))
            for o in raw_options
            if o.get("label")
        ]
        multi_select = bool(q.get("multiSelect", False))
        has_preview = any(o.get("preview") is not None for o in raw_options)
        questions.append(
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
    return questions


@dataclass
class SessionState:
    """What one run keeps track of while its events are being handled."""

    session_id: str | None = None
    thread_id: int = 0
    accumulated_text: str = ""
    partial_text: str = ""
    # tool_use id → the Discord message showing that call
    active_tools: dict[str, discord.Message] = field(default_factory=dict)
    # tool_use id → the task refreshing that call's elapsed time
    active_timers: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    # the message showing the todo list, once one has been posted
    todo_message: discord.Message | None = None
