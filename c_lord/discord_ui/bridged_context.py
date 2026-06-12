"""#399 AC3: registry of pane-bridged context texts for transcript-mirror dedup.

When Claude talks (経緯・推し) and then opens an AskUserQuestion / plan menu,
the CLI buffers the whole jsonl chunk until the menu resolves. The pane-ask
bridge therefore posts that prose to Discord live (``bridge_pane_ask``), and
the CLI flushes the SAME text to the jsonl after resolution — which the
transcript mirror would then re-post. This registry lets the bridge mark a
text as already delivered so the mirror can suppress the duplicate.

The two copies are never byte-identical: the pane carries the TUI *rendering*
(markdown stripped, hard-wrapped, box-drawn), the jsonl carries the raw
markdown. Matching therefore normalizes both sides — all whitespace plus
markdown/box-drawing punctuation removed — and accepts containment or a high
similarity ratio.

Safety properties (a false positive here swallows a real message, so the
design is deliberately conservative):

- entries are **one-shot**: consumed on first match;
- texts whose normalized form is shorter than ``_MIN_NORM_LEN`` are never
  registered nor matched — tiny sentences are too ambiguous to suppress;
- entries expire after ``_TTL_SECONDS`` (the ask-menu answer timeout);
- at most ``_MAX_PER_THREAD`` entries are kept per thread.

The registry is in-memory: a bot restart while a menu is open loses the
entry, degrading to the pre-#399 behavior (the flush is posted late) — a
duplicate-free failure mode.
"""

from __future__ import annotations

import difflib
import re
import time

# Whitespace + markdown + box-drawing punctuation differ between the TUI
# rendering and the raw markdown; none of it is content.
_NORM_RE = re.compile(r"[\s#*_`>~|│╭╮╰╯┌┐└┘├┤─━═╌\-]+")
# Below this normalized length a text is too ambiguous to suppress safely.
_MIN_NORM_LEN = 24
# Matches the 24h ask-menu answer timeout (ask_handler.ASK_ANSWER_TIMEOUT).
_TTL_SECONDS = 86_400.0
_MAX_PER_THREAD = 8
# SequenceMatcher acceptance threshold for the fuzzy fallback.
_MATCH_RATIO = 0.9
# Containment counts only between comparably-sized texts. Without this cap, a
# long final reply that merely QUOTES the registered context would be
# suppressed wholesale — the worst possible failure (a real message silently
# lost). 1.5x still covers the intended case: a watchdog-truncated pane
# registration matching its slightly longer flushed twin.
_CONTAINMENT_MAX_RATIO = 1.5


def _normalize(text: str) -> str:
    return _NORM_RE.sub("", text)


def _matches(a: str, b: str) -> bool:
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    if shorter in longer and len(longer) <= len(shorter) * _CONTAINMENT_MAX_RATIO:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= _MATCH_RATIO


class BridgedContextRegistry:
    """Per-thread, one-shot, TTL-bound store of already-delivered texts."""

    def __init__(self) -> None:
        # thread_id -> [(registered_at_monotonic, normalized_text), ...]
        self._entries: dict[int, list[tuple[float, str]]] = {}

    def register(self, thread_id: int, text: str) -> None:
        """Record *text* as delivered to Discord for *thread_id*."""
        norm = _normalize(text)
        if len(norm) < _MIN_NORM_LEN:
            return
        bucket = self._entries.setdefault(thread_id, [])
        bucket.append((time.monotonic(), norm))
        del bucket[:-_MAX_PER_THREAD]

    def consume_match(self, thread_id: int, text: str) -> bool:
        """True iff *text* matches a live entry — which is then removed."""
        bucket = self._entries.get(thread_id)
        if not bucket:
            return False
        now = time.monotonic()
        bucket[:] = [(t, n) for t, n in bucket if now - t < _TTL_SECONDS]
        norm = _normalize(text)
        if len(norm) >= _MIN_NORM_LEN:
            for i, (_, cand) in enumerate(bucket):
                if _matches(cand, norm):
                    del bucket[i]
                    if not bucket:
                        self._entries.pop(thread_id, None)
                    return True
        if not bucket:
            self._entries.pop(thread_id, None)
        return False

    def clear(self) -> None:
        """Drop all entries (test isolation)."""
        self._entries.clear()


# Process-wide singleton shared by the pane-ask bridge (producer) and the
# transcript mirror (consumer) — same pattern as ``ask_bus``.
bridged_context = BridgedContextRegistry()
