"""Fold a runaway repetition of the same lines into one self-updating message (#747).

The pain: on 2026-09-15 one thread received **616** messages in 16 minutes, all
of them one of three short lines (``待機します。`` / ``待機中です。`` /
``待機中。``), and the twelve posts worth reading in that turn were buried
under them.  Claude was looping — woken again and again by a Monitor it was
waiting on — and the mirror faithfully posted every line.  The same shape
showed up in four more threads that week (44 minutes of ≥15 posts/min).

Two things this deliberately is *not*:

- **Not "the same as the previous line".** None of the measured loops repeated
  a line back to back: #742's thread alternated three of them, so a
  previous-line test would have folded zero of 616.  A line counts as a repeat
  when it is identical to *any* line posted recently.
- **Not a drop.** A folded line is identical to one already in the thread, so
  nothing is lost but the count — and the count is the one fact the flood was
  carrying ("Claude is going round in circles").  So the copies become one
  message that says how many there were, updated in place (#678: say it in one
  line, neither swallow it nor flood).

What is folded is only ever an *intermediate* message.  The caller never offers
a turn's final answer: that is what pings the reader and what the turn is judged
by, and folding it into a counter higher up the thread would be worse than the
flood.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable

from ..utils.logger import log_ctx

logger = logging.getLogger(__name__)

#: N — how many copies of a recently posted line reach the thread on their own
#: before the rest are folded into one message.  The measured loops ran to 66–616
#: copies while legitimate repeats (a "CI を待ちます。" between real steps) never
#: reach even two in a row, so a small N costs nothing and bounds the damage.
REPEAT_FOLD_AFTER = 3

# How many distinct recent lines count as "the same thing again".  The widest
# measured loop cycled through four lines; this leaves room without letting a
# line from long ago make a new one look like a repeat.
_RECENT_DISTINCT = 8

# Discord allows roughly five message edits per five seconds per channel, and
# the fastest measured loop posted every 0.2 s.  The count only needs to be
# roughly current while the loop runs; the final number always lands on close.
_EDIT_INTERVAL_SECONDS = 5.0

# The counter is one quiet line, so the lines it quotes are kept short.
_MAX_QUOTED_CHARS = 40
_MAX_QUOTED_LINES = 3


_Post = Callable[[str], Awaitable[object | None]]
_Edit = Callable[[object, str], Awaitable[None]]


def _quote(line: str) -> str:
    flat = " ".join(line.split())
    if len(flat) > _MAX_QUOTED_CHARS:
        flat = flat[: _MAX_QUOTED_CHARS - 1] + "…"
    return f"「{flat}」"


def _for_log(line: str) -> str:
    """The line as the log shows it: one short row, so a repeated paragraph cannot flood it."""
    flat = " ".join(line.split())
    return repr(flat if len(flat) <= 80 else flat[:79] + "…")


def _render(counts: Counter[str], *, countable: bool) -> str:
    """The counter's text: one subtext line saying what repeats, and how often.

    ``countable`` is False when the message cannot be edited later — the first
    text is then all anyone will ever see, so it must not carry a number that is
    about to go stale.
    """
    ranked = counts.most_common()
    quoted = " · ".join(
        _quote(line) + (f" ×{n}" if countable and len(ranked) > 1 else "")
        for line, n in ranked[:_MAX_QUOTED_LINES]
    )
    if len(ranked) > _MAX_QUOTED_LINES:
        quoted += f" ほか {len(ranked) - _MAX_QUOTED_LINES} 種"
    total = sum(counts.values())
    head = "-# 🔁 同じ発言が続いています"
    if countable and total > 1:
        return f"{head} — {total} 回ぶんをこのメッセージにまとめました: {quoted}"
    return f"{head} — 以降はこのメッセージにまとめます: {quoted}"


class RepeatFold:
    """Decides, line by line, whether an intermediate message is a loop's copy.

    Wiring is two async callables, as with the #539 progress line, so the
    Discord specifics stay in the Cog and this stays testable with a fake clock:

    - ``post(text) -> handle`` — send the counter, return something to edit later
    - ``edit(handle, text)`` — rewrite it in place (``None``: cannot edit)

    Both are best-effort.  A failed post still keeps the flood out: the lines
    were already in the thread, and Discord failing is no reason to repeat them.
    """

    def __init__(
        self,
        *,
        thread_id: int,
        post: _Post,
        edit: _Edit | None,
        fold_after: int = REPEAT_FOLD_AFTER,
        edit_interval: float = _EDIT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._thread_id = thread_id
        self._post = post
        self._edit = edit
        self._fold_after = fold_after
        self._edit_interval = edit_interval
        self._clock = clock
        # Recently posted lines, oldest first; re-seeing one moves it to the end.
        self._recent: OrderedDict[str, None] = OrderedDict()
        # Copies in a row, since the last line that was not a copy.
        self._streak = 0
        # This turn has been seen looping: a return to the loop after a real
        # line folds straight away instead of spending another N messages.
        self._armed = False
        # The counter currently open, if any.
        self._counts: Counter[str] = Counter()
        self._handle: object | None = None
        self._last_edit = 0.0
        self._dirty = False

    async def offer(self, body: str) -> bool:
        """Return True when *body* was folded — the caller must not post it."""
        line = body.strip()
        is_copy = line in self._recent
        self._remember(line)
        if not is_copy:
            # AC2: a new line ends the fold and is posted like any other.
            await self.close()
            return False
        self._streak += 1
        if not self._armed and self._streak < self._fold_after:
            return False
        self._armed = True
        self._counts[line] += 1
        if sum(self._counts.values()) == 1:
            await self._open(line)
        else:
            self._dirty = True
            if self._clock() - self._last_edit >= self._edit_interval:
                await self.flush()
        return True

    async def flush(self) -> None:
        """Push a count held back by the edit throttle.  Cheap when nothing is pending."""
        if not self._dirty or self._handle is None or self._edit is None:
            return
        self._dirty = False
        self._last_edit = self._clock()
        try:
            await self._edit(self._handle, _render(self._counts, countable=True))
        except Exception:
            logger.warning(
                "%s repeat fold: could not update the counter",
                log_ctx(thread_id=self._thread_id),
                exc_info=True,
            )

    async def close(self) -> None:
        """End the current fold: settle the counter's final number and log what it held."""
        self._streak = 0
        if not self._counts:
            return
        await self.flush()
        logger.info(
            "%s repeat fold: folded %d repeated line(s) into one message (%s)",
            log_ctx(thread_id=self._thread_id),
            sum(self._counts.values()),
            ", ".join(f"{_for_log(line)}×{n}" for line, n in self._counts.most_common()),
        )
        self._counts = Counter()
        self._handle = None
        self._dirty = False

    async def reset(self) -> None:
        """Turn boundary: close the fold and forget this turn's lines."""
        await self.close()
        self._recent.clear()
        self._armed = False

    def _remember(self, line: str) -> None:
        self._recent[line] = None
        self._recent.move_to_end(line)
        while len(self._recent) > _RECENT_DISTINCT:
            self._recent.popitem(last=False)

    async def _open(self, line: str) -> None:
        logger.info(
            "%s repeat fold: %s repeated %d time(s) in a row — folding further copies "
            "into one message",
            log_ctx(thread_id=self._thread_id),
            _for_log(line),
            self._streak,
        )
        self._last_edit = self._clock()
        try:
            self._handle = await self._post(_render(self._counts, countable=self._edit is not None))
        except Exception:
            # Still folding: the copies are already in the thread, and the close
            # log below keeps the count.
            logger.warning(
                "%s repeat fold: could not post the counter",
                log_ctx(thread_id=self._thread_id),
                exc_info=True,
            )
            self._handle = None
