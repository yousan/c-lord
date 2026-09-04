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
