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
_LLM_TIMEOUT_SECONDS = 30.0

# <message> tags isolate user content so haiku cannot misinterpret action-like
# messages (e.g. "〜してください") as instructions to the model itself (#138).
_LLM_PROMPT_TEMPLATE = (
    "あなたはDiscordスレッドのタイトル生成器です。"
    "<message>タグ内のテキストを読み、20字以内の日本語で内容を要約してください。"
    "タイトルのみ返してください。記号・絵文字・説明文は不要です。"
    "\n<message>\n{msg}\n</message>"
)
_RETITLE_PROMPT_TEMPLATE = (
    "あなたはDiscordスレッドのタイトル管理者です。"
    "現在のタイトル:「{current_topic}」\n"
    "<message>タグ内の新しいメッセージを読み、"
    "このWorkのタイトルとしてまだ適切なら「{current_topic}」を一字一句そのまま返してください。"
    "Workの内容が根本的に変わった場合のみ20字以内の日本語で新タイトルを返してください。"
    "タイトルのみ返してください。余計な説明・記号・絵文字は不要です。"
    "\n<message>\n{msg}\n</message>"
)

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"<[@#!&][^>]+>|@\w+")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_WHITESPACE_RE = re.compile(r"\s+")
_FALLBACK_TOPIC = "新しいスレッド"

# Minimum length for a message to be considered instruction-like.
_INSTRUCTION_MIN_LEN = 15

# Substrings that indicate an invalid LLM response (refusal, safety notice, etc.).
# Outputs containing these are rejected and trigger a retry.
_INVALID_TOPIC_MARKERS = ("許可", "Permission", "権限", "申し訳", "---")


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


def _is_valid_topic(result: str | None, original_msg: str) -> bool:
    """Return True when *result* looks like a genuine ≤20-char summary.

    Rejects None, empty strings, outputs containing refusal/safety markers,
    and exact verbatim copies of the beginning of the original message.
    """
    if not result:
        return False
    if any(m in result for m in _INVALID_TOPIC_MARKERS):
        return False
    # Exact first-20-chars copy = haiku echoed the input instead of summarising.
    return result != original_msg[:_TOPIC_MAX_LEN]


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
            stdin=asyncio.subprocess.DEVNULL,
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

    Retries once on invalid/None output before falling back to heuristic.
    """
    for attempt in range(2):
        llm = None
        try:
            llm = await _invoke_claude_haiku(first_message)
        except Exception:  # pragma: no cover — defensive
            logger.warning("topic LLM: unexpected error (attempt=%d)", attempt + 1, exc_info=True)

        if _is_valid_topic(llm, first_message):
            return llm, "llm"  # type: ignore[return-value]

        if attempt == 0:
            logger.debug("topic LLM: attempt 1 invalid (%r), retrying", llm)

    fallback = heuristic_topic(first_message)
    logger.info("topic LLM: both attempts failed — heuristic fallback %r", fallback)
    return fallback, "heuristic"


async def maybe_retitle(message: str, current_topic: str) -> str | None:
    """Return a new topic if the message warrants a retitle, else None (#121).

    Gate: skips short messages and questions (returns None immediately).
    LLM: asked to return ``current_topic`` verbatim if still applicable,
    or a new ≤20-char topic when the work has fundamentally changed.
    Comparison: returns None when LLM output equals ``current_topic``
    byte-for-byte (no Discord rename needed).

    Retries once when the output is invalid. On any exception or after two
    invalid outputs, returns None (safe default — no retitle).
    """
    if not _looks_like_instruction(message):
        return None

    prompt = _RETITLE_PROMPT_TEMPLATE.format(current_topic=current_topic, msg=message)

    for attempt in range(2):
        try:
            new_topic = await _call_claude_p(prompt)
        except Exception:
            logger.debug("maybe_retitle: LLM call failed (attempt=%d)", attempt + 1, exc_info=True)
            return None

        if not new_topic or new_topic == current_topic:
            return None  # "no change" signal — valid, stop here

        if _is_valid_topic(new_topic, message):
            return new_topic

        if attempt == 0:
            logger.debug("maybe_retitle: invalid output %r, retrying", new_topic)

    return None
