"""Unit tests for StatusManager: the per-turn reaction lamp and stall detection."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.types import ToolCategory
from c_lord.discord_ui.status import (
    EMOJI_ERROR,
    EMOJI_RUNNING,
    EMOJI_STALL_HARD,
    EMOJI_WAITING,
    STALL_HARD_SECONDS,
    StatusManager,
)


def _make_message() -> MagicMock:
    """Create a mock Discord message with guild.me for reactions."""
    msg = MagicMock()
    msg.add_reaction = AsyncMock()
    msg.remove_reaction = AsyncMock()
    msg.guild = MagicMock()
    msg.guild.me = MagicMock()
    return msg


class TestHardStall:
    """#473: a 30s stall paints ⚠️ on the trigger message — and posts nothing.

    The lamp used to be accompanied by an ``on_hard_stall`` callback that the
    chat cog used to post a "no activity" line into the thread. The reaction
    already says it, so the callback and the line are gone.
    """

    @pytest.mark.asyncio
    async def test_hard_stall_paints_the_warning_lamp(self) -> None:
        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_thinking()
        loop = asyncio.get_running_loop()
        sm._last_activity = loop.time() - STALL_HARD_SECONDS - 1
        await asyncio.sleep(2.5)
        assert sm._current_emoji == EMOJI_STALL_HARD
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_the_lamp_is_painted_once_per_stall(self) -> None:
        """Staying stalled must not re-paint (and re-notify) every 2s tick."""
        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_thinking()
        loop = asyncio.get_running_loop()
        sm._last_activity = loop.time() - STALL_HARD_SECONDS - 1
        await asyncio.sleep(5)
        hard = [c for c in msg.add_reaction.await_args_list if c.args[0] == EMOJI_STALL_HARD]
        assert len(hard) == 1
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_the_lamp_comes_back_after_activity_and_another_stall(self) -> None:
        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_thinking()
        loop = asyncio.get_running_loop()
        sm._last_activity = loop.time() - STALL_HARD_SECONDS - 1
        await asyncio.sleep(2.5)
        assert sm._current_emoji == EMOJI_STALL_HARD

        await sm.set_tool(ToolCategory.READ)  # activity → back to 🟢
        assert sm._current_emoji == EMOJI_RUNNING
        sm._last_activity = loop.time() - STALL_HARD_SECONDS - 1
        await asyncio.sleep(2.5)
        assert sm._current_emoji == EMOJI_STALL_HARD
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_a_failing_reaction_does_not_crash_the_monitor(self) -> None:
        msg = _make_message()
        msg.add_reaction = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
        sm = StatusManager(msg)
        await sm.set_thinking()
        loop = asyncio.get_running_loop()
        sm._last_activity = loop.time() - STALL_HARD_SECONDS - 1
        await asyncio.sleep(2.5)
        assert sm._stall_task is not None
        assert not sm._stall_task.done()
        await sm.cleanup()


class TestCompactStatus:
    """Tests for compact status emoji."""

    @pytest.mark.asyncio
    async def test_set_compact_changes_emoji(self) -> None:
        from c_lord.discord_ui.status import EMOJI_COMPACT

        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_thinking()
        await sm.set_compact()
        # Reactions apply immediately now (no debounce, #246).
        assert sm._current_emoji == EMOJI_COMPACT
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_set_compact_resets_stall_timer(self) -> None:
        """set_compact should reset the stall timer so ⚠️ doesn't appear during compaction."""
        from c_lord.discord_ui.status import EMOJI_COMPACT

        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_thinking()
        # Simulate time passing
        loop = asyncio.get_running_loop()
        sm._last_activity = loop.time() - 25  # Almost at hard stall threshold
        # Compact resets the timer
        await sm.set_compact()
        # Wait past what would have been the stall threshold
        await asyncio.sleep(3)
        # Still 🗜️ — the stall lamp must NOT have taken over.
        assert sm._current_emoji == EMOJI_COMPACT
        await sm.cleanup()


class TestReactionLamp:
    """#246: the per-turn lamp is a 🟢 running / 🟡 waiting message reaction."""

    @pytest.mark.asyncio
    async def test_running_adds_green_immediately(self) -> None:
        # AC1: a 🟢 reaction appears on the trigger message right at turn start
        # (no debounce — reactions are not on the thread-rename rate-limit bucket).
        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_running()
        msg.add_reaction.assert_awaited_once_with(EMOJI_RUNNING)
        assert sm._current_emoji == EMOJI_RUNNING
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_thinking_is_green_alias(self) -> None:
        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_thinking()
        msg.add_reaction.assert_awaited_once_with(EMOJI_RUNNING)
        assert sm._current_emoji == EMOJI_RUNNING
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_stays_green_while_working(self) -> None:
        # AC3: while working (thinking / tools), the lamp stays 🟢 — never 🟡,
        # and tools no longer paint a per-category emoji (🛠️/💻/🌐 removed).
        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_running()
        await sm.set_tool(ToolCategory.WEB)
        await sm.set_thinking()
        await sm.set_tool(ToolCategory.EDIT)
        # Only the initial 🟢 add — repeated work calls are no-ops on the reaction.
        msg.add_reaction.assert_awaited_once_with(EMOJI_RUNNING)
        assert sm._current_emoji == EMOJI_RUNNING
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_done_switches_green_to_yellow(self) -> None:
        # AC2: when the turn finishes the lamp flips 🟢 → 🟡 (your turn).
        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_running()
        await sm.set_done()
        msg.remove_reaction.assert_awaited()  # old 🟢 removed
        assert msg.add_reaction.await_args_list[-1].args[0] == EMOJI_WAITING
        assert sm._current_emoji == EMOJI_WAITING
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_error_shows_red(self) -> None:
        # AC4: error paints ❌.
        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_running()
        await sm.set_error()
        assert msg.add_reaction.await_args_list[-1].args[0] == EMOJI_ERROR
        assert sm._current_emoji == EMOJI_ERROR
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_consecutive_turns_each_go_green_then_yellow(self) -> None:
        # AC5: each turn has its own trigger message; the lamp cycles 🟢 → 🟡
        # independently per message and never stalls on a shared resource.
        for _ in range(3):
            msg = _make_message()
            sm = StatusManager(msg)
            await sm.set_running()
            await sm.set_done()
            adds = [c.args[0] for c in msg.add_reaction.await_args_list]
            assert adds == [EMOJI_RUNNING, EMOJI_WAITING]
            assert sm._current_emoji == EMOJI_WAITING
            await sm.cleanup()
