"""#399 AC3: registry that dedups the pre-menu prose across its two delivery paths.

When Claude talks (経緯・推し) and then opens an AskUserQuestion / plan menu, the
prose can reach Discord by two independent paths and we want it exactly once:

- the **pane-bridge** (``bridge_pane_ask``), which reads the prose from the TUI
  pane and posts it as the menu's context message; and
- the **transcript mirror**, which posts the prose when the CLI flushes it to
  the jsonl.

Which path fires first depends on the CLI's flush timing, which differs by menu
type (verified on staging, CLI v2.1.177):

- **AskUserQuestion**: the CLI buffers the prose until the menu resolves, so the
  pane-bridge posts first and the later flush must be suppressed by the mirror.
- **ExitPlanMode**: the CLI flushes the prose as a normal text event BEFORE the
  menu, so the mirror posts first and the pane-bridge must then skip.

The registry is therefore **bidirectional and order-independent**: each entry
carries its delivering ``source`` ("pane" / "mirror"), and each side only ever
matches the OTHER source's entries — whoever delivers first wins, the second
skips, and neither suppresses its own legitimate repeat.

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

The registry is in-memory: a bot restart between the context post and the
flush loses the entry, so the flush is posted late as a DUPLICATE of the
already-delivered context — the degraded mode is duplication, never loss.
"""

from __future__ import annotations

import difflib
import logging
import re
import time

logger = logging.getLogger(__name__)

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
# lost). 1.25x still covers the intended case: a watchdog-truncated pane
# registration matching its slightly longer flushed twin; deeper truncation
# degrades to a late duplicate, never to loss.
_CONTAINMENT_MAX_RATIO = 1.25


def _normalize(text: str) -> str:
    return _NORM_RE.sub("", text)


def _match_kind(a: str, b: str) -> str | None:
    """Return how *a* and *b* match ("exact"/"containment"/"ratio") or None."""
    if a == b:
        return "exact"
    shorter, longer = sorted((a, b), key=len)
    if shorter in longer and len(longer) <= len(shorter) * _CONTAINMENT_MAX_RATIO:
        return "containment"
    if difflib.SequenceMatcher(None, a, b).ratio() >= _MATCH_RATIO:
        return "ratio"
    return None


class BridgedContextRegistry:
    """Per-thread, one-shot, TTL-bound store of already-delivered texts.

    Each entry carries the *source* that delivered it ("pane" or "mirror").
    Dedup is bidirectional and order-independent: a side only ever matches the
    OTHER source's entries, so whichever path delivers the prose first wins and
    the second skips — and a side never suppresses its own legitimate repeat.

    - AskUserQuestion order: the CLI buffers the prose until the menu resolves,
      so the pane-bridge posts first (source="pane") and the later jsonl flush
      is suppressed by the mirror (which matches source="pane").
    - ExitPlanMode order: the CLI flushes the prose as a normal text event
      *before* the menu, so the mirror posts first (source="mirror") and the
      pane-bridge then skips (matching source="mirror").
    """

    def __init__(self) -> None:
        # thread_id -> [(registered_at_monotonic, normalized_text, source), ...]
        self._entries: dict[int, list[tuple[float, str, str]]] = {}

    def register(self, thread_id: int, text: str, source: str = "pane") -> None:
        """Record *text* as delivered to Discord by *source* for *thread_id*."""
        norm = _normalize(text)
        if len(norm) < _MIN_NORM_LEN:
            return
        now = time.monotonic()
        # Purge expired entries everywhere — in skill mode no consumer ever
        # calls consume_match, so without this the registry grows forever.
        for tid in list(self._entries):
            bucket = self._entries[tid]
            bucket[:] = [e for e in bucket if now - e[0] < _TTL_SECONDS]
            if not bucket:
                del self._entries[tid]
        bucket = self._entries.setdefault(thread_id, [])
        bucket.append((now, norm, source))
        del bucket[:-_MAX_PER_THREAD]

    def consume_match(self, thread_id: int, text: str, source: str = "pane") -> bool:
        """True iff *text* matches a live entry from *source* — removed on hit.

        *source* is the source to match against (the OTHER side's deliveries):
        the mirror passes ``source="pane"``, the pane-bridge passes
        ``source="mirror"``. Entries from the caller's own source are ignored,
        so a legitimate same-source repeat is never swallowed.
        """
        bucket = self._entries.get(thread_id)
        if not bucket:
            return False
        now = time.monotonic()
        bucket[:] = [e for e in bucket if now - e[0] < _TTL_SECONDS]
        norm = _normalize(text)
        if len(norm) >= _MIN_NORM_LEN:
            for i, (_, cand, cand_source) in enumerate(bucket):
                if cand_source != source:
                    continue
                kind = _match_kind(cand, norm)
                if kind is not None:
                    del bucket[i]
                    if not bucket:
                        self._entries.pop(thread_id, None)
                    if kind != "exact":
                        # Non-exact suppression is the risky path — keep it
                        # loud so a false positive is diagnosable from logs.
                        logger.warning(
                            "bridged_context: non-exact suppression (%s) thread=%d len=%d",
                            kind,
                            thread_id,
                            len(norm),
                        )
                    return True
        if not bucket:
            self._entries.pop(thread_id, None)
        return False

    def clear_thread(self, thread_id: int) -> None:
        """Disarm all entries for *thread_id* (called at turn boundaries).

        The legitimate flush always arrives before its turn ends, so an entry
        surviving a turn boundary can only ever cause a false positive — a
        later, genuinely new message being swallowed.
        """
        self._entries.pop(thread_id, None)

    def clear(self) -> None:
        """Drop all entries (test isolation)."""
        self._entries.clear()


# Process-wide singleton shared by the pane-ask bridge (producer) and the
# transcript mirror (consumer) — same pattern as ``ask_bus``.
bridged_context = BridgedContextRegistry()
