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

    # Reserve room so closing/continuation fences never push a chunk over the
    # hard limit. We reserve unconditionally to keep the accounting simple and
    # always safe.
    usable = max(1, limit - _FENCE_RESERVE)

    chunks: list[str] = []
    current: list[str] = []
    cur_len = 0
    open_fence: str | None = None  # opening fence line text while inside a block

    def finalize() -> None:
        nonlocal current, cur_len
        text = "\n".join(current)
        if open_fence is not None:
            text = f"{text}\n{_FENCE}"  # close the fence for this chunk
        if text != "":
            chunks.append(text)
        current = []
        cur_len = 0
        if open_fence is not None:
            # Re-open the fence at the start of the next chunk.
            current.append(open_fence)
            cur_len = len(open_fence)

    for raw_line in content.split("\n"):
        is_fence = raw_line.lstrip().startswith(_FENCE)

        segments = [raw_line] if len(raw_line) <= usable else _hard_split(raw_line, usable)
        for seg in segments:
            added = (1 if current else 0) + len(seg)
            if current and cur_len + added > usable:
                finalize()
                added = (1 if current else 0) + len(seg)
            if current:
                cur_len += 1
            current.append(seg)
            cur_len += len(seg)

        if is_fence:
            open_fence = raw_line if open_fence is None else None

    if current:
        text = "\n".join(current)
        if open_fence is not None:
            text = f"{text}\n{_FENCE}"
        if text != "":
            chunks.append(text)

    return chunks or [content[:limit]]
