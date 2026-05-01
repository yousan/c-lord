"""Tests for StreamingMessageManager — OGP/embed suppression."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.discord_ui.streaming_manager import StreamingMessageManager


@pytest.fixture
def thread():
    t = MagicMock()
    t.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    return t


@pytest.mark.asyncio
async def test_send_suppresses_embeds(thread) -> None:
    """First send must pass suppress_embeds=True so URL OGP cards do not render."""
    mgr = StreamingMessageManager(thread)
    await mgr.append("see https://example.com")
    await mgr.finalize()

    thread.send.assert_called()
    _, kwargs = thread.send.call_args
    assert kwargs.get("suppress_embeds") is True


@pytest.mark.asyncio
async def test_edit_suppresses_embeds(thread) -> None:
    """Subsequent edits must keep embeds suppressed."""
    sent_message = MagicMock(edit=AsyncMock())
    thread.send = AsyncMock(return_value=sent_message)

    mgr = StreamingMessageManager(thread)
    await mgr.append("first https://a.com")
    # Force flush so _current_message is set, then trigger edit on next append.
    await mgr._flush()
    mgr._last_edit_time = 0  # bypass debounce
    await mgr.append(" more https://b.com")
    await mgr.finalize()

    sent_message.edit.assert_called()
    _, kwargs = sent_message.edit.call_args
    assert kwargs.get("suppress") is True
