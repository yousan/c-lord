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


class _HttpFakeMessage:
    """A message that models Discord's HTTP semantics for reactions.

    The reaction is applied on the server the moment the request goes out; the
    coroutine then awaits the *response*. That gap is where #718 lives: a
    ``task.cancel()`` landing there stops our bookkeeping but does **not**
    un-send the request, so Discord ends up showing an emoji the manager does
    not know about.
    """

    def __init__(self) -> None:
        self.reactions: list[str] = []
        self.guild = MagicMock()
        self._gates: dict[str, asyncio.Event] = {}
        self._sent: dict[str, asyncio.Event] = {}

    def hold(self, emoji: str) -> asyncio.Event:
        """Make ``add_reaction(emoji)`` hang while awaiting its response.

        Returns an event that fires once the request has gone out (i.e. the
        emoji is already on the message and the caller is awaiting the reply).
        """
        self._gates[emoji] = asyncio.Event()
        self._sent[emoji] = asyncio.Event()
        return self._sent[emoji]

    def release(self, emoji: str) -> None:
        self._gates[emoji].set()

    async def add_reaction(self, emoji: str) -> None:
        if emoji not in self.reactions:
            self.reactions.append(emoji)  # the server applied it
        sent = self._sent.get(emoji)
        if sent is not None:
            sent.set()
        gate = self._gates.get(emoji)
        if gate is not None:
            await gate.wait()  # still awaiting the response
        else:
            await asyncio.sleep(0)

    async def remove_reaction(self, emoji: str, member: object) -> None:
        if emoji in self.reactions:
            self.reactions.remove(emoji)
        await asyncio.sleep(0)


class TestStallOverrideIsTemporary:
    """#718: ⏳/⚠️ are *temporary* overrides — a finished turn ends on 🟡 alone.

    The stall monitor runs in its own task, so its paint can overlap the end of
    the turn. Before the fix that overlap left either two lamps (⚠️ *and* 🟡) or
    the override alone, and the user could not tell whose turn it was.
    """

    @pytest.mark.asyncio
    async def test_a_turn_ending_mid_stall_paint_leaves_only_the_waiting_lamp(self) -> None:
        """AC1/AC4②: ⚠️ already sent to Discord, then the turn ends → 🟡 alone."""
        msg = _HttpFakeMessage()
        in_flight = msg.hold(EMOJI_STALL_HARD)
        sm = StatusManager(msg)  # type: ignore[arg-type]
        await sm.set_running()
        sm._last_activity = asyncio.get_running_loop().time() - STALL_HARD_SECONDS - 1
        await asyncio.wait_for(in_flight.wait(), timeout=10)
        assert msg.reactions == [EMOJI_STALL_HARD]  # ⚠️ is on the message already

        finish = asyncio.create_task(sm.set_waiting())  # the turn ends right now
        await asyncio.sleep(0)
        msg.release(EMOJI_STALL_HARD)
        await asyncio.wait_for(finish, timeout=10)
        await asyncio.sleep(0.05)

        assert msg.reactions == [EMOJI_WAITING]
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_an_error_ending_mid_stall_paint_leaves_only_the_error_lamp(self) -> None:
        """AC1: the same guarantee for the ❌ ending."""
        msg = _HttpFakeMessage()
        in_flight = msg.hold(EMOJI_STALL_HARD)
        sm = StatusManager(msg)  # type: ignore[arg-type]
        await sm.set_running()
        sm._last_activity = asyncio.get_running_loop().time() - STALL_HARD_SECONDS - 1
        await asyncio.wait_for(in_flight.wait(), timeout=10)

        finish = asyncio.create_task(sm.set_error())
        await asyncio.sleep(0)
        msg.release(EMOJI_STALL_HARD)
        await asyncio.wait_for(finish, timeout=10)
        await asyncio.sleep(0.05)

        assert msg.reactions == [EMOJI_ERROR]
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_the_monitor_is_finished_when_the_final_lamp_is_painted(self) -> None:
        """AC2/AC4①: `set_waiting()` must not merely *request* the cancel.

        A cancel that is only requested leaves the monitor free to run up to its
        next await — including the rest of an ``add_reaction(⚠️)`` — after the
        turn's final lamp has been decided.
        """
        msg = _make_message()
        sm = StatusManager(msg)
        await sm.set_running()
        sm._last_activity = asyncio.get_running_loop().time() - STALL_HARD_SECONDS - 1
        await asyncio.sleep(2.5)
        assert sm._current_emoji == EMOJI_STALL_HARD
        monitor = sm._stall_task

        await sm.set_waiting()

        assert monitor is not None and monitor.done()
        await asyncio.sleep(0.05)
        assert sm._current_emoji == EMOJI_WAITING
        await sm.cleanup()

    @pytest.mark.asyncio
    async def test_a_late_stall_paint_is_dropped(self) -> None:
        """AC2: a stall paint that lands after the turn ended is a no-op."""
        msg = _HttpFakeMessage()
        sm = StatusManager(msg)  # type: ignore[arg-type]
        await sm.set_running()
        await sm.set_waiting()

        await sm._paint_stall(EMOJI_STALL_HARD)

        assert msg.reactions == [EMOJI_WAITING]
        await sm.cleanup()
