"""Scenario model + LLM-output parsing + generation prompt (Issue #377).

The generator (``scripts/fuzz/generator.py``) asks the ``claude`` CLI to emit
fuzz scenarios as ``===FUZZ===`` / ``---TEXT---`` delimiter blocks whose text is
RAW (unescaped). That format is deliberate: the payloads themselves contain
quotes, backslashes and code fences that routinely break LLM-emitted JSON (the
real failure seen with haiku). ``parse_scenarios`` parses that format first and
falls back to tolerant JSON extraction for models that emit clean JSON. The CLI's
stdout is *untrusted text*, so parsing is forgiving: it keeps the valid items and
drops the rest — a bad generation must never crash the run, it just means fewer
scenarios that hour.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Greedy capture so the fenced fallback matches to the LAST fence, not the first
# (a scenario's text may itself contain an embedded ```code fence```).
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*)```", re.DOTALL)

# Primary generation format: delimiter blocks whose text is RAW (unescaped). This
# is robust to adversarial payloads (quotes, backslashes, braces, code fences)
# that routinely break LLM-emitted JSON — the actual failure mode seen with haiku
# (#377). JSON parsing remains as a fallback for models that emit clean JSON.
_BLOCK_SENTINEL = "===FUZZ==="
_TEXT_SENTINEL = "---TEXT---"


@dataclass(frozen=True)
class Scenario:
    """One natural-language input to fire at the bot.

    Attributes:
        id: Stable identifier within a run (e.g. ``"s01"``). Auto-assigned when
            the LLM omits it.
        category: Coarse bucket the LLM placed this in (e.g. ``"emoji-flood"``).
        text: The actual message text to send to Claude.
        intent: What this scenario tries to break / what a healthy reply looks
            like. Recorded for human triage; the oracle does not require a match.
    """

    id: str
    category: str
    text: str
    intent: str


def _parse_delimited(raw: str) -> list[dict]:
    """Parse the primary ``===FUZZ===`` / ``---TEXT---`` block format.

    Each block:  category/intent header lines, then ``---TEXT---``, then the raw
    (unescaped) message text up to the next ``===FUZZ===`` or EOF. Returns ``[]``
    when the sentinel is absent (so the JSON fallback can take over).
    """
    if _BLOCK_SENTINEL not in raw:
        return []
    # Split on lines that are exactly the sentinel (allow surrounding whitespace).
    segments = re.split(rf"(?m)^\s*{re.escape(_BLOCK_SENTINEL)}\s*$", raw)
    items: list[dict] = []
    for seg in segments[1:]:  # text before the first sentinel is preamble
        lines = seg.splitlines()
        sep_idx = next((i for i, ln in enumerate(lines) if ln.strip() == _TEXT_SENTINEL), None)
        if sep_idx is None:
            continue
        header = lines[:sep_idx]
        text_lines = lines[sep_idx + 1 :]
        meta: dict[str, str] = {}
        for ln in header:
            key, sep, val = ln.partition(":")
            if sep:
                meta[key.strip().lower()] = val.strip()
        text = "\n".join(text_lines)
        # Drop a trailing wrapping code fence the model may have appended.
        text = re.sub(r"\n?```\s*$", "", text)
        if not text.strip():
            continue
        items.append(
            {
                "category": meta.get("category", ""),
                "intent": meta.get("intent", ""),
                "text": text.strip("\n"),
            }
        )
    return items


def _candidate_blobs(raw: str) -> list[str]:
    """Yield plausible JSON substrings, best-first.

    Bracket spans come *before* fenced blocks on purpose: a scenario's ``text``
    may itself contain a ```code fence```, which fools a ```…``` extractor into
    truncating the JSON. The outermost ``[ … ]`` / ``{ … }`` span is robust to
    that, since the array's real closer is the last ``]`` in the output.
    """
    if not raw or not raw.strip():
        return []
    blobs: list[str] = []
    for opener, closer in (("[", "]"), ("{", "}")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end > start:
            blobs.append(raw[start : end + 1])
    # Fenced block (greedy to the *last* fence) as a final fallback.
    m = _FENCE_RE.search(raw)
    if m and m.group(1).strip():
        blobs.append(m.group(1).strip())
    return blobs


def _coerce_items(blob: str) -> list[dict]:
    """Parse *blob* into a list of dict items, tolerating array-or-object."""
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(data, dict):
        # Accept {"scenarios": [...]} or a single scenario object.
        inner = data.get("scenarios")
        data = inner if isinstance(inner, list) else [data]
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _to_scenarios(items: list[dict], limit: int | None) -> list[Scenario]:
    out: list[Scenario] = []
    for item in items:
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        raw_id = item.get("id")
        sid = str(raw_id) if raw_id not in (None, "") else f"s{len(out) + 1:02d}"
        category = item.get("category")
        intent = item.get("intent")
        out.append(
            Scenario(
                id=sid,
                category=str(category) if category else "uncategorized",
                text=text,
                intent=str(intent) if intent else "",
            )
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def parse_scenarios(raw: str, *, limit: int | None = None) -> list[Scenario]:
    """Parse LLM output into a list of :class:`Scenario`.

    Primary path is the ``===FUZZ===`` delimiter format (robust to unescaped
    adversarial payloads); JSON is a fallback for models that emit clean JSON.
    Invalid items (no usable ``text``) are dropped; missing ``id`` values are
    auto-assigned ``s01``, … . Returns ``[]`` for unparseable input.
    """
    delimited = _to_scenarios(_parse_delimited(raw), limit)
    if delimited:
        return delimited
    # Fallback: JSON. Try each candidate span best-first.
    for blob in _candidate_blobs(raw):
        out = _to_scenarios(_coerce_items(blob), limit)
        if out:
            return out
    return []


GENERATION_SYSTEM = (
    "You are a red-team test designer for c-lord, a Discord front-end to the "
    "Claude Code CLI. Users type natural-language messages in a Discord thread; "
    "the bot forwards them to a Claude Code session and posts the answer back."
)


def build_generation_prompt(n: int, *, focus: str | None = None) -> str:
    """Build the prompt that asks the ``claude`` CLI for *n* fuzz scenarios.

    The prompt is adversarial: it asks for *diverse* inputs that probe for
    unknown bugs (rendering, truncation, encoding, prompt-injection-shaped text,
    extreme length, control characters, multilingual mixing, markdown/codeblock
    edge cases, etc.) rather than benign questions.
    """
    focus_line = f"\nBias this batch toward: {focus}.\n" if focus else ""
    return (
        f"{GENERATION_SYSTEM}\n\n"
        f"Design exactly {n} DIVERSE adversarial test messages a real user might "
        "send, chosen to surface UNEXPECTED bugs in how the bot handles, renders, "
        "and replies to text. Cover a wide spread of categories — for example: "
        "extreme length, emoji/unicode floods, control & zero-width characters, "
        "mixed languages (RTL + CJK + latin), nested markdown / unterminated code "
        "blocks, messages that look like CLI prompts or TUI chrome, very long single "
        "lines, prompt-injection-shaped requests, empty-ish / whitespace-only, "
        "messages demanding huge output, and weird quoting. Keep each realistic "
        "(something a user could actually type) and self-contained — do NOT ask the "
        "bot to run destructive shell commands or touch the network. Each message "
        "MUST be at most 1900 characters (Discord's per-message limit)."
        f"{focus_line}\n"
        "OUTPUT FORMAT — do NOT use JSON (the payloads contain quotes/backslashes "
        "that break JSON). Emit each scenario as a raw block exactly like this:\n\n"
        "===FUZZ===\n"
        "category: <short-kebab-case>\n"
        "intent: <one line: what this probes + what a healthy reply looks like>\n"
        "---TEXT---\n"
        "<the exact raw message to send — may span multiple lines and contain ANY\n"
        "characters (quotes, backslashes, backticks, braces); do NOT escape anything>\n\n"
        f"Repeat the block for each of the {n} scenarios. Do not wrap the output in a "
        "code fence. The literal tokens ===FUZZ=== and ---TEXT--- must never appear "
        "inside a message's text.\n"
        f"Output exactly {n} blocks and nothing else."
    )
