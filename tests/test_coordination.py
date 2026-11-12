"""Session announcements in the shared coordination channel (``c_lord.coordination``).

The announcement is a courtesy: it is posted when a channel is configured and
can be found, and nothing that goes wrong on the way may reach the caller —
the caller is a turn that has just finished.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

import c_lord.coordination as coordination_package
from c_lord.coordination import CoordinationService

COORDINATION_CHANNEL = 424242


def _bot(channel: MagicMock | None) -> MagicMock:
    """A bot whose channel cache answers every lookup with *channel*."""
    bot = MagicMock(spec=discord.Client)
    bot.get_channel = MagicMock(return_value=channel)
    return bot


def _channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    return channel


def _thread(name: str = "refactor the parser") -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.name = name
    return thread


def test_the_package_exports_only_the_service() -> None:
    assert coordination_package.__all__ == ["CoordinationService"]


class TestEnabled:
    def test_on_when_a_channel_is_configured(self) -> None:
        assert CoordinationService(_bot(None), channel_id=COORDINATION_CHANNEL).enabled is True

    def test_off_without_a_channel(self) -> None:
        assert CoordinationService(_bot(None), channel_id=None).enabled is False

    def test_the_channel_may_be_given_positionally(self) -> None:
        assert CoordinationService(_bot(None), COORDINATION_CHANNEL).enabled is True


class TestSessionEnd:
    async def test_names_the_thread_in_the_coordination_channel(self) -> None:
        channel = _channel()
        bot = _bot(channel)
        service = CoordinationService(bot, channel_id=COORDINATION_CHANNEL)

        await service.post_session_end(_thread("refactor the parser"))

        bot.get_channel.assert_called_once_with(COORDINATION_CHANNEL)
        channel.send.assert_awaited_once_with("✅ **refactor the parser** セッション終了")

    async def test_without_a_channel_discord_is_not_touched(self) -> None:
        """Disabled means disabled: not even the channel cache is consulted."""
        bot = _bot(_channel())
        service = CoordinationService(bot, channel_id=None)

        await service.post_session_end(_thread())

        bot.get_channel.assert_not_called()

    async def test_a_channel_missing_from_the_cache_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The bot may not be able to see the configured channel; the turn still ends cleanly."""
        service = CoordinationService(_bot(None), channel_id=COORDINATION_CHANNEL)

        with caplog.at_level(logging.WARNING, logger="c_lord.coordination.service"):
            await service.post_session_end(_thread())

        assert f"Coordination channel {COORDINATION_CHANNEL} not found" in caplog.text

    @pytest.mark.parametrize(
        "failure",
        [
            discord.HTTPException(MagicMock(status=500, reason="boom"), "server error"),
            aiohttp.ClientConnectionError("connection reset"),
            OSError("network unreachable"),
        ],
        ids=["discord-http", "aiohttp", "os"],
    )
    async def test_a_failed_post_is_logged_not_raised(
        self, failure: BaseException, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = _channel()
        channel.send.side_effect = failure
        service = CoordinationService(_bot(channel), channel_id=COORDINATION_CHANNEL)

        with caplog.at_level(logging.WARNING, logger="c_lord.coordination.service"):
            await service.post_session_end(_thread())

        assert f"Failed to post to coordination channel {COORDINATION_CHANNEL}" in caplog.text

    async def test_a_programming_error_is_not_swallowed(self) -> None:
        """Only delivery failures are forgiven; a bug in the call still surfaces."""
        channel = _channel()
        channel.send.side_effect = ValueError("not a delivery problem")
        service = CoordinationService(_bot(channel), channel_id=COORDINATION_CHANNEL)

        with pytest.raises(ValueError):
            await service.post_session_end(_thread())
