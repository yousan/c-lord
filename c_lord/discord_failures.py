"""What a decorative Discord call can fail with without anything being wrong (#904/#930).

``discord.HTTPException`` is only what Discord *answered* with. When the network
itself blips — DNS cannot resolve, the connection is refused, a request times
out — aiohttp and asyncio raise their own types, which are not
``HTTPException``. A decoration (a notice, a reaction, a dashboard edit, a
progress line) that catches only ``HTTPException`` then lets a momentary outage
take down the turn or handler it was decorating
(``docs/specs/turn-resilience.md``).

Catch :data:`DISCORD_CALL_FAILURES` where a failure must stay a missing
decoration. Do not use it where the call *is* the work (posting Claude's
answer, creating the thread a turn runs in): there a network error has to
surface.
"""

from __future__ import annotations

import asyncio

import aiohttp
import discord

# ``asyncio.TimeoutError`` is spelled out: before 3.11 it is not an OSError.
DISCORD_CALL_FAILURES: tuple[type[BaseException], ...] = (
    discord.HTTPException,
    aiohttp.ClientError,
    asyncio.TimeoutError,
    OSError,
)
