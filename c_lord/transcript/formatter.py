"""Render a single JSONL event into Discord-bound text.

The mirror is intentionally narrow: only events that have a visible
counterpart in the tmux pane are rendered.  Everything else (thinking,
framing meta like ``ai-title`` / ``pr-link`` / ``permission-mode``, the
self-loopback of c-lord-driven input marked with a zero-width-space prefix)
returns ``None`` and is dropped before it ever reaches Discord.
"""

from __future__ import annotations

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
        if content.startswith(ZWSP_MARKER):
            # Echo of c-lord-driven send-keys; Discord already has the original.
            return None
        if not content.strip():
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
