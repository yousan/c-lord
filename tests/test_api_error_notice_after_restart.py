"""The ❌ line for a self-started turn after a bot restart (#1212).

#1178 made the API error ❌ (#1131/#1149) ping the thread's last requester, but
who that was lived only in this process's memory. Production restarts several
times a day, so a thread that was asked something, then saw c-lord restart, then
had Claude resume itself on a ``<task-notification>`` that ended on an API error
pinged the owner instead of the person who asked — until that person happened
to send something again.

The last turn's trigger message is already on record
(``sessions.trigger_message_id``, #115); its author is who to ping. A webhook,
a bot or c-lord's own seed message is nobody to ping: the owner, as before
(#520/#525).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from c_lord import turn_events
from c_lord.cogs.transcript_mirror import TranscriptMirrorCog

OWNER = 1
BOT_SELF = 99
THREAD = 42
TRIGGER = 555


def _repo(trigger_message_id: int | None) -> MagicMock:
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[])
    record = MagicMock()
    record.trigger_message_id = trigger_message_id
    repo.get = AsyncMock(return_value=record)
    return repo


def _message(author_id: int, *, bot: bool = False, webhook_id: int | None = None) -> MagicMock:
    message = MagicMock()
    message.id = TRIGGER
    message.webhook_id = webhook_id
    message.author = SimpleNamespace(id=author_id, bot=bot, name="x")
    return message


async def _ping_after_restart(monkeypatch, message: MagicMock | None, **kw) -> str:  # noqa: ANN001
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    bot = MagicMock()
    bot.owner_id = OWNER
    bot.user = SimpleNamespace(id=BOT_SELF)
    channel = MagicMock()
    channel.send = AsyncMock()
    if message is None:
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "gone")
        )
        channel.parent = None
    else:
        channel.fetch_message = AsyncMock(return_value=message)
    bot.get_channel.return_value = channel
    # The restart: whatever this process remembered about the thread is gone.
    turn_events.reset()
    try:
        cog = TranscriptMirrorCog(bot, session_repo=_repo(kw.get("trigger", TRIGGER)))
        await cog._make_api_error_sink(THREAD)("API Error: The response stopped arriving.")
    finally:
        turn_events.reset()
    return channel.send.call_args.args[0]


async def test_pings_the_last_requester_after_a_restart(monkeypatch) -> None:  # noqa: ANN001
    body = await _ping_after_restart(monkeypatch, _message(777))
    assert body.endswith("<@777>"), body


async def test_a_webhook_trigger_still_pings_the_owner(monkeypatch) -> None:  # noqa: ANN001
    body = await _ping_after_restart(monkeypatch, _message(777, webhook_id=5))
    assert body.endswith(f"<@{OWNER}>"), body


async def test_a_bot_trigger_still_pings_the_owner(monkeypatch) -> None:  # noqa: ANN001
    body = await _ping_after_restart(monkeypatch, _message(777, bot=True))
    assert body.endswith(f"<@{OWNER}>"), body


async def test_c_lords_own_seed_message_pings_the_owner(monkeypatch) -> None:  # noqa: ANN001
    body = await _ping_after_restart(monkeypatch, _message(BOT_SELF))
    assert body.endswith(f"<@{OWNER}>"), body


async def test_a_trigger_that_cannot_be_read_pings_the_owner(monkeypatch) -> None:  # noqa: ANN001
    body = await _ping_after_restart(monkeypatch, None)
    assert body.endswith(f"<@{OWNER}>"), body


async def test_no_trigger_on_record_pings_the_owner(monkeypatch) -> None:  # noqa: ANN001
    body = await _ping_after_restart(monkeypatch, _message(777), trigger=None)
    assert body.endswith(f"<@{OWNER}>"), body


async def test_what_this_process_remembers_still_wins(monkeypatch) -> None:  # noqa: ANN001
    """A person who asked since the restart is the requester, not the old trigger."""
    monkeypatch.delenv(turn_events.ENV_LOG, raising=False)
    monkeypatch.delenv(turn_events.ENV_URL, raising=False)
    bot = MagicMock()
    bot.owner_id = OWNER
    bot.user = SimpleNamespace(id=BOT_SELF)
    channel = MagicMock()
    channel.send = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=_message(777))
    bot.get_channel.return_value = channel
    turn_events.reset()
    try:
        thread = SimpleNamespace(id=THREAD, parent_id=1, guild=None)
        turn_events.begin_turn(thread, turn_events.StartedBy("user", "888"))
        cog = TranscriptMirrorCog(bot, session_repo=_repo(TRIGGER))
        await cog._make_api_error_sink(THREAD)("API Error: The response stopped arriving.")
    finally:
        turn_events.reset()
    assert channel.send.call_args.args[0].endswith("<@888>")
    channel.fetch_message.assert_not_awaited()
