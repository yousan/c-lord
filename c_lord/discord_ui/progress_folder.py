"""Fold streaming progress noise into a single ``progress.txt`` attachment.

During a Claude Code session we post a number of intermediate Discord
messages — session-start embed, thinking embeds, tool-use / tool-result
embeds, todo-list embed, etc. They're useful while the run is in flight
but become noise once the final answer is posted.

``ProgressFolder`` collects those messages and a textual transcript of
each one. When the run completes, the EventProcessor:

1. asks the folder for a ``discord.File`` containing the transcript
2. attaches it to the final response message (or sends standalone)
3. deletes the tracked progress messages

Discord's per-message attachment cap is 25MB on free guilds; we
truncate well below that to leave headroom for the response itself.
"""

from __future__ import annotations

import contextlib
import io
import logging

import discord

logger = logging.getLogger(__name__)

_MAX_PROGRESS_BYTES = 8 * 1024 * 1024  # 8MB; Discord free cap is 25MB


class ProgressFolder:
    """Collects intermediate progress messages so they can be folded later."""

    def __init__(self) -> None:
        self._messages: list[discord.Message] = []
        self._lines: list[str] = []
        # tool_id -> index into _lines, so a tool result can amend its own line
        self._tool_idx: dict[str, int] = {}
        # Lines a reader would actually get something out of.  Bookkeeping lines
        # (the session-start banner) are tracked but not counted (#542).
        self._meaningful = 0

    @property
    def is_empty(self) -> bool:
        """True when nothing worth attaching was collected this turn.

        #542: this used to be ``not self._lines``, which counted the
        ``[session start] <id>`` banner — a line every turn records — as
        content.  In the default jsonl bridge mode ``_last_response_msg`` is
        None (Claude's answer is posted by the transcript mirror, not here), so
        every first turn fell through to the standalone send and shipped a
        40-byte ``progress.txt`` with nothing in it but that banner, attached to
        an empty message.  Emptiness is now about whether a *reader* would get
        anything out of the file, not whether we happened to append a line.
        """
        return self._meaningful == 0

    def track(self, msg: discord.Message | None, line: str, *, meaningful: bool = True) -> None:
        """Record a progress message and its transcript line.

        Pass ``meaningful=False`` for bookkeeping lines that should be kept in
        the transcript when it is built, but must not by themselves make the
        folder look non-empty (#542).
        """
        if msg is not None:
            self._messages.append(msg)
        self._lines.append(line)
        if meaningful:
            self._meaningful += 1

    def track_tool(self, tool_id: str, msg: discord.Message | None, line: str) -> None:
        """Record a tool-use message; later updatable via ``update_tool_result``."""
        if msg is not None:
            self._messages.append(msg)
        self._tool_idx[tool_id] = len(self._lines)
        self._lines.append(line)
        self._meaningful += 1

    def update_tool_result(self, tool_id: str, result: str) -> None:
        """Append a tool result snippet to the line previously tracked for ``tool_id``."""
        idx = self._tool_idx.get(tool_id)
        if idx is None:
            return
        self._lines[idx] = f"{self._lines[idx]}\n  → {result}"

    def build_file(self) -> discord.File | None:
        """Build a ``discord.File`` for the collected transcript, or None if empty."""
        if not self._lines:
            return None
        content = "\n\n".join(self._lines)
        encoded = content.encode("utf-8", errors="replace")
        if len(encoded) > _MAX_PROGRESS_BYTES:
            encoded = encoded[:_MAX_PROGRESS_BYTES] + b"\n... (truncated)"
        return discord.File(io.BytesIO(encoded), filename="progress.txt")

    async def cleanup_messages(self) -> None:
        """Delete every tracked Discord message. Failures are suppressed."""
        for msg in self._messages:
            with contextlib.suppress(Exception):
                await msg.delete()
        self._messages.clear()
