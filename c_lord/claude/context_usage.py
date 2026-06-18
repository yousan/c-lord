"""Context-window usage: read the numerator from the JSONL transcript and the
denominator from the ``/context`` slash command (with a per-model fallback).

The numerator (tokens currently in the context window) is read from the Claude
Code transcript, which is written for every session regardless of the user's
statusline configuration.  The denominator (the window total) is *not* present
in the transcript — the model id alone cannot distinguish a 200k window from a
1M one — so it is learned by scraping ``/context`` once per session, falling
back to :data:`MODEL_CONTEXT_WINDOW` when that is unavailable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..discord_ui.embeds import AUTOCOMPACT_THRESHOLD

# Standard context window per model.  All current Claude models default to 200k;
# the 1M window is an opt-in tier that the model id does not reveal, so it is
# detected via ``/context`` (parse_context_total) rather than this map.  Unknown
# models fall back to ``DEFAULT_CONTEXT_WINDOW``.
DEFAULT_CONTEXT_WINDOW = 200_000
MODEL_CONTEXT_WINDOW: dict[str, int] = {
    "claude-opus-4-8": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
}

# Standard context-window tiers, ascending.  Used as fallback denominators when
# ``/context`` cannot be scraped: a session whose probe failed but whose
# ``used`` already exceeds the per-model default must be on a larger tier, so we
# promote to the smallest tier that can actually contain ``used`` (see #292).
STANDARD_WINDOW_TIERS: tuple[int, ...] = (200_000, 1_000_000)

# ``<used>/<total> tokens`` line emitted by ``/context`` (e.g. ``11.7k/1m tokens``).
_CONTEXT_TOTAL_RE = re.compile(
    r"[\d.]+\s*[kmb]?\s*/\s*([\d.]+)\s*([kmb]?)\s*tokens",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContextUsage:
    """Token usage from a single assistant turn's ``usage`` block."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    model: str | None = None

    @property
    def used(self) -> int:
        """Tokens occupying the context window for the *next* turn.

        Matches Claude Code's own accounting (``embeds.session_complete_embed``):
        prompt tokens only — input plus cache reads/creation.  Output tokens are
        not yet "in" the window; they become cached input on the next turn.
        """
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens


# Claude Code writes a synthetic assistant entry (this placeholder model id,
# all-zero usage) for failed tool-call parses and no-op turns.  These do not
# occupy context and must not be treated as the latest real turn (#425).
_SYNTHETIC_MODEL = "<synthetic>"


def read_latest_usage(jsonl_path: Path) -> ContextUsage | None:
    """Return the usage of the most recent *real* assistant turn in ``jsonl_path``.

    Synthetic / no-op assistant entries are skipped: Claude Code emits a
    ``<synthetic>``-model entry with all-zero usage for failed tool-call parses
    and no-op turns.  Reporting one would show 0 tokens used (a wrong
    ``0% context`` line) and key the context-window probe on a phantom model
    (#425), so the last entry carrying real usage is used instead.

    Returns ``None`` when the file is missing or contains no assistant message
    with real ``usage``.  Malformed lines are skipped.
    """
    if not jsonl_path.is_file():
        return None

    latest: ContextUsage | None = None
    with jsonl_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "assistant":
                continue
            message = record.get("message", {})
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            candidate = ContextUsage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
                cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
                model=message.get("model"),
            )
            # Skip synthetic / no-op entries (#425): they carry no real context
            # tokens, so the last entry with real usage is the true window state.
            if candidate.model == _SYNTHETIC_MODEL or candidate.used == 0:
                continue
            latest = candidate
    return latest


def _scale(suffix: str) -> int:
    return {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix.lower(), 1)


def parse_context_total(pane_text: str) -> int | None:
    """Extract the context-window total from ``/context`` output.

    Looks for the ``<used>/<total> tokens`` line and returns ``<total>`` in
    absolute tokens (e.g. ``1m`` → ``1_000_000``).  Anchors on the latest
    ``Context Usage`` header so a stray ``X/Y tokens`` string elsewhere in the
    pane scrollback (e.g. text the bot itself echoed in earlier turns) does
    not get picked up.  Returns ``None`` when no anchor is present.
    """
    anchor_idx = pane_text.rfind("Context Usage")
    if anchor_idx == -1:
        return None
    match = _CONTEXT_TOTAL_RE.search(pane_text, pos=anchor_idx)
    if match is None:
        return None
    value, suffix = match.group(1), match.group(2)
    try:
        return round(float(value) * _scale(suffix))
    except ValueError:
        return None


def default_window(model: str | None) -> int:
    """Return the fallback context-window total for ``model``."""
    if model is None:
        return DEFAULT_CONTEXT_WINDOW
    return MODEL_CONTEXT_WINDOW.get(model, DEFAULT_CONTEXT_WINDOW)


def fallback_window(model: str | None, used: int = 0) -> int:
    """Return a fallback window total that is never smaller than ``used``.

    Used when ``/context`` cannot be scraped (:func:`parse_context_total`
    returned ``None``).  Starts from the per-model :func:`default_window`, but a
    real context window can never be smaller than what already occupies it — so
    if ``used`` exceeds that default, the session must be on a larger tier and
    we promote to the smallest :data:`STANDARD_WINDOW_TIERS` entry that contains
    ``used``.  This prevents a false ``100% full`` / auto-compact warning on a
    1M-tier session whose probe transiently failed (see #292).

    When ``used`` exceeds even the largest known tier, ``used`` itself is
    returned so the ``total >= used`` invariant always holds (the session is
    then genuinely at capacity).
    """
    total = default_window(model)
    if used <= total:
        return total
    for tier in STANDARD_WINDOW_TIERS:
        if tier >= used:
            return tier
    return used


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}k"
    return str(n)


def format_context_line(used: int, total: int) -> str:
    """Build the Discord message announcing context usage.

    Below the auto-compact threshold this is a subtle ``-#`` line; at or above
    it the message is promoted to a visible warning so the user can ``/clear``
    or ``/compact`` before auto-compact kicks in.
    """
    pct = min(100.0, used / total * 100) if total else 0.0
    used_str, total_str = _fmt_tokens(used), _fmt_tokens(total)
    if pct >= AUTOCOMPACT_THRESHOLD:
        return (
            f"⚠️ Context {pct:.0f}% full ({used_str}/{total_str}) — auto-compact may run next turn"
        )
    return f"-# \U0001f4ca {pct:.0f}% context ({used_str}/{total_str})"
