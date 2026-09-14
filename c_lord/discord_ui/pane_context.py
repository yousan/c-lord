"""Replace the pane-rendered pre-menu prose with the CLI's own markdown (#686).

While an AskUserQuestion menu is open the CLI writes nothing of that turn to the
jsonl (measured on staging — see ``docs/askuserquestion-bridge.md``), so the
prose above the menu can only be read from the **pane**. What that gives us is
the TUI *rendering*: box-drawn tables, hard wraps at the terminal width, markdown
stripped. Discord is not monospaced, so the table does not line up, the text is
~3x longer than it needs to be, and it can be cut mid-word at a chunk boundary.

The CLI's own markdown does arrive — flushed once the menu resolves — but the
mirror dropped it as an already-delivered duplicate (``bridged_context``), so the
readable version never reached anyone. This module is the other half: keep the
pane copy's immediacy (#399/#549 — the 経緯 must be there *with* the question),
then swap its text for the markdown when it lands.

Two rules the implementation exists to keep:

- **Never post a new message.** The prose is already in the thread; adding a
  second copy is #680 all over again. If the markdown cannot fit in the messages
  that are already there, the pane copy simply stays.
- **Never lose text.** Every failure path leaves what is in the thread alone,
  and surplus messages are removed only after every edit has landed. An ugly
  経緯 is a nuisance; a missing one is the bug #549 was about.

**Folding (裁定 2026-09-14).** The replacement above only lands when the menu
resolves — and that is exactly when nobody needs to read the prose any more.
While the question is open, the box-drawn wall is all there is: production
2026-09-08 left a 1,900-char message with 933 box characters sitting unanswered,
and unreadable, for three days. So a wall is not posted at all. Over
``FOLD_BOX_CHAR_THRESHOLD`` box characters the prose is folded to a single
pointer (``fold_pane_context``) — the prose lines that are still readable, plus
the promise that the full text arrives in a readable form — and the flush
replaces that pointer instead. Under the threshold nothing changes.

A pointer is **not** a delivery: it does not contain the prose. That is why the
registry entry is tagged ``folded`` — if the markdown will not fit into the
pointer, the mirror must post it rather than suppress it as a duplicate.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from io import BytesIO
from typing import Any

import discord

from .reply_chunker import chunk_discord_content
from .table_renderer import get_table_images

logger = logging.getLogger(__name__)


# #686: the box-drawing characters Claude Code's table renderer uses. Counting
# them is how "the pane handed us a rendered table" is told from "the pane handed
# us prose": a sentence has none, and a table has one per cell edge per row, so
# the count grows with the part that is unreadable rather than with the text.
_BOX_CHARS = frozenset("┌─┬┐│└┴┘├┼┤")

# 100 is the 裁定 (2026-09-14) threshold. For scale: a 2-row table is ~30 and
# reads fine as text; the production case that motivated this was 933.
FOLD_BOX_CHAR_THRESHOLD = 100

# What the pointer keeps of the prose. Per line so one runaway line cannot eat
# the summary, and overall so the pointer stays a pointer.
_FOLD_LINE_LIMIT = 200
_FOLD_SUMMARY_LIMIT = 400
_FOLD_MSG_LIMIT = 1900

_FOLD_FOOTER = "-# 回答すると、全文が読める形でここに届きます"


def box_char_count(text: str) -> int:
    """How many box-drawing characters *text* holds (#686)."""
    return sum(1 for ch in text if ch in _BOX_CHARS)


def should_fold_pane_context(text: str) -> bool:
    """True when *text* is a box-drawn wall rather than something readable."""
    return box_char_count(text) >= FOLD_BOX_CHAR_THRESHOLD


def _fold_summary(text: str) -> str:
    """The readable lines of *text* — the ones with no box drawing in them.

    Head first (what this is about) and the last line always (the 推し is
    conventionally last, and it is the line a reader about to choose needs).
    """
    prose = [line.strip() for line in text.splitlines()]
    prose = [line for line in prose if line and not any(ch in _BOX_CHARS for ch in line)]
    if not prose:
        return ""

    def _clip(line: str) -> str:
        return line if len(line) <= _FOLD_LINE_LIMIT else line[:_FOLD_LINE_LIMIT] + "…"

    kept: list[str] = []
    used = 0
    for line in prose:
        clipped = _clip(line)
        if kept and used + len(clipped) > _FOLD_SUMMARY_LIMIT:
            break
        kept.append(clipped)
        used += len(clipped) + 1
    last = _clip(prose[-1])
    if last not in kept:
        kept.extend(("…", last))
    return "\n".join(kept)


def fold_pane_context(text: str) -> str:
    """One message standing in for the box-drawn *text* (#686).

    Says what the prose was about, how much was folded away, and that the
    readable version is coming — so the menu still has a 経緯 (#549) instead of
    a hole, without pasting a wall nobody can read into the thread.
    """
    head = (
        "📄 **この質問の経緯は、ターミナルの罫線表示のままで読める形ではないので畳みました**"
        f"（本文 {len(text):,} 文字 / 罫線 {box_char_count(text):,} 個）"
    )
    summary = _fold_summary(text)
    if not summary:
        return f"{head}\n{_FOLD_FOOTER}"
    body = "\n".join(f"> {line}" for line in summary.splitlines())
    budget = _FOLD_MSG_LIMIT - len(head) - len(_FOLD_FOOTER) - 4
    if len(body) > budget:
        body = body[: max(budget - 1, 0)].rstrip() + "…"
    return f"{head}\n{body}\n{_FOLD_FOOTER}"


async def replace_pane_context(messages: Sequence[Any], markdown: str) -> bool:
    """Rewrite the already-posted pane prose *messages* to *markdown* (#686).

    Returns True when the thread now shows the markdown. False means nothing was
    changed — the pane copy is still there, which is the safe outcome.
    """
    if not messages or not markdown.strip():
        return False
    chunks = chunk_discord_content(markdown)
    if len(chunks) > len(messages):
        # Posting the remainder would put a second copy of the prose in the
        # thread (#680). The pane copy is readable-ish; a duplicate is not.
        logger.info(
            "pane context: markdown needs %d messages but only %d were posted — "
            "keeping the pane rendering (#686)",
            len(chunks),
            len(messages),
        )
        return False

    files = [
        discord.File(BytesIO(img), filename=fname) for fname, img in get_table_images(markdown)
    ]
    last = len(chunks) - 1
    for idx, chunk in enumerate(chunks):
        kwargs: dict[str, Any] = {"content": chunk}
        # Only ever pass attachments when there is something to attach:
        # ``attachments=[]`` would strip whatever the message already carries.
        if idx == last and files:
            kwargs["attachments"] = files
        try:
            await messages[idx].edit(**kwargs)
        except (discord.HTTPException, AttributeError):
            # Partially replaced is fine — every message still holds the same
            # prose, some rendered one way and some the other. Deleting anything
            # now is what could actually lose text, so stop here.
            logger.warning(
                "pane context: replacing the pane prose failed at chunk %d/%d (#686)",
                idx + 1,
                len(chunks),
                exc_info=True,
            )
            return False

    # Markdown is normally much shorter than the pane's hard-wrapped rendering,
    # so the tail messages of a long prose can be left over. They still show the
    # old text, so they go — but only now that every edit has landed.
    for msg in messages[len(chunks) :]:
        with contextlib.suppress(discord.HTTPException, AttributeError):
            await msg.delete()
    return True
