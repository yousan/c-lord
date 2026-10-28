"""A turn Claude Code ended on an API error (#908).

When the API fails mid-turn — the connection drops mid-response, the server
errors, the safeguards flag the session — Claude Code writes a *synthetic*
assistant message (model ``<synthetic>``, envelope ``isApiErrorMessage: true``)
and ends the turn.  c-lord used to treat that like any answer: the English CLI
text went to the thread, the lamp turned 🟡 ("your move"), and the advice in it
("Double press esc to edit your last message…") is something nobody in Discord
can do.  Readers had to notice by themselves that the turn had stopped.

This module recognises such an event and says, in Japanese, what the reader
*can* do from Discord.  Rate limits are not handled here: they have their own
path and wording (#631, :mod:`c_lord.usage_limit`).
"""

from __future__ import annotations

import re

from .usage_limit import is_rate_limit_event

#: What the runner's RESULT error starts with when the turn ended on an API error.
API_ERROR_PREFIX = "API error —"

_MID_RESPONSE = "mid-response"
_SAFEGUARDS = "safeguards flagged"

RESUME_ADVICE = "通信が途中で切れて、返事が途中で止まりました。『続けて』と送ると再開します。"
SAFEGUARDS_ADVICE = (
    "このセッションは Claude の安全機能で止められました。"
    "`/model` で別のモデルにするか、新しいスレッドで始めてください。"
)
GENERIC_ADVICE = "Claude Code が API エラーで止まりました。『続けて』と送ると再開を試みます。"

# The first sentence: up to the first ". " / "." at a line end, or the first line.
_FIRST_SENTENCE = re.compile(r"^(.+?[.。])(?=\s|$)")


def api_error_text(event: object) -> str | None:
    """The CLI's error text when *event* is a synthetic API-error message, else None.

    Runs on every tailed event, so it never assumes a shape.  Subagent
    (sidechain) errors are not this thread's turn ending, and a rate limit is
    #631's to report.
    """
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return None
    if not event.get("isApiErrorMessage") or event.get("isSidechain"):
        return None
    if is_rate_limit_event(event):
        return None
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    text = "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
    return text.strip() or None


def headline(text: str) -> str:
    """The CLI's first sentence — what went wrong, without its keyboard advice.

    Drops everything after it: "Double press esc…", "/feedback", request ids —
    steps for someone sitting at the terminal, not for a Discord reader.
    """
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    match = _FIRST_SENTENCE.match(first_line)
    return (match.group(1) if match else first_line).strip()


def advice(text: str) -> str:
    """The one Japanese line that tells a Discord reader what to do next."""
    if _SAFEGUARDS in text:
        return SAFEGUARDS_ADVICE
    if _MID_RESPONSE in text:
        return RESUME_ADVICE
    return GENERIC_ADVICE


def runner_error(text: str) -> str:
    """The RESULT error for a turn that ended on *text*."""
    return f"{API_ERROR_PREFIX} {headline(text)}"


def advice_for_error(error: str) -> str:
    """:func:`advice` for a RESULT error built by :func:`runner_error`."""
    return advice(error.removeprefix(API_ERROR_PREFIX))
