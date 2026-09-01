"""#632: a failing dashboard update must not take the whole turn down.

The dashboard embed is decoration. When ``set_state`` raises — a closed aiohttp
session, a revoked permission, a Discord outage — the turn used to die before
``run_claude_with_config`` was ever reached, so the user's message vanished
without a reply, without ❌, without anything.
"""

from __future__ import annotations

import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.discord_ui.thread_dashboard import ThreadState


class _Stop(BaseException):
    """Sentinel raised to halt _run_claude once Claude has been reached."""


def _message() -> MagicMock:
    author = MagicMock()
    author.id = 4242
    author.display_name = "yousan"
    author.name = "yousan"
    author.bot = False

    m = MagicMock(spec=discord.Message)
    m.id = 77
    m.author = author
    m.add_reaction = AsyncMock()
    m.remove_reaction = AsyncMock()
    m.clear_reaction = AsyncMock()
    return m


def _make_cog() -> ClaudeChatCog:
    bot = MagicMock()
    bot.channel_id = 999
    bot.owner_id = None
    bot.settings_repo = None
    bot.transcript_mirror_cog = None
    bot.user = MagicMock(id=1111)
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    repo.update_trigger_message = AsyncMock()
    runner = MagicMock()
    runner.working_dir = "/tmp/work"
    runner.model = None
    runner.timeout_seconds = 60
    runner.effort = None
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner)


async def _run_turn(
    cog: ClaudeChatCog,
    dashboard: MagicMock,
    thread_send: AsyncMock | None = None,
) -> MagicMock:
    """Drive ``_run_claude`` until Claude is invoked (or the turn dies first)."""
    sdm = MagicMock()
    sdm.create_session_dir = MagicMock(return_value="/tmp/work")
    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w1")

    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    cog._get_dashboard = MagicMock(return_value=dashboard)  # type: ignore[method-assign]
    cog._get_coordination = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_current_model = AsyncMock(return_value=None)  # type: ignore[method-assign]
    cog._apply_thread_naming = AsyncMock()  # type: ignore[method-assign]

    thread = MagicMock(spec=discord.Thread)
    thread.id = 501
    thread.parent_id = 500
    thread.send = thread_send or AsyncMock(return_value=MagicMock())

    run_config = AsyncMock(side_effect=_Stop)
    with (
        patch("c_lord.cogs.claude_chat.run_claude_with_config", run_config),
        contextlib.suppress(BaseException),
    ):
        await cog._run_claude(_message(), thread, "hi", None)
    return run_config


def _exploding_dashboard(exc: BaseException) -> MagicMock:
    """A dashboard whose PROCESSING update always fails."""
    dashboard = MagicMock()

    async def _set_state(_thread_id: int, state: ThreadState, *_a: object, **_kw: object) -> None:
        if state is ThreadState.PROCESSING:
            raise exc

    dashboard.set_state = AsyncMock(side_effect=_set_state)
    return dashboard


class TestDashboardFailureDoesNotKillTheTurn:
    @pytest.mark.parametrize(
        "exc",
        [
            discord.HTTPException(MagicMock(status=503), "Service Unavailable"),
            RuntimeError("Session is closed"),
        ],
        ids=["http", "runtime"],
    )
    async def test_claude_still_runs(self, exc: BaseException) -> None:
        """AC1/AC3 — the turn reaches Claude even though the embed update failed."""
        run_config = await _run_turn(_make_cog(), _exploding_dashboard(exc))
        run_config.assert_awaited_once()

    async def test_failure_is_logged_as_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """AC2 — swallowed, but never silently."""
        with caplog.at_level(logging.WARNING, logger="c_lord.cogs.claude_chat"):
            await _run_turn(_make_cog(), _exploding_dashboard(RuntimeError("Session is closed")))

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "the swallowed dashboard failure must be logged"
        assert any("dashboard" in r.getMessage().lower() for r in warnings)


class TestNoticeFailureDoesNotKillTheTurn:
    """AC4 — the other decoration posts on the same turn path (#632)."""

    async def test_a_failing_session_running_notice_still_runs_claude(self) -> None:
        """The Stop-button notice is decoration; losing it must not lose the turn."""
        send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=429), "rate limited"))
        run_config = await _run_turn(_make_cog(), MagicMock(set_state=AsyncMock()), send)
        run_config.assert_awaited_once()
