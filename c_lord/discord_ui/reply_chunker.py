"""Split long reply content into Discord-sendable chunks.

Discord rejects any message body longer than 2000 characters. The
``/api/reply`` path (``c_lord/ext/api_server.py``) used to call ``send`` once
with the raw ``content``, so any answer longer than the limit was truncated /
rejected — the user saw the reply cut off mid-sentence. This module splits
content into ``<= limit`` pieces, preferring line boundaries and keeping
code fences (```` ``` ````) balanced within each chunk so every piece renders
as valid Markdown on its own.

This is intentionally a server-side concern: Claude posts its final answer via
the injected ``discord-reply`` skill (#53) and should not have to reason about
Discord's length limit. Chunking here means consumers get full-length replies
just by updating the package (zero-config principle).
"""

from __future__ import annotations

#: Discord's hard limit on a message ``content`` body.
DISCORD_MAX = 2000

_FENCE = "```"
#: Room reserved in every chunk for a trailing close-fence line ("\n```").
_FENCE_RESERVE = len("\n" + _FENCE)


def _hard_split(text: str, size: int) -> list[str]:
    """Split a single oversized token into ``<= size`` pieces."""
    return [text[i : i + size] for i in range(0, len(text), size)]


def chunk_discord_content(content: str, limit: int = DISCORD_MAX) -> list[str]:
    """Split ``content`` into chunks each no longer than ``limit`` characters.

    Splitting happens on line boundaries where possible. When a code fence is
    open at a chunk boundary, the chunk is closed with ```` ``` ```` and the next
    chunk re-opens it (preserving the language hint) so each chunk is
    self-contained valid Markdown. Lines longer than ``limit`` are hard-split.

    Fence accounting (#266): a continuation chunk carries **both** the re-opened
    opening fence at its start *and* a closing fence at its end, so its room for
    body content is ``limit - len(open_fence) - 1 - _FENCE_RESERVE``. The earlier
    implementation only reserved for the closing fence, so a single code line
    longer than ~1996 chars produced a chunk of ``limit + 4`` (rejected by
    Discord) plus a degenerate empty code block — the code body never arrived.

    Args:
        content: The full reply body (may contain newlines / code fences).
        limit: Maximum length of each returned chunk. Defaults to Discord's
            2000-char message limit.

    Returns:
        A non-empty list of chunks. ``content`` short enough to fit returns
        ``[content]`` unchanged.

    Raises:
        ValueError: If ``limit`` is not positive.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(content) <= limit:
        return [content]

    chunks: list[str] = []
    current: list[str] = []
    cur_len = 0  # length of "\n".join(current)
    open_fence: str | None = None  # opening fence line text while inside a block

    def flush() -> None:
        """Emit the in-progress chunk, then re-open the fence for the next one."""
        nonlocal current, cur_len
        if not current:
            return
        lines = current
        # If the only thing trailing is the just-opened fence (no body yet),
        # move it to the next chunk instead of emitting an empty "```\n```".
        dangling_open = open_fence is not None and lines[-1] == open_fence
        if dangling_open:
            lines = lines[:-1]
        text = "\n".join(lines)
        if open_fence is not None and not dangling_open:
            text = f"{text}\n{_FENCE}"  # close the fence for this chunk
        if text != "":
            chunks.append(text)
        current = []
        cur_len = 0
        if open_fence is not None:
            # Re-open the fence at the start of the next chunk.
            current = [open_fence]
            cur_len = len(open_fence)

    def add_line(line: str, reserve: int) -> None:
        """Append ``line`` to the current chunk, flushing first if it won't fit.

        ``reserve`` is the room to keep free for a trailing close fence so the
        finished chunk (content + close) never exceeds ``limit``.
        """
        nonlocal cur_len
        sep = 1 if current else 0
        if current and cur_len + sep + len(line) + reserve > limit:
            flush()
            sep = 1 if current else 0
        current.append(line)
        cur_len += sep + len(line)

    for raw_line in content.split("\n"):
        is_fence = raw_line.lstrip().startswith(_FENCE)
        # A chunk holding this line needs a trailing close fence iff we're inside
        # a block (already open, or this line opens one).
        opening = is_fence and open_fence is None
        reserve = _FENCE_RESERVE if (open_fence is not None or opening) else 0

        if open_fence is not None and not is_fence:
            # Body line inside a fence: every continuation chunk also carries the
            # re-opened fence line + its newline, so shrink the per-piece budget.
            max_piece = max(1, limit - _FENCE_RESERVE - len(open_fence) - 1)
        else:
            max_piece = limit

        segments = [raw_line] if len(raw_line) <= max_piece else _hard_split(raw_line, max_piece)
        for seg in segments:
            add_line(seg, reserve)

        if is_fence:
            open_fence = raw_line if open_fence is None else None

    if current:
        lines = current
        dangling_open = open_fence is not None and lines[-1] == open_fence
        if dangling_open:
            lines = lines[:-1]
        text = "\n".join(lines)
        if open_fence is not None and not dangling_open:
            text = f"{text}\n{_FENCE}"
        if text != "":
            chunks.append(text)

    return chunks or [content[:limit]]
