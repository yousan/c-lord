"""#930: a decorative Discord call that hits a network error must not take its caller down.

``discord.HTTPException`` is only what Discord answered with. A DNS failure, a
refused connection or a timeout surfaces as ``aiohttp.ClientError`` /
``asyncio.TimeoutError`` / ``OSError`` instead, and every decoration that caught
only ``HTTPException`` let it through (#904 fixed the lamp; these are the rest).
One representative per shape of caller.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

from c_lord.discord_failures import DISCORD_CALL_FAILURES

NETWORK_ERRORS = [
    aiohttp.ClientConnectionError("Cannot connect to host discord.com:443"),
    TimeoutError(),
    OSError("Temporary failure in name resolution"),
]


@pytest.mark.parametrize("exc", NETWORK_ERRORS, ids=lambda e: type(e).__name__)
def test_the_shared_tuple_covers_network_errors(exc: BaseException) -> None:
    assert isinstance(exc, DISCORD_CALL_FAILURES)
    assert isinstance(discord.HTTPException(MagicMock(), "x"), DISCORD_CALL_FAILURES)


@pytest.mark.parametrize("exc", NETWORK_ERRORS, ids=lambda e: type(e).__name__)
async def test_rerun_button_still_reruns_when_the_ack_fails(exc: BaseException) -> None:
    """views.py: a failed defer used to skip the callback while the double-click
    guard was already set — the ⚡ button could then never run the instruction."""
    from c_lord.discord_ui.views import TextAnsweredMenuView

    on_rerun = AsyncMock()
    view = TextAnsweredMenuView(on_rerun)
    interaction = MagicMock()
    interaction.response.defer = AsyncMock(side_effect=exc)
    interaction.message.edit = AsyncMock(side_effect=exc)

    await view.rerun_button.callback(interaction)  # type: ignore[call-arg]

    on_rerun.assert_awaited_once_with(interaction)


@pytest.mark.parametrize("exc", NETWORK_ERRORS, ids=lambda e: type(e).__name__)
async def test_reopen_button_still_reopens_when_the_ack_fails(exc: BaseException) -> None:
    from c_lord.discord_ui.views import ReopenSessionView

    on_reopen = AsyncMock()
    view = ReopenSessionView(on_reopen)
    interaction = MagicMock()
    interaction.response.defer = AsyncMock(side_effect=exc)
    interaction.message.edit = AsyncMock(side_effect=exc)

    await view.reopen_button.callback(interaction)  # type: ignore[call-arg]

    on_reopen.assert_awaited_once_with(interaction)


@pytest.mark.parametrize("exc", NETWORK_ERRORS, ids=lambda e: type(e).__name__)
async def test_bot_lookup_falls_back_like_an_http_error(exc: BaseException) -> None:
    """thread_owner.py: the same "not another bot" answer an HTTP error gets."""
    from c_lord.thread_owner import _is_bot_user

    bot = MagicMock()
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(side_effect=exc)

    assert await _is_bot_user(bot, 42) is False


@pytest.mark.parametrize("exc", NETWORK_ERRORS, ids=lambda e: type(e).__name__)
async def test_slash_reply_still_goes_out_when_the_placeholder_cannot_be_deleted(
    exc: BaseException,
) -> None:
    """slash_io.py: same branch as an HTTP error — warn, then send the followup."""
    from c_lord.discord_ui import slash_io

    interaction = MagicMock()
    interaction.extras = {slash_io._PUBLIC_PLACEHOLDER: True}
    interaction.delete_original_response = AsyncMock(side_effect=exc)
    interaction.followup.send = AsyncMock()

    await slash_io.followup(interaction, "hi", ephemeral=True)

    interaction.followup.send.assert_awaited_once()


@pytest.mark.parametrize("exc", NETWORK_ERRORS, ids=lambda e: type(e).__name__)
async def test_one_cleanup_notice_failing_does_not_drop_the_rest(exc: BaseException) -> None:
    """session_cleanup.py: the rows are already deleted — a raise would lose every
    notice still queued behind it."""
    from c_lord.cogs.session_cleanup import SessionCleanupCog
    from c_lord.database.repository import SessionRecord

    def rec(tid: int) -> SessionRecord:
        return SessionRecord(
            thread_id=tid,
            session_id="s",
            working_dir=None,
            model=None,
            origin="discord",
            summary=None,
            created_at="2026-05-18 10:00:00",
            last_used_at="2026-06-18 11:00:00",
        )

    broken, fine = MagicMock(spec=discord.Thread), MagicMock(spec=discord.Thread)
    broken.archived = fine.archived = False
    broken.send = AsyncMock(side_effect=exc)
    fine.send = AsyncMock()
    fine.edit = AsyncMock()
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(side_effect=lambda tid: {1: broken, 2: fine}[tid])
    cog = SessionCleanupCog(bot)
    cog._post_delay = 0

    cog.announce([rec(1), rec(2)])
    await cog.flush()

    fine.send.assert_awaited_once()
