"""Emoji reaction status manager.

Shows Claude's per-turn status as a single reaction on the user's trigger
message: 🟢 *running* while the turn is active, 🟡 *waiting* once it's the
user's turn again. ❌ error, ⏳/⚠️ stall and 🗜️ compact are temporary
overrides shown while that condition holds.

Reactions live in a different Discord rate-limit bucket than thread renames,
so they stay responsive even under heavy use (#246). The thread-name lamp
(🟢/🟡 in ``<emoji> W<N> │ <topic>``) is the low-frequency, eventually
consistent "sidebar" view driven by the state-sync poll — it is no longer
flipped per turn, which is what saturated Discord's ~2-renames-per-10-min
limit and made the lamp stick (#236 regression).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import discord

from ..claude.types import ToolCategory  # noqa: F401  — kept for set_tool signature

logger = logging.getLogger(__name__)

# Per-turn lamp (the message reaction).
EMOJI_RUNNING = "\U0001f7e2"  # 🟢 — Claude is actively working
EMOJI_WAITING = "\U0001f7e1"  # 🟡 — turn finished, your move
# Temporary overrides that replace the lamp while a special condition holds.
EMOJI_ERROR = "❌"  # ❌
EMOJI_STALL_SOFT = "⏳"  # ⏳
EMOJI_STALL_HARD = "⚠️"  # ⚠️
EMOJI_COMPACT = "\U0001f5dc️"  # 🗜️

STALL_SOFT_SECONDS = 10
STALL_HARD_SECONDS = 30


class StatusManager:
    """Manages a single emoji reaction on a Discord message to show Claude's
    per-turn status.

    Lifecycle: 🟢 running (set at turn start, kept through thinking and tool
    work) → 🟡 waiting (set when the turn finishes). Only one reaction is shown
    at a time.

    Transitions apply immediately — there is no debounce. The lamp only changes
    a couple of times per turn (🟢 at the start, 🟡 at the end, plus the rare
    override), and the reaction rate-limit bucket easily absorbs that. Repeated
    "still working" calls collapse to a no-op because the target equals the
    current reaction.
    """

    def __init__(self, message: discord.Message) -> None:
        self._message = message
        self._current_emoji: str | None = None
        self._stall_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_activity = asyncio.get_running_loop().time()

    async def set_running(self) -> None:
        """🟢 — Claude is actively working (turn start)."""
        await self._set_reaction(EMOJI_RUNNING)
        self._start_stall_timer()

    async def set_thinking(self) -> None:
        """Alias for :meth:`set_running` — work continues, lamp stays 🟢."""
        await self.set_running()

    async def set_tool(self, category: ToolCategory | None = None) -> None:
        """Work continues on a tool — keep 🟢 and note activity.

        ``category`` is accepted for backward compatibility but no longer maps
        to a per-tool emoji (#246): the tool detail is already shown by the
        per-tool embeds posted in the thread, so duplicating it on the reaction
        only added churn (and reaction edits).
        """
        await self._set_reaction(EMOJI_RUNNING)
        self._reset_stall_timer()

    async def set_done(self) -> None:
        """🟡 — the turn finished; it's the user's turn now."""
        await self.set_waiting()

    async def set_waiting(self) -> None:
        """🟡 — flip the lamp to *waiting for user input*."""
        self._cancel_stall_timer()
        await self._set_reaction(EMOJI_WAITING)

    async def set_compact(self) -> None:
        """🗜️ — context compaction in progress (temporary override)."""
        await self._set_reaction(EMOJI_COMPACT)
        self._reset_stall_timer()

    async def set_error(self) -> None:
        """❌ — the turn ended in an error (temporary override, left visible)."""
        self._cancel_stall_timer()
        await self._set_reaction(EMOJI_ERROR)

    async def cleanup(self) -> None:
        """Remove the current status reaction."""
        self._cancel_stall_timer()
        async with self._lock:
            await self._remove_current_locked()
            self._current_emoji = None

    async def _set_reaction(self, emoji: str) -> None:
        """Replace the current reaction with ``emoji`` (immediate, single-flight)."""
        async with self._lock:
            if self._current_emoji == emoji:
                return
            await self._remove_current_locked()
            with contextlib.suppress(discord.HTTPException):
                await self._message.add_reaction(emoji)
            self._current_emoji = emoji

    async def _remove_current_locked(self) -> None:
        """Remove the bot's current reaction. Caller must hold ``self._lock``."""
        if self._current_emoji:
            with contextlib.suppress(discord.HTTPException, AttributeError):
                guild = self._message.guild
                if guild:
                    await self._message.remove_reaction(self._current_emoji, guild.me)

    def _start_stall_timer(self) -> None:
        """Start the stall detection timer."""
        self._cancel_stall_timer()
        self._last_activity = asyncio.get_running_loop().time()
        self._stall_task = asyncio.create_task(self._stall_monitor())

    def _reset_stall_timer(self) -> None:
        """Reset the stall timer (activity detected)."""
        self._last_activity = asyncio.get_running_loop().time()

    def _cancel_stall_timer(self) -> None:
        """Cancel the stall timer."""
        if self._stall_task and not self._stall_task.done():
            self._stall_task.cancel()

    async def _stall_monitor(self) -> None:
        """Monitor for stall conditions and override the lamp accordingly."""
        soft_warned = False
        while True:
            await asyncio.sleep(2)
            elapsed = asyncio.get_running_loop().time() - self._last_activity

            if elapsed >= STALL_HARD_SECONDS and self._current_emoji != EMOJI_STALL_HARD:
                # #473: the ⚠️ lamp is the *only* thing a hard stall shows.
                # It used to also post a prose line into the thread saying the
                # same thing — a warning for what is a normal long think, and
                # repeatable several times in one turn.
                await self._set_reaction(EMOJI_STALL_HARD)
            elif (
                elapsed >= STALL_SOFT_SECONDS
                and not soft_warned
                and self._current_emoji != EMOJI_STALL_HARD
            ):
                await self._set_reaction(EMOJI_STALL_SOFT)
                soft_warned = True
