"""#682: registry of text c-lord typed into the pane *without* the ZWSP marker.

The mirror decides whether a ``user`` event is c-lord's own echo by looking for
the zero-width space :data:`~c_lord.transcript.formatter.ZWSP_MARKER` that
:meth:`~c_lord.tmux.TmuxSessionManager.send_input` prefixes every prompt with.
That covers ordinary messages, but not the one path that deliberately types
*unmarked* text: :meth:`~c_lord.tmux.TmuxSessionManager.send_literal`, used to
answer an open AskUserQuestion menu in prose (#172 / #650).

The ZWSP is left off there on purpose — the string is typed onto the menu's
free-text row and becomes the recorded answer, so a stray marker would corrupt
the answer itself. But the CLI can still write that answer to the transcript as
a plain ``user`` event, and the marker test then reads it as "a human typed in
the pane" and posts it back with a 👤. The user's own sentence returned from the
bot two seconds after they wrote it (#682).

So the marker gets a companion rather than a replacement: what
``send_literal`` typed is recorded here, and the mirror asks this registry
about any ``user`` event that carried no marker. The answer text itself is
never touched — that is the whole point (#682 AC3).

Design (a false positive here swallows a real message, so it is deliberately
conservative — see also :mod:`c_lord.discord_ui.bridged_context`, the same
pattern for the pre-menu prose):

- matching is **exact** after normalization (all whitespace and any stray ZWSP
  removed), never fuzzy or containment. The two copies come from the same
  string, so nothing weaker is needed, and anything weaker would start
  swallowing human pane input that merely *quotes* an answer;
- there is deliberately **no minimum length**: menu answers are routinely two
  or three characters ("はい", "2番"), and a length floor would leave exactly
  the common case broken. The cost of the rare collision — a human typing the
  same short string in the pane within the TTL — is one 👤 line not mirrored,
  once;
- entries are **one-shot** (consumed on first match), expire after
  :data:`_TTL_SECONDS`, and at most :data:`_MAX_PER_THREAD` are kept per thread;
- the store is in-memory: a bot restart between the keystrokes and the flush
  loses the entry, so the echo is posted as it is today. The degraded mode is a
  duplicate, never a lost message.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# The pane wraps at its width and the bridge marker may appear anywhere, so
# compare whitespace-free forms — the same normalization ``tmux._squash`` uses
# to match text read back off the pane.
# Generous: the answer arrives in the transcript within seconds of the
# keystrokes. A longer window would only widen the chance of colliding with a
# genuine, identical human line.
_TTL_SECONDS = 300.0
_MAX_PER_THREAD = 8


def _normalize(text: str) -> str:
    from .formatter import ZWSP_MARKER

    return "".join(text.split()).replace(ZWSP_MARKER, "")


class PaneEchoRegistry:
    """Per-thread, one-shot, TTL-bound store of unmarked c-lord pane input."""

    def __init__(self) -> None:
        # thread_id -> [(registered_at_monotonic, normalized_text), ...]
        self._entries: dict[int, list[tuple[float, str]]] = {}

    def register(self, thread_id: int, text: str) -> None:
        """Record *text* as typed into *thread_id*'s pane by c-lord itself."""
        norm = _normalize(text)
        if not norm:
            return
        now = time.monotonic()
        # Purge expired entries everywhere: under CLORD_BRIDGE_MODE=skill no
        # consumer ever calls consume_match, so without this the registry would
        # grow for the lifetime of the process.
        for tid in list(self._entries):
            bucket = self._entries[tid]
            bucket[:] = [e for e in bucket if now - e[0] < _TTL_SECONDS]
            if not bucket:
                del self._entries[tid]
        bucket = self._entries.setdefault(thread_id, [])
        bucket.append((now, norm))
        del bucket[:-_MAX_PER_THREAD]

    def consume_match(self, thread_id: int, text: str) -> bool:
        """True iff *text* is a live entry for *thread_id* — removed on hit."""
        bucket = self._entries.get(thread_id)
        if not bucket:
            return False
        now = time.monotonic()
        bucket[:] = [e for e in bucket if now - e[0] < _TTL_SECONDS]
        norm = _normalize(text)
        for i, (_, cand) in enumerate(bucket):
            if cand == norm:
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


# Process-wide singleton shared by the tmux layer (producer) and the transcript
# mirror (consumer) — same pattern as ``ask_bus`` / ``bridged_context``.
pane_echo = PaneEchoRegistry()
