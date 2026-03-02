"""Tests for setup_bridge() auto-discovery function."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.setup import BridgeComponents, setup_bridge


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.loop = MagicMock()
    bot.add_cog = AsyncMock()
    return bot


def _make_runner() -> MagicMock:
    runner = MagicMock()
    runner.clone.return_value = runner
    return runner


@pytest.mark.asyncio
async def test_setup_bridge_registers_core_cogs(tmp_path: object) -> None:
    """setup_bridge should register ClaudeChatCog, SessionManageCog, SkillCommandCog."""
    bot = _make_bot()
    runner = _make_runner()

    result = await setup_bridge(
        bot,
        runner,
        session_db_path=str(tmp_path / "sessions.db"),  # type: ignore[operator]
        claude_channel_id=12345,
        enable_scheduler=False,
    )

    cog_names = [call.args[0].__class__.__name__ for call in bot.add_cog.call_args_list]
    assert "ClaudeChatCog" in cog_names
    assert "SessionManageCog" in cog_names
    assert "SkillCommandCog" in cog_names
    assert isinstance(result, BridgeComponents)


@pytest.mark.asyncio
async def test_setup_bridge_registers_scheduler_when_enabled(tmp_path: object) -> None:
    """setup_bridge should register SchedulerCog when enable_scheduler=True."""
    bot = _make_bot()
    runner = _make_runner()

    result = await setup_bridge(
        bot,
        runner,
        session_db_path=str(tmp_path / "sessions.db"),  # type: ignore[operator]
        enable_scheduler=True,
        task_db_path=str(tmp_path / "tasks.db"),  # type: ignore[operator]
    )

    cog_names = [call.args[0].__class__.__name__ for call in bot.add_cog.call_args_list]
    assert "SchedulerCog" in cog_names
    assert result.task_repo is not None


@pytest.mark.asyncio
async def test_setup_bridge_skips_scheduler_when_disabled(tmp_path: object) -> None:
    """setup_bridge should NOT register SchedulerCog when enable_scheduler=False."""
    bot = _make_bot()
    runner = _make_runner()

    result = await setup_bridge(
        bot,
        runner,
        session_db_path=str(tmp_path / "sessions.db"),  # type: ignore[operator]
        enable_scheduler=False,
    )

    cog_names = [call.args[0].__class__.__name__ for call in bot.add_cog.call_args_list]
    assert "SchedulerCog" not in cog_names
    assert result.task_repo is None


@pytest.mark.asyncio
async def test_setup_bridge_returns_components(tmp_path: object) -> None:
    """setup_bridge should return BridgeComponents with session_repo."""
    bot = _make_bot()
    runner = _make_runner()

    result = await setup_bridge(
        bot,
        runner,
        session_db_path=str(tmp_path / "sessions.db"),  # type: ignore[operator]
        enable_scheduler=False,
    )

    assert isinstance(result, BridgeComponents)
    assert result.session_repo is not None
    assert result.session_repo.db_path == str(tmp_path / "sessions.db")  # type: ignore[operator]


@pytest.mark.asyncio
async def test_setup_bridge_skips_skill_cog_without_channel_id(tmp_path: object) -> None:
    """setup_bridge should skip SkillCommandCog when claude_channel_id is None."""
    bot = _make_bot()
    runner = _make_runner()

    await setup_bridge(
        bot,
        runner,
        session_db_path=str(tmp_path / "sessions.db"),  # type: ignore[operator]
        claude_channel_id=None,
        enable_scheduler=False,
    )

    cog_names = [call.args[0].__class__.__name__ for call in bot.add_cog.call_args_list]
    assert "SkillCommandCog" not in cog_names


# ---------------------------------------------------------------------------
# apply_to_api_server()
# ---------------------------------------------------------------------------


def _make_api_server() -> MagicMock:
    server = MagicMock()
    server.task_repo = None
    server.lounge_repo = None
    server.port = 8099
    return server


def test_apply_to_api_server_wires_task_and_lounge_repos(tmp_path: object) -> None:
    """apply_to_api_server should set task_repo and lounge_repo on the ApiServer."""
    from c_lord.database.lounge_repo import LoungeRepository
    from c_lord.database.repository import SessionRepository
    from c_lord.database.task_repo import TaskRepository

    session_repo = MagicMock(spec=SessionRepository)
    task_repo = MagicMock(spec=TaskRepository)
    lounge_repo = MagicMock(spec=LoungeRepository)

    components = BridgeComponents(
        session_repo=session_repo,
        task_repo=task_repo,
        lounge_repo=lounge_repo,
    )
    api_server = _make_api_server()

    components.apply_to_api_server(api_server)

    assert api_server.task_repo is task_repo
    assert api_server.lounge_repo is lounge_repo


def test_apply_to_api_server_skips_none_repos() -> None:
    """apply_to_api_server should not overwrite existing repos with None."""
    from c_lord.database.repository import SessionRepository

    session_repo = MagicMock(spec=SessionRepository)
    components = BridgeComponents(
        session_repo=session_repo,
        task_repo=None,
        lounge_repo=None,
    )
    api_server = _make_api_server()
    existing_task_repo = MagicMock()
    api_server.task_repo = existing_task_repo

    components.apply_to_api_server(api_server)

    # None repos must not overwrite existing values
    assert api_server.task_repo is existing_task_repo


def test_apply_to_api_server_is_idempotent() -> None:
    """apply_to_api_server called twice should leave the same repo references."""
    from c_lord.database.lounge_repo import LoungeRepository
    from c_lord.database.repository import SessionRepository
    from c_lord.database.task_repo import TaskRepository

    session_repo = MagicMock(spec=SessionRepository)
    task_repo = MagicMock(spec=TaskRepository)
    lounge_repo = MagicMock(spec=LoungeRepository)

    components = BridgeComponents(
        session_repo=session_repo,
        task_repo=task_repo,
        lounge_repo=lounge_repo,
    )
    api_server = _make_api_server()

    components.apply_to_api_server(api_server)
    components.apply_to_api_server(api_server)

    assert api_server.task_repo is task_repo
    assert api_server.lounge_repo is lounge_repo


@pytest.mark.asyncio
async def test_setup_bridge_auto_wires_api_server(tmp_path: object) -> None:
    """setup_bridge(api_server=...) should auto-wire repos."""
    bot = _make_bot()
    runner = _make_runner()
    api_server = _make_api_server()

    result = await setup_bridge(
        bot,
        runner,
        api_server=api_server,
        session_db_path=str(tmp_path / "sessions.db"),  # type: ignore[operator]
        enable_scheduler=True,
        task_db_path=str(tmp_path / "tasks.db"),  # type: ignore[operator]
    )

    # Repos should be wired automatically
    assert api_server.task_repo is result.task_repo
    assert api_server.lounge_repo is result.lounge_repo


@pytest.mark.asyncio
async def test_setup_bridge_does_not_set_runner_api_port(tmp_path: object) -> None:
    """setup_bridge no longer sets runner.api_port (ClaudeConfig doesn't have it)."""
    bot = _make_bot()
    runner = _make_runner()
    runner.api_port = 9999  # Pre-existing value
    api_server = _make_api_server()
    api_server.port = 8099

    await setup_bridge(
        bot,
        runner,
        api_server=api_server,
        session_db_path=str(tmp_path / "sessions.db"),  # type: ignore[operator]
        enable_scheduler=False,
    )

    # setup_bridge no longer touches runner.api_port — it stays as-is.
    assert runner.api_port == 9999


# ---------------------------------------------------------------------------
# Global manager generation removed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_global_session_dir_manager(tmp_path: object) -> None:
    """setup_bridge should NOT set bot.session_dir_manager even with env vars."""
    bot = _make_bot()
    runner = _make_runner()

    # Track attribute assignments by wrapping __setattr__
    assigned_attrs: list[str] = []
    original_setattr = type(bot).__setattr__

    def tracking_setattr(self: object, name: str, value: object) -> None:
        assigned_attrs.append(name)
        original_setattr(self, name, value)

    type(bot).__setattr__ = tracking_setattr  # type: ignore[assignment]
    try:
        await setup_bridge(
            bot,
            runner,
            session_db_path=str(tmp_path / "sessions.db"),  # type: ignore[operator]
            enable_scheduler=False,
            session_dir_base="/tmp/test-base",
            session_source_repo="https://example.com/repo.git",
        )
    finally:
        type(bot).__setattr__ = original_setattr  # type: ignore[assignment]

    assert "session_dir_manager" not in assigned_attrs


@pytest.mark.asyncio
async def test_no_global_tmux_manager(tmp_path: object) -> None:
    """setup_bridge should NOT set bot.tmux_manager."""
    bot = _make_bot()
    runner = _make_runner()

    assigned_attrs: list[str] = []
    original_setattr = type(bot).__setattr__

    def tracking_setattr(self: object, name: str, value: object) -> None:
        assigned_attrs.append(name)
        original_setattr(self, name, value)

    type(bot).__setattr__ = tracking_setattr  # type: ignore[assignment]
    try:
        await setup_bridge(
            bot,
            runner,
            session_db_path=str(tmp_path / "sessions.db"),  # type: ignore[operator]
            enable_scheduler=False,
            enable_tmux=True,
        )
    finally:
        type(bot).__setattr__ = original_setattr  # type: ignore[assignment]

    assert "tmux_manager" not in assigned_attrs
