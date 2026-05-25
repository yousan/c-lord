"""Stable-topic generator for Discord threads (Issue #95, #121).

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

Re-summarization (#121):
``maybe_retitle(message, current_topic)`` runs on subsequent messages
to detect work changes.  It:
  1. Gates on instruction-like messages (skips questions / short replies).
  2. Asks haiku to return the current topic verbatim if still applicable,
     or a new ≤20-char topic if the work changed.
  3. Returns None when the output equals ``current_topic`` (no rename needed).
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
_RETITLE_PROMPT_TEMPLATE = (
    "前のタイトル「{current_topic}」。"
    "次のメッセージを見て、このWorkのタイトルとしてまだ適切なら「{current_topic}」を一字一句そのまま返せ。"
    "Workの内容が根本的に変わった場合のみ20字以内の日本語で新タイトルを返せ。"
    "余計な説明・記号・絵文字は不要。タイトルだけ返せ。: {msg}"
)

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"<[@#!&][^>]+>|@\w+")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_WHITESPACE_RE = re.compile(r"\s+")
_FALLBACK_TOPIC = "新しいスレッド"

# Minimum length for a message to be considered instruction-like.
_INSTRUCTION_MIN_LEN = 15


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


def _looks_like_instruction(message: str) -> bool:
    """Return True when the message is instruction-like (not a question/short reply).

    Used as a lightweight gate before invoking the retitle LLM — skips
    short messages and clear questions so the LLM is not called on every
    back-and-forth exchange.
    """
    msg = message.strip()
    if len(msg) < _INSTRUCTION_MIN_LEN:
        return False
    # Questions typically end with ? or ？
    return not (msg.endswith("?") or msg.endswith("？"))


async def _call_claude_p(prompt: str) -> str | None:
    """Call ``claude -p --model haiku`` with the given raw prompt string.

    Returns the trimmed, quote-stripped response or None on any failure.
    Never raises.
    """
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
    raw = raw.strip("「」\"' \n\r\t　。.")
    if not raw:
        return None
    raw = _WHITESPACE_RE.sub(" ", raw)
    return raw[:_TOPIC_MAX_LEN]


async def _invoke_claude_haiku(first_message: str) -> str | None:
    """Call ``_call_claude_p`` with the standard topic-generation prompt.

    Returns None on any failure.  Preserved as a named function so that
    existing tests can patch it by name.
    """
    prompt = _LLM_PROMPT_TEMPLATE.format(msg=first_message)
    return await _call_claude_p(prompt)


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


async def maybe_retitle(message: str, current_topic: str) -> str | None:
    """Return a new topic if the message warrants a retitle, else None (#121).

    Gate: skips short messages and questions (returns None immediately).
    LLM: asked to return ``current_topic`` verbatim if still applicable,
    or a new ≤20-char topic when the work has fundamentally changed.
    Comparison: returns None when LLM output equals ``current_topic``
    byte-for-byte (no Discord rename needed).

    On any LLM failure, returns None (no retitle — safe default).
    """
    if not _looks_like_instruction(message):
        return None

    prompt = _RETITLE_PROMPT_TEMPLATE.format(current_topic=current_topic, msg=message)
    try:
        new_topic = await _call_claude_p(prompt)
    except Exception:
        logger.debug("maybe_retitle: LLM call failed", exc_info=True)
        return None

    if not new_topic or new_topic == current_topic:
        return None
    return new_topic
