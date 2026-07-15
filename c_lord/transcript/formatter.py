"""Render a single JSONL event into Discord-bound text.

The mirror is intentionally narrow: only events that have a visible
counterpart in the tmux pane are rendered.  Everything else (thinking,
framing meta like ``ai-title`` / ``pr-link`` / ``permission-mode``, the
self-loopback of c-lord-driven input marked with a zero-width-space prefix)
returns ``None`` and is dropped before it ever reaches Discord.

The rendered form mirrors what a human sees in the Claude Code **TUI**, not
the raw JSONL storage bytes.  In particular, a ``!``-prefixed bash-mode
command is stored as ``user``-role string events wrapped in
``<bash-input>`` / ``<bash-stdout>`` / ``<bash-stderr>`` tags — an internal
encoding the TUI renders as a bash block and never shows raw.  We therefore
classify those as tool activity (``tool_use`` / ``tool_result``) so they
fold into ``progress.txt`` in minimal mode instead of leaking the raw tags
as a 👤 user-input bubble (#487).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# c-lord prefixes ``tmux send-keys`` input with this zero-width space so the
# resulting ``user`` event in the JSONL can be recognized as our own echo and
# skipped — Discord already saw it on the way in.  Any user event without the
# marker is treated as a human typing directly in the pane and is mirrored.
ZWSP_MARKER = "​"


@dataclass(frozen=True)
class RenderedEvent:
    kind: str  # "assistant_text" | "tool_use" | "tool_result" | "user_input"
    body: str
    session_id: str | None = None


_FRAMING_TYPES = frozenset(
    {
        "file-history-snapshot",
        "ai-title",
        "last-prompt",
        "pr-link",
        "permission-mode",
        "queue-operation",
        "attachment",
        "system",
    }
)


def _format_tool_use(block: dict[str, Any]) -> str:
    name = block.get("name", "?")
    inp = block.get("input") or {}
    if name == "Bash":
        return f"🔧 Bash: `{inp.get('command', '')}`"
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return f"🔧 {name}: `{inp.get('file_path', '')}`"
    if name == "Grep":
        return f"🔧 Grep: `{inp.get('pattern', '')}`"
    if name == "Glob":
        return f"🔧 Glob: `{inp.get('pattern', '')}`"
    return f"🔧 {name}"


def _render_assistant(event: dict[str, Any]) -> RenderedEvent | None:
    msg = event.get("message")
    if not isinstance(msg, dict):
        return None
    parts: list[str] = []
    kind = "assistant_text"
    for block in msg.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
        elif bt == "tool_use":
            parts.append(_format_tool_use(block))
            kind = "tool_use"
        # thinking: deliberately suppressed (Issue #71 §4)
    if not parts:
        return None
    return RenderedEvent(kind=kind, body="\n".join(parts), session_id=event.get("sessionId"))


def _tool_result_body(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


# Claude Code bash-mode (``! command``) markers (#487).  A ``!``-prefixed pane
# command is stored in the JSONL as ``user``-role *string* events wrapped in
# these tags — the CLI's internal storage form, which the TUI renders as a bash
# block and never shows raw.  They are a bash *execution*, not human-typed
# input, so the mirror must render them like the Bash tool (``tool_use`` /
# ``tool_result`` → folded into progress.txt in minimal mode) rather than leak
# the raw ``<bash-*>`` tags as a 👤 user_input bubble.
_BASH_INPUT_RE = re.compile(r"^<bash-input>(.*)</bash-input>$", re.DOTALL)
_BASH_OUTPUT_RE = re.compile(
    r"^<bash-stdout>(.*)</bash-stdout><bash-stderr>(.*)</bash-stderr>$", re.DOTALL
)
# Placeholder the CLI writes when a ``!`` command produced no output — noise, drop it.
_BASH_NO_OUTPUT = "(Bash completed with no output)"
_BASH_TAG_RE = re.compile(r"</?bash-(?:input|stdout|stderr)>")


def _is_bash_mode_marker(text: str) -> bool:
    """True when *text* (already stripped) is a Claude Code bash-mode marker."""
    return text.startswith("<bash-input>") or text.startswith("<bash-stdout>")


def _render_bash_mode(text: str, session_id: str | None) -> RenderedEvent | None:
    """Render a bash-mode marker as tool activity, or ``None`` to drop it (#487).

    ``<bash-input>`` → a ``tool_use`` rendered exactly like the Bash tool.
    ``<bash-stdout>``/``<bash-stderr>`` → a ``tool_result`` carrying the combined
    output; empty output (or the "no output" placeholder) is dropped.  A marker
    that starts with a bash tag but does not fully match falls back to stripping
    the tags so the raw storage form can never reach Discord.
    """
    m = _BASH_INPUT_RE.match(text)
    if m is not None:
        command = m.group(1).strip()
        return RenderedEvent(kind="tool_use", body=f"🔧 Bash: `{command}`", session_id=session_id)
    m = _BASH_OUTPUT_RE.match(text)
    if m is not None:
        parts = [
            chunk.strip()
            for chunk in (m.group(1), m.group(2))
            if chunk.strip() and chunk.strip() != _BASH_NO_OUTPUT
        ]
        if not parts:
            return None
        return RenderedEvent(kind="tool_result", body="\n".join(parts), session_id=session_id)
    # Malformed marker: strip the tags rather than leak them raw.
    stripped = _BASH_TAG_RE.sub("", text).strip()
    if not stripped:
        return None
    return RenderedEvent(kind="tool_result", body=stripped, session_id=session_id)


def _render_user(event: dict[str, Any]) -> RenderedEvent | None:
    if event.get("isMeta"):
        return None
    msg = event.get("message")
    if not isinstance(msg, dict):
        return None

    content = msg.get("content")

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                body = _tool_result_body(block).strip()
                if body:
                    parts.append(body)
        if not parts:
            return None
        return RenderedEvent(
            kind="tool_result",
            body="\n".join(parts),
            session_id=event.get("sessionId"),
        )

    if isinstance(content, str):
        stripped = content.strip()
        # #487: bash-mode markers are a bash execution, not human input — render
        # them like the Bash tool (buffered to progress.txt) instead of leaking
        # the raw ``<bash-*>`` tags as a 👤 bubble.  Handles both the render and
        # the drop (empty output) cases.
        if _is_bash_mode_marker(stripped):
            return _render_bash_mode(stripped, event.get("sessionId"))
        if content.startswith(ZWSP_MARKER):
            # Echo of c-lord-driven send-keys; Discord already has the original.
            return None
        if not stripped:
            return None
        return RenderedEvent(
            kind="user_input",
            body=content,
            session_id=event.get("sessionId"),
        )

    return None


def render_event(event: dict[str, Any]) -> RenderedEvent | None:
    """Map one parsed JSONL event to its Discord-bound rendering, or ``None``."""
    t = event.get("type")
    if t == "assistant":
        return _render_assistant(event)
    if t == "user":
        return _render_user(event)
    if t in _FRAMING_TYPES:
        return None
    return None
