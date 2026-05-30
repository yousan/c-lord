"""Tests for WebhookTriggerCog."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from c_lord.claude.config import ClaudeConfig
from c_lord.cogs.webhook_trigger import WebhookTrigger, WebhookTriggerCog

_PATCH_RUN = "c_lord.cogs.webhook_trigger.run_claude_with_config"
_PATCH_TMUX_RUNNER = "c_lord.claude.tmux_runner.TmuxClaudeRunner"


@pytest.fixture
def bot() -> MagicMock:
    return MagicMock(spec=commands.Bot)


@pytest.fixture
def runner() -> ClaudeConfig:
    return ClaudeConfig(
        command="claude",
        model="sonnet",
        permission_mode="acceptEdits",
        working_dir="/home/user",
        timeout_seconds=300,
        dangerously_skip_permissions=False,
    )


@pytest.fixture
def triggers() -> dict[str, WebhookTrigger]:
    return {
        "🔄 docs-sync": WebhookTrigger(
            prompt="Sync docs",
            working_dir="/home/user/project",
            timeout=600,
        ),
        "🔄 deploy": WebhookTrigger(
            prompt="Deploy to staging",
        ),
    }


@pytest.fixture
def tmux_manager() -> MagicMock:
    """Mock TmuxSessionManager returned by _resolve_tmux_manager."""
    return MagicMock()


@pytest.fixture
def cog(
    bot: MagicMock,
    runner: ClaudeConfig,
    triggers: dict[str, WebhookTrigger],
    tmux_manager: MagicMock,
) -> WebhookTriggerCog:
    c = WebhookTriggerCog(
        bot=bot,
        runner=runner,
        triggers=triggers,
    )
    c._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
    return c


def _make_message(
    content: str = "🔄 docs-sync",
    webhook_id: int | None = 12345,
    channel_id: int = 999,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.webhook_id = webhook_id
    msg.channel = MagicMock()
    msg.channel.id = channel_id
    msg.reply = AsyncMock()
    msg.add_reaction = AsyncMock()
    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock()
    msg.create_thread = AsyncMock(return_value=thread)
    return msg


class TestWebhookFiltering:
    """Test message filtering logic."""

    @pytest.mark.asyncio
    async def test_ignores_non_webhook_messages(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """Messages without webhook_id should be ignored."""
        msg = _make_message(webhook_id=None)
        with patch(_PATCH_RUN) as mock_run:
            await cog.on_message(msg)
            mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_unauthorized_webhook(
        self,
        bot: MagicMock,
        runner: ClaudeConfig,
        triggers: dict[str, WebhookTrigger],
        tmux_manager: MagicMock,
    ) -> None:
        """Unauthorized webhooks are ignored."""
        cog = WebhookTriggerCog(
            bot=bot,
            runner=runner,
            triggers=triggers,
            allowed_webhook_ids={99999},
        )
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
        msg = _make_message(webhook_id=12345)
        with patch(_PATCH_RUN) as mock_run:
            await cog.on_message(msg)
            mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_authorized_webhook(
        self,
        bot: MagicMock,
        runner: ClaudeConfig,
        triggers: dict[str, WebhookTrigger],
        tmux_manager: MagicMock,
    ) -> None:
        """Authorized webhooks are processed."""
        cog = WebhookTriggerCog(
            bot=bot,
            runner=runner,
            triggers=triggers,
            allowed_webhook_ids={12345},
        )
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
        msg = _make_message(webhook_id=12345)
        with patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "session-123"
            await cog.on_message(msg)
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_wrong_channel(
        self,
        bot: MagicMock,
        runner: ClaudeConfig,
        triggers: dict[str, WebhookTrigger],
        tmux_manager: MagicMock,
    ) -> None:
        """Messages in other channels are ignored."""
        cog = WebhookTriggerCog(
            bot=bot,
            runner=runner,
            triggers=triggers,
            channel_ids={111},
        )
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
        msg = _make_message(channel_id=999)
        with patch(_PATCH_RUN) as mock_run:
            await cog.on_message(msg)
            mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_unmatched_prefix(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """Unmatched prefixes are ignored."""
        msg = _make_message(content="Hello world")
        with patch(_PATCH_RUN) as mock_run:
            await cog.on_message(msg)
            mock_run.assert_not_called()


class TestTriggerExecution:
    """Test trigger execution logic."""

    @pytest.mark.asyncio
    async def test_matching_prefix_triggers_run(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """A matching prefix should trigger Claude Code execution."""
        msg = _make_message(content="🔄 docs-sync")
        with patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "session-abc"
            await cog.on_message(msg)

            mock_run.assert_called_once()
            config = mock_run.call_args[0][0]  # RunConfig object
            assert config.prompt == "Sync docs"
            assert config.repo is None
            assert config.session_id is None

    @pytest.mark.asyncio
    async def test_creates_thread(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """Trigger execution should create a Discord thread."""
        msg = _make_message(content="🔄 docs-sync")
        with patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "session-abc"
            await cog.on_message(msg)
            msg.create_thread.assert_called_once()
            # Defaults to 3 days (4320 min) when no setting is configured.
            assert msg.create_thread.call_args.kwargs["auto_archive_duration"] == 4320

    @pytest.mark.asyncio
    async def test_success_reaction(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """Successful execution should add check reaction."""
        msg = _make_message(content="🔄 docs-sync")
        with patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "session-abc"
            await cog.on_message(msg)
            msg.add_reaction.assert_called_with("✅")

    @pytest.mark.asyncio
    async def test_failure_reaction(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """Failed execution should add error reaction."""
        msg = _make_message(content="🔄 docs-sync")
        with patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run:
            mock_run.return_value = None
            await cog.on_message(msg)
            msg.add_reaction.assert_called_with("❌")

    @pytest.mark.asyncio
    async def test_tmux_runner_created_with_trigger_config(
        self,
        cog: WebhookTriggerCog,
        tmux_manager: MagicMock,
    ) -> None:
        """TmuxClaudeRunner should be created with trigger-specific config."""
        msg = _make_message(content="🔄 docs-sync")
        with (
            patch(_PATCH_TMUX_RUNNER) as mock_tmux_cls,
            patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = "session-abc"
            await cog.on_message(msg)

            mock_tmux_cls.assert_called_once()
            call_kwargs = mock_tmux_cls.call_args[1]
            assert call_kwargs["tmux_manager"] is tmux_manager
            assert call_kwargs["model"] == "sonnet"
            assert call_kwargs["working_dir"] == "/home/user/project"
            assert call_kwargs["timeout_seconds"] == 600
            assert call_kwargs["dangerously_skip_permissions"] is True

    @pytest.mark.asyncio
    async def test_tmux_runner_falls_back_to_config_working_dir(
        self,
        bot: MagicMock,
        runner: ClaudeConfig,
        tmux_manager: MagicMock,
    ) -> None:
        """When trigger has no working_dir, fall back to ClaudeConfig.working_dir."""
        triggers = {
            "🔄 deploy": WebhookTrigger(
                prompt="Deploy to staging",
            ),
        }
        cog = WebhookTriggerCog(bot=bot, runner=runner, triggers=triggers)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)

        msg = _make_message(content="🔄 deploy")
        with (
            patch(_PATCH_TMUX_RUNNER) as mock_tmux_cls,
            patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = "session-abc"
            await cog.on_message(msg)

            call_kwargs = mock_tmux_cls.call_args[1]
            assert call_kwargs["working_dir"] == "/home/user"

    @pytest.mark.asyncio
    async def test_tmux_not_configured_sends_warning(
        self,
        bot: MagicMock,
        runner: ClaudeConfig,
        triggers: dict[str, WebhookTrigger],
    ) -> None:
        """When _resolve_tmux_manager returns None, a warning is sent to the thread."""
        cog = WebhookTriggerCog(bot=bot, runner=runner, triggers=triggers)
        cog._resolve_tmux_manager = AsyncMock(return_value=None)

        msg = _make_message(content="🔄 docs-sync")
        with patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run:
            await cog.on_message(msg)
            mock_run.assert_not_called()

        thread = msg.create_thread.return_value
        thread.send.assert_called_once()
        assert "tmux is not configured" in thread.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_prefix_starts_with_matching(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """Prefix matching works with startswith."""
        msg = _make_message(content="🔄 docs-sync extra info")
        with patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "session-abc"
            await cog.on_message(msg)
            mock_run.assert_called_once()


class TestConcurrency:
    """Test concurrent execution prevention."""

    @pytest.mark.asyncio
    async def test_concurrent_same_prefix_blocked(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """Concurrent runs of same prefix should be blocked."""
        lock = cog._locks["🔄 docs-sync"]
        msg = _make_message(content="🔄 docs-sync")

        await lock.acquire()
        try:
            with patch(_PATCH_RUN) as mock_run:
                await cog.on_message(msg)
                mock_run.assert_not_called()
                msg.reply.assert_called_once()
                assert "already running" in msg.reply.call_args[0][0]
        finally:
            lock.release()

    @pytest.mark.asyncio
    async def test_different_prefix_not_blocked(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """Different prefixes should have independent locks."""
        lock = cog._locks["🔄 docs-sync"]
        msg = _make_message(content="🔄 deploy")

        await lock.acquire()
        try:
            with patch(
                _PATCH_RUN,
                new_callable=AsyncMock,
            ) as mock_run:
                mock_run.return_value = "session-abc"
                await cog.on_message(msg)
                mock_run.assert_called_once()
        finally:
            lock.release()


class TestActiveCount:
    """Test active_count tracking for drain coordination."""

    def test_initial_count_is_zero(self, cog: WebhookTriggerCog) -> None:
        assert cog.active_count == 0

    @pytest.mark.asyncio
    async def test_count_increments_during_execution(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """active_count should be 1 during execution and 0 after."""
        msg = _make_message(content="🔄 docs-sync")
        observed_counts: list[int] = []

        async def fake_run(config: object) -> str:  # receives RunConfig
            observed_counts.append(cog.active_count)
            return "session-abc"

        with patch(_PATCH_RUN, side_effect=fake_run):
            await cog.on_message(msg)

        assert observed_counts == [1]
        assert cog.active_count == 0

    @pytest.mark.asyncio
    async def test_count_decrements_on_failure(
        self,
        cog: WebhookTriggerCog,
    ) -> None:
        """active_count should decrement even if run_claude_with_config raises."""
        msg = _make_message(content="🔄 docs-sync")

        with (
            patch(_PATCH_RUN, side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await cog._execute_trigger(
                msg,
                "🔄 docs-sync",
                cog.triggers["🔄 docs-sync"],
            )

        assert cog.active_count == 0


class TestWebhookTriggerDataclass:
    """Test WebhookTrigger dataclass."""

    def test_defaults(self) -> None:
        trigger = WebhookTrigger(prompt="test")
        assert trigger.prompt == "test"
        assert trigger.working_dir is None
        assert trigger.timeout == 300
        assert trigger.allowed_tools is None
        assert trigger.dangerously_skip_permissions is True

    def test_frozen(self) -> None:
        trigger = WebhookTrigger(prompt="test")
        with pytest.raises(AttributeError):
            trigger.prompt = "changed"  # type: ignore[misc]
