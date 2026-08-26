"""Unit tests for ProgressFolder + EventProcessor folding behaviour."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.types import (
    MessageType,
    StreamEvent,
    ToolCategory,
    ToolUseEvent,
)
from c_lord.cogs.event_processor import EventProcessor
from c_lord.cogs.run_config import RunConfig
from c_lord.discord_ui.progress_folder import ProgressFolder


def _config(thread: MagicMock, runner: MagicMock, **kwargs) -> RunConfig:
    return RunConfig(thread=thread, runner=runner, prompt="p", **kwargs)


def _new_msg() -> MagicMock:
    m = MagicMock(spec=discord.Message)
    m.edit = AsyncMock()
    m.delete = AsyncMock()
    m.embeds = []
    return m


class TestProgressFolderUnit:
    def test_empty_initially(self) -> None:
        f = ProgressFolder()
        assert f.is_empty is True
        assert f.build_file() is None

    def test_track_adds_line(self) -> None:
        f = ProgressFolder()
        f.track(_new_msg(), "[thinking] x")
        assert f.is_empty is False
        file = f.build_file()
        assert file is not None
        assert file.filename == "progress.txt"

    def test_track_tool_then_update_result(self) -> None:
        f = ProgressFolder()
        f.track_tool("t1", _new_msg(), "[tool] Bash echo")
        f.update_tool_result("t1", "hello")
        # The corresponding line should now contain the result.
        # We exfiltrate the lines by building the file and reading the buffer.
        file = f.build_file()
        assert file is not None
        content = file.fp.read().decode()
        assert "[tool] Bash echo" in content
        assert "→ hello" in content

    def test_update_unknown_tool_is_noop(self) -> None:
        f = ProgressFolder()
        f.update_tool_result("missing", "x")  # must not raise
        assert f.is_empty is True

    @pytest.mark.asyncio
    async def test_cleanup_deletes_messages(self) -> None:
        f = ProgressFolder()
        m1, m2 = _new_msg(), _new_msg()
        f.track(m1, "a")
        f.track(m2, "b")
        await f.cleanup_messages()
        m1.delete.assert_awaited_once()
        m2.delete.assert_awaited_once()


class TestEventProcessorFolding:
    @pytest.mark.asyncio
    async def test_progress_messages_folded_on_complete(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        # Each thread.send returns a fresh mock message so we can assert deletes.
        sent_messages: list[MagicMock] = []

        async def fake_send(*args, **kwargs):
            m = _new_msg()
            sent_messages.append(m)
            return m

        thread.send = AsyncMock(side_effect=fake_send)

        p = EventProcessor(_config(thread, runner))

        # SYSTEM: triggers session_start_embed (tracked)
        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))

        # Tool use (tracked)
        await p.process(
            StreamEvent(
                message_type=MessageType.ASSISTANT,
                tool_use=ToolUseEvent(
                    tool_id="t1",
                    tool_name="Bash",
                    tool_input={"command": "echo hi"},
                    category=ToolCategory.COMMAND,
                ),
            )
        )

        # Tool result
        await p.process(
            StreamEvent(
                message_type=MessageType.USER,
                tool_result_id="t1",
                tool_result_content="hi",
            )
        )

        # Final response text + RESULT
        await p.process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text="Final answer.",
                session_id="s1",
                cost_usd=0.0,
                duration_ms=1,
            )
        )

        # Since #53, the final answer is posted by Claude via the discord-reply
        # skill (REST API). EventProcessor no longer sends text, so _last_response_msg
        # is None on success. _fold_progress falls back to sending progress.txt
        # as a standalone message, then deletes the tracked embeds.
        session_msg, tool_msg = sent_messages[0], sent_messages[1]

        session_msg.delete.assert_awaited_once()
        tool_msg.delete.assert_awaited_once()

        # The standalone progress.txt message was sent (3rd send call: file= kwarg).
        file_sends = [c for c in thread.send.await_args_list if "file" in c.kwargs]
        assert len(file_sends) == 1
        assert file_sends[0].kwargs["file"].filename == "progress.txt"

    @pytest.mark.asyncio
    async def test_no_fold_when_no_progress(self, thread: MagicMock, runner: MagicMock) -> None:
        sent_messages: list[MagicMock] = []

        async def fake_send(*args, **kwargs):
            m = _new_msg()
            sent_messages.append(m)
            return m

        thread.send = AsyncMock(side_effect=fake_send)

        p = EventProcessor(_config(thread, runner, session_id="resumed"))
        # Resumed session: no session_start embed, no tools — no progress at all.
        await p.process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text="Hi.",
                session_id="resumed",
                cost_usd=0.0,
                duration_ms=1,
            )
        )

        # The final message should NOT have been edited with attachments.
        for m in sent_messages:
            edits = [c for c in m.edit.await_args_list if "attachments" in c.kwargs]
            assert edits == []
            m.delete.assert_not_called()

class TestEmptyProgressNotSent:
    """#542: a progress.txt whose only line is the session banner must not ship.

    Every turn tracks one bookkeeping line — ``[session start] <id>``.  In the
    default jsonl bridge mode Claude's answer is posted by the transcript
    mirror, so ``_last_response_msg`` stays None and folding always fell through
    to the standalone branch: an **empty message** carrying a 40-byte
    ``progress.txt``.  It appeared on 11 of 11 sampled threads and told the
    reader nothing.
    """

    def test_bookkeeping_line_alone_counts_as_empty(self) -> None:
        """AC2: is_empty asks whether a reader would get anything out of it."""
        f = ProgressFolder()
        f.track(_new_msg(), "[session start] abc-123", meaningful=False)
        assert f.is_empty is True

    def test_bookkeeping_plus_real_line_is_not_empty(self) -> None:
        """A banner next to real content must still fold normally."""
        f = ProgressFolder()
        f.track(_new_msg(), "[session start] abc-123", meaningful=False)
        f.track_tool("t1", _new_msg(), "[tool] Bash echo")
        assert f.is_empty is False
        file = f.build_file()
        assert file is not None
        # The banner is still *in* the file — it just doesn't make it non-empty.
        assert "[session start] abc-123" in file.fp.read().decode()

    @pytest.mark.asyncio
    async def test_session_start_only_sends_no_file(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """AC1: a turn with only a session banner posts no progress.txt at all."""
        sent_messages: list[MagicMock] = []

        async def fake_send(*args, **kwargs):
            m = _new_msg()
            sent_messages.append(m)
            return m

        thread.send = AsyncMock(side_effect=fake_send)

        p = EventProcessor(_config(thread, runner))
        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))
        await p.process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text="Final answer.",
                session_id="s1",
                cost_usd=0.0,
                duration_ms=1,
            )
        )

        file_sends = [c for c in thread.send.await_args_list if "file" in c.kwargs]
        assert file_sends == [], (
            f"an empty progress.txt was posted: {file_sends!r}"
        )
        # The session-start embed is still cleaned up, so the thread ends in the
        # same state as a folded turn — just without the empty bubble.
        sent_messages[0].delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_standalone_progress_has_caption(
        self, thread: MagicMock, runner: MagicMock
    ) -> None:
        """AC3: when progress.txt IS sent standalone, it carries a caption."""
        sent_messages: list[MagicMock] = []

        async def fake_send(*args, **kwargs):
            m = _new_msg()
            sent_messages.append(m)
            return m

        thread.send = AsyncMock(side_effect=fake_send)

        p = EventProcessor(_config(thread, runner))
        await p.process(StreamEvent(message_type=MessageType.SYSTEM, session_id="s1"))
        await p.process(
            StreamEvent(
                message_type=MessageType.ASSISTANT,
                tool_use=ToolUseEvent(
                    tool_id="t1",
                    tool_name="Bash",
                    tool_input={"command": "echo hi"},
                    category=ToolCategory.COMMAND,
                ),
            )
        )
        await p.process(
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text="Final answer.",
                session_id="s1",
                cost_usd=0.0,
                duration_ms=1,
            )
        )

        file_sends = [c for c in thread.send.await_args_list if "file" in c.kwargs]
        assert len(file_sends) == 1
        content = file_sends[0].kwargs.get("content")
        assert content, "progress.txt was sent on an empty message"
        assert "progress.txt" in content
