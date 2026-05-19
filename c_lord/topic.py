"""Stable-topic generator for Discord threads (Issue #95).

Given the first user message of a new thread, derive a short Japanese
topic body (≤20 chars) that will be stored as the thread's stable
identity.  Two paths:

1. ``claude -p --model haiku`` with a 10s timeout (preferred).
2. Heuristic fallback: strip URLs / mentions / code blocks, collapse
   whitespace, take the first 20 chars (always returns a non-empty
   string, never raises).

The caller stores the returned ``(topic, source)`` tuple in the
``sessions`` table — ``source`` is one of ``"llm"`` / ``"heuristic"``
and is kept for debugging.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

logger = logging.getLogger(__name__)

# 20 characters is the upper bound for the topic body (Japanese full-width
# count, which matches Python's len() for the BMP characters we care about).
_TOPIC_MAX_LEN = 20
_LLM_TIMEOUT_SECONDS = 10.0
_LLM_PROMPT_TEMPLATE = (
    "次の発言を20字以内の日本語トピックに要約してください。記号や絵文字は使わず簡潔に。: {msg}"
)

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"<[@#!&][^>]+>|@\w+")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_WHITESPACE_RE = re.compile(r"\s+")
_FALLBACK_TOPIC = "新しいスレッド"


def heuristic_topic(first_message: str) -> str:
    """Derive a topic from ``first_message`` without invoking any LLM.

    Always returns a non-empty string ≤20 chars.  Pure function — safe
    to call from anywhere.
    """
    if not first_message:
        return _FALLBACK_TOPIC
    text = _CODE_FENCE_RE.sub(" ", first_message)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return _FALLBACK_TOPIC
    return text[:_TOPIC_MAX_LEN]


async def _invoke_claude_haiku(first_message: str) -> str | None:
    """Call ``claude -p --model haiku`` and return its trimmed stdout.

    Returns None on any failure (non-zero exit, timeout, empty output,
    OSError when the binary is missing, etc.). Never raises.
    """
    prompt = _LLM_PROMPT_TEMPLATE.format(msg=first_message)
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            "--model",
            "haiku",
            "--",
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.debug("topic LLM: claude binary unavailable: %s", exc)
        return None

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_LLM_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.debug("topic LLM: timeout after %.1fs — falling back", _LLM_TIMEOUT_SECONDS)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None

    if proc.returncode != 0:
        logger.debug(
            "topic LLM: claude exit=%s stderr=%r",
            proc.returncode,
            (stderr or b"").decode("utf-8", errors="replace")[:200],
        )
        return None

    raw = (stdout or b"").decode("utf-8", errors="replace").strip()
    # The model sometimes wraps output in quotes or adds trailing punctuation.
    raw = raw.strip("「」\"' \n\r\t　。.")
    if not raw:
        return None
    # Collapse any internal whitespace and clip to the limit.
    raw = _WHITESPACE_RE.sub(" ", raw)
    return raw[:_TOPIC_MAX_LEN]


async def generate_topic(first_message: str) -> tuple[str, str]:
    """Generate a stable topic for the first message of a thread.

    Returns ``(topic, source)`` where ``source`` is ``"llm"`` when the
    Claude CLI succeeded, or ``"heuristic"`` otherwise. The returned
    topic is always a non-empty string ≤20 chars; this function never
    raises.
    """
    llm = None
    try:
        llm = await _invoke_claude_haiku(first_message)
    except Exception:  # pragma: no cover — defensive
        logger.warning("topic LLM: unexpected error", exc_info=True)
        llm = None

    if llm:
        return llm, "llm"
    return heuristic_topic(first_message), "heuristic"
