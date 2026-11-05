"""A busy thread's read-back still reaches the requests sent while c-lord was away (#1248).

On a restart c-lord reads each thread back to pick up what was posted while no
process was running (#776). It read the *oldest* 100 messages from an hour
before the restart. In a thread where sub-agent lines and voices (#971) run past
100 messages an hour, those 100 were all the previous process's own posts, and
the seconds the bot was down — the only part that can hold a missed request —
were never read. Such a request got no reaction and no reply.

Production 2026-11-05, thread=1566967064349114438 (sub-agent voices on):

    12:23:46 [WARNING] c_lord.cogs.claude_chat: [thread=1566967064349114438] read back
             only the first 100 messages posted since 2026-11-05 11:23:39 (#745)

The thread had 205 messages between 11:23 and 12:23:40; the read stopped at 11:52:42.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from c_lord.gateway_backfill import HISTORY_LIMIT, Startup
from tests.test_claude_chat import _make_cog
from tests.test_claude_chat_gaps import _record, _thread

UTC = timezone.utc  # noqa: UP017
JST = timezone(timedelta(hours=9))
STARTED = datetime(2026, 11, 5, 12, 23, 39, tzinfo=JST).astimezone(UTC)
UP = STARTED + timedelta(seconds=5)
BOT_ID = 777


def _msg(at: datetime, *, bot: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=discord.utils.time_snowflake(at),
        created_at=at,
        author=SimpleNamespace(id=BOT_ID if bot else 5, bot=bot),
        reactions=[],
    )


class _History:
    """``thread.history`` over *rows*, honouring ``limit`` / ``after`` / ``oldest_first``."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows

    async def __call__(self, *, limit: int | None, after: datetime, oldest_first: bool):  # noqa: ANN204
        rows = [m for m in self.rows if m.created_at > after]
        rows.sort(key=lambda m: m.id, reverse=not oldest_first)
        for m in rows if limit is None else rows[:limit]:
            yield m


def _cog_and_thread(history: _History):  # noqa: ANN202
    cog = _make_cog()
    cog.bot.user = SimpleNamespace(id=BOT_ID)
    thread = _thread(1566967064349114438)
    thread.history = history
    cog._is_runnable_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda m: not m.author.bot
    )
    cog._handle_thread_reply = AsyncMock()  # type: ignore[method-assign]
    return cog, thread


async def test_request_posted_during_the_restart_is_picked_up_in_a_busy_thread() -> None:
    since = STARTED - timedelta(hours=1)
    # 204 bot posts across the hour before the restart (sub-agent lines, voices,
    # progress), the last one 20 s before the old process went down …
    posts = [_msg(since + timedelta(seconds=17 * i + 1), bot=True) for i in range(204)]
    assert posts[-1].created_at < STARTED - timedelta(seconds=10)
    # … and one request that arrived while no process was running.
    request = _msg(STARTED - timedelta(seconds=2), bot=False)
    history = _History(posts + [request])
    cog, thread = _cog_and_thread(history)

    got = await cog._pick_up_thread(
        thread,
        _record(),
        since=since,
        down_at=STARTED - timedelta(seconds=5),
        back_at=UP,
        cause="restart",
        startup=Startup(started_at=STARTED, up_at=UP, since=since),
    )

    assert HISTORY_LIMIT == 100
    assert got == 1, (
        "the request posted while c-lord was down was never read: the read-back took "
        "the oldest 100 messages of the hour before the restart"
    )
    cog._handle_thread_reply.assert_awaited_once()
    assert cog._handle_thread_reply.await_args.args[0] is request


async def test_a_long_outage_in_a_busy_thread_keeps_the_newest_requests() -> None:
    """The bound counts back from the present, so what it cuts is the oldest end.

    The mirror keeps posting over REST while the gateway is down, so a long
    outage in a busy thread is mostly the bot's own lines.
    """
    since = STARTED - timedelta(hours=6)
    rows = [_msg(since + timedelta(seconds=10 * i + 1), bot=True) for i in range(2000)]
    # Asked a few hundred messages before the present — past one page of 100.
    request = _msg(rows[1500].created_at + timedelta(seconds=1), bot=False)
    rows.append(request)
    cog, thread = _cog_and_thread(_History(rows))

    got = await cog._pick_up_thread(
        thread,
        _record(),
        since=since,
        down_at=since,
        back_at=UP,
        cause="gateway",
    )

    assert got == 1
    assert cog._handle_thread_reply.await_args.args[0] is request


async def test_requests_before_the_restart_mark_are_still_not_rerun() -> None:
    """A request the previous process handled (its post came after) is not picked up."""
    since = STARTED - timedelta(hours=1)
    handled = _msg(since + timedelta(minutes=5), bot=False)
    answer = _msg(since + timedelta(minutes=6), bot=True)
    missed = _msg(STARTED - timedelta(seconds=3), bot=False)
    cog, thread = _cog_and_thread(_History([handled, answer, missed]))

    got = await cog._pick_up_thread(
        thread,
        _record(),
        since=since,
        down_at=STARTED - timedelta(seconds=5),
        back_at=UP,
        cause="restart",
        startup=Startup(started_at=STARTED, up_at=UP, since=since),
    )

    assert got == 1
    assert cog._handle_thread_reply.await_args.args[0] is missed
    assert list(cog._handle_thread_reply.await_args.kwargs["earlier"]) == []
