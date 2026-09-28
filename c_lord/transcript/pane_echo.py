"""#682/#808: registry of the text c-lord itself typed into a thread's pane.

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

#808 made the companion the main rule. Claude Code 2.1.278+ strips the ZWSP
from input before writing the ``user`` event, so the marker test became false
for *every* message and each Discord message came back as a 👤 line in every
thread. ``send_input`` and ``start_claude`` now record what they type here too,
so "did c-lord type this?" no longer depends on what the CLI does to its input
(the #773 lesson). The marker is still honoured where it survives; the mirror
then retires the matching record so it cannot outlive the echo it stood for.

A prompt is recorded with a longer lifetime than a menu answer
(:data:`PROMPT_TTL_SECONDS`): a message sent while a turn is running is queued
by the CLI and only written to the transcript when it is dequeued, i.e. after
that turn — which routinely takes far longer than five minutes.

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
  :data:`_TTL_SECONDS` (menu answers) or :data:`PROMPT_TTL_SECONDS` (prompts),
  and at most :data:`_MAX_PER_THREAD` are kept per thread;
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
# #808: a prompt typed during a running turn reaches the transcript only when
# the CLI dequeues it, after that turn ends. Long turns are normal, and the
# match is exact and one-shot, so a long window costs little.
PROMPT_TTL_SECONDS = 6 * 3600.0
# Enough for a burst of messages queued behind one long turn (#808).
_MAX_PER_THREAD = 32


def _normalize(text: str) -> str:
    from .formatter import ZWSP_MARKER

    return "".join(text.split()).replace(ZWSP_MARKER, "")


class PaneEchoRegistry:
    """Per-thread, one-shot, TTL-bound store of c-lord's own pane input."""

    def __init__(self) -> None:
        # thread_id -> [(expires_at_monotonic, normalized_text), ...]
        self._entries: dict[int, list[tuple[float, str]]] = {}

    def register(self, thread_id: int, text: str, *, ttl: float = _TTL_SECONDS) -> None:
        """Record *text* as typed into *thread_id*'s pane by c-lord itself.

        *ttl* is how long the echo may take to reach the transcript — see
        :data:`PROMPT_TTL_SECONDS` for why a prompt gets longer than a menu answer.
        """
        norm = _normalize(text)
        if not norm:
            return
        now = time.monotonic()
        # Purge expired entries everywhere: a thread whose mirror never reads
        # its bucket (no consume_match) would otherwise grow it for the lifetime
        # of the process.
        for tid in list(self._entries):
            bucket = self._entries[tid]
            bucket[:] = [e for e in bucket if now < e[0]]
            if not bucket:
                del self._entries[tid]
        bucket = self._entries.setdefault(thread_id, [])
        bucket.append((now + ttl, norm))
        del bucket[:-_MAX_PER_THREAD]

    def consume_match(self, thread_id: int, text: str) -> bool:
        """True iff *text* is a live entry for *thread_id* — removed on hit."""
        bucket = self._entries.get(thread_id)
        if not bucket:
            return False
        now = time.monotonic()
        bucket[:] = [e for e in bucket if now < e[0]]
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
