# <<REDACTED 10 lines>>
from c_lord.claude.config import ClaudeConfig
from c_lord.cogs.webhook_trigger import WebhookTrigger, WebhookTriggerCog
# <<REDACTED 1 lines>>
_PATCH_RUN = "c_lord.cogs.webhook_trigger.run_claude_with_config"
_PATCH_TMUX_RUNNER = "c_lord.claude.tmux_runner.TmuxClaudeRunner"
# <<REDACTED 8 lines>>
def runner() -> ClaudeConfig:
    return ClaudeConfig(
        command="claude",
        model="sonnet",
        permission_mode="acceptEdits",
        working_dir="/home/user",
        timeout_seconds=300,
        dangerously_skip_permissions=False,
    )
# <<REDACTED 16 lines>>
@pytest.fixture
def tmux_manager() -> MagicMock:
    """Mock TmuxSessionManager returned by _resolve_tmux_manager."""
    return MagicMock()


# <<REDACTED 3 lines>>
    runner: ClaudeConfig,
    # <<REDACTED 1 lines>>
    tmux_manager: MagicMock,
# <<REDACTED 1 lines>>
    c = WebhookTriggerCog(
        # <<REDACTED 4 lines>>
    c._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
    return c
# <<REDACTED 38 lines>>
        runner: ClaudeConfig,
        # <<REDACTED 1 lines>>
        tmux_manager: MagicMock,
    # <<REDACTED 8 lines>>
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
        # <<REDACTED 9 lines>>
        runner: ClaudeConfig,
        # <<REDACTED 1 lines>>
        tmux_manager: MagicMock,
    # <<REDACTED 8 lines>>
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
        # <<REDACTED 10 lines>>
        runner: ClaudeConfig,
        # <<REDACTED 1 lines>>
        tmux_manager: MagicMock,
    # <<REDACTED 8 lines>>
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
        # <<REDACTED 48 lines>>
            # Defaults to 3 days (4320 min) when no setting is configured.
            assert msg.create_thread.call_args.kwargs["auto_archive_duration"] == 4320
# <<REDACTED 26 lines>>
    async def test_tmux_runner_created_with_trigger_config(
        # <<REDACTED 2 lines>>
        tmux_manager: MagicMock,
    # <<REDACTED 1 lines>>
        """TmuxClaudeRunner should be created with trigger-specific config."""
        # <<REDACTED 1 lines>>
        with (
            patch(_PATCH_TMUX_RUNNER) as mock_tmux_cls,
            patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run,
        ):
            # <<REDACTED 3 lines>>
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

        # <<REDACTED 1 lines>>
        with (
            patch(_PATCH_TMUX_RUNNER) as mock_tmux_cls,
            patch(_PATCH_RUN, new_callable=AsyncMock) as mock_run,
        ):
            # <<REDACTED 2 lines>>

            call_kwargs = mock_tmux_cls.call_args[1]
            assert call_kwargs["working_dir"] == "/home/user"

    @pytest.mark.asyncio
    async def test_tmux_not_configured_sends_warning(
        self,
        bot: MagicMock,
        runner: ClaudeConfig,
        # <<REDACTED 2 lines>>
        """When _resolve_tmux_manager returns None, a warning is sent to the thread."""
        cog = WebhookTriggerCog(bot=bot, runner=runner, triggers=triggers)
        cog._resolve_tmux_manager = AsyncMock(return_value=None)

        # <<REDACTED 4 lines>>

        thread = msg.create_thread.return_value
        thread.send.assert_called_once()
        assert "tmux is not configured" in thread.send.call_args[0][0]
# <<REDACTED 88 lines>>
        """active_count should decrement even if run_claude_with_config raises."""
        # <<REDACTED 30 lines>>


class TestClaimsMessage:
    """#862: ClaudeChatCog asks this before starting a conversation from a webhook."""

    def _cog(self, **kwargs):
        from c_lord.cogs.webhook_trigger import WebhookTrigger, WebhookTriggerCog
# <<REDACTED 1 lines>>
        trigger = WebhookTrigger(prompt="sync docs")
        return WebhookTriggerCog(
            bot=MagicMock(), runner=MagicMock(), triggers={"🔄 docs-sync": trigger}, **kwargs
        )

    def _msg(self, content: str, *, webhook_id: int | None = 1, channel_id: int = 5):
        message = MagicMock()
        message.webhook_id = webhook_id
        message.content = content
        message.channel.id = channel_id
        return message

    def test_a_matching_webhook_message_is_claimed(self) -> None:
        assert self._cog().claims_message(self._msg("🔄 docs-sync run 42"))

    def test_other_text_is_not_claimed(self) -> None:
        assert not self._cog().claims_message(self._msg("サーバを再起動して"))

    def test_a_human_message_is_not_claimed(self) -> None:
        assert not self._cog().claims_message(self._msg("🔄 docs-sync", webhook_id=None))

    def test_a_webhook_outside_the_allowlist_is_not_claimed(self) -> None:
        cog = self._cog(allowed_webhook_ids={2})
        assert not cog.claims_message(self._msg("🔄 docs-sync"))
