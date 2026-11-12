# <<REDACTED 11 lines>>
from c_lord.cogs.skill_command import (
    # <<REDACTED 5 lines>>
from c_lord.discord_ui.authorization import Authorizer
# <<REDACTED 13 lines>>
    # No settings repo → thread auto-archive resolver uses the 3-day default.
    bot.settings_repo = None
    # <<REDACTED 4 lines>>
    runner.model = "sonnet"
    runner.working_dir = None
    runner.timeout_seconds = 300
# <<REDACTED 12 lines>>
        # #713: the shipped default is owner-only, resolved from Discord at
        # on_ready — which a unit test never reaches. Tests that pass an
        # allowlist keep it; the rest are not about who may run /skill.
        authorizer=None if allowed_user_ids else Authorizer(allow_anyone=True),
    # <<REDACTED 207 lines>>
        with patch("c_lord.cogs.skill_command._load_skills") as mock_load:
            # <<REDACTED 7 lines>>
        with patch("c_lord.cogs.skill_command._load_skills", return_value=new_skills) as mock_load:
            # <<REDACTED 10 lines>>
        with patch("c_lord.cogs.skill_command._load_skills", return_value=new_skills):
            # <<REDACTED 42 lines>>
    def _setup_bound_channel(self, cog: SkillCommandCog) -> None:
        """Set up ChannelRepoCog mock so the channel appears bound."""
        cc = _make_channel_cog_mock(session_dir_manager=MagicMock(), tmux_manager=MagicMock())
        cog.bot.get_cog = MagicMock(return_value=cc)

    # <<REDACTED 3 lines>>
        self._setup_bound_channel(cog)
        # <<REDACTED 9 lines>>
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 16 lines>>
        self._setup_bound_channel(cog)
        # <<REDACTED 8 lines>>
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 8 lines>>
        self._setup_bound_channel(cog)
        # <<REDACTED 7 lines>>
        with patch("c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock):
            # <<REDACTED 3 lines>>
        # Defaults to 3 days (4320 min) when no setting is configured.
        assert call_kwargs["auto_archive_duration"] == 4320
# <<REDACTED 18 lines>>
    def _setup_in_thread_cog(self, cog: SkillCommandCog) -> None:
        """Mock tmux/session_dir resolution for in-thread mode."""
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
        cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())

    # <<REDACTED 4 lines>>
        self._setup_in_thread_cog(cog)
        # <<REDACTED 9 lines>>
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 11 lines>>
        self._setup_in_thread_cog(cog)
        # <<REDACTED 5 lines>>
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 5 lines>>
    @pytest.mark.asyncio
    async def test_other_clords_thread_is_not_taken_over(self) -> None:
        """#811: /skill picked from the wrong bot names the owner and runs nothing."""
        other = 1517328788164182116
        # <<REDACTED 1 lines>>
        self._setup_in_thread_cog(cog)
        cog.bot.user.id = 1475105094071750818
        cog.bot.get_user = MagicMock(return_value=MagicMock(bot=True))
        # <<REDACTED 1 lines>>
        thread.owner_id = other
        # <<REDACTED 4 lines>>
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 2 lines>>
            mock_run.assert_not_called()
        sent = [str(c) for c in interaction.followup.send.call_args_list]
        sent += [str(c) for c in interaction.response.send_message.call_args_list]
        assert any(f"<@{other}>" in s for s in sent), sent

    # <<REDACTED 4 lines>>
        # Set up ChannelRepoCog so channel appears bound
        cc = _make_channel_cog_mock(session_dir_manager=MagicMock(), tmux_manager=MagicMock())
        cog.bot.get_cog = MagicMock(return_value=cc)
        # <<REDACTED 10 lines>>
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 11 lines>>
        self._setup_in_thread_cog(cog)
        # <<REDACTED 5 lines>>
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 26 lines>>


# ---------------------------------------------------------------------------
# Per-channel resolution
# ---------------------------------------------------------------------------


def _make_channel_cog_mock(
    *,
    session_dir_manager: object = None,
    tmux_manager: object = None,
) -> MagicMock:
    """Return a mock ChannelRepoCog with configurable resolve methods."""
    from c_lord.cogs.channel_repo import ChannelRepoCog

    channel_cog = MagicMock(spec=ChannelRepoCog)
    channel_cog.resolve_manager = AsyncMock(return_value=session_dir_manager)
    channel_cog.resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
    return channel_cog


class TestPerChannelResolution:
    """SkillCommandCog should resolve managers per-channel via ChannelRepoCog."""

    @pytest.mark.asyncio
    async def test_in_thread_resolves_per_channel(self) -> None:
        """In-thread mode uses ChannelRepoCog to resolve managers."""
        sdm = MagicMock()
        tmux = MagicMock()
        cc = _make_channel_cog_mock(session_dir_manager=sdm, tmux_manager=tmux)

        # <<REDACTED 1 lines>>
        cog.bot.get_cog = MagicMock(return_value=cc)
        # <<REDACTED 5 lines>>
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 2 lines>>
            config = mock_run.call_args[0][0]
            assert config.session_dir_manager is sdm
            assert config.tmux_manager is tmux

    @pytest.mark.asyncio
    async def test_new_thread_resolves_per_channel(self) -> None:
        """New-thread mode uses ChannelRepoCog to resolve managers."""
        sdm = MagicMock()
        tmux = MagicMock()
        cc = _make_channel_cog_mock(session_dir_manager=sdm, tmux_manager=tmux)

        # <<REDACTED 1 lines>>
        cog.bot.get_cog = MagicMock(return_value=cc)
# <<REDACTED 2 lines>>
        mock_channel.id = 999
        # <<REDACTED 4 lines>>
        interaction = _make_interaction()  # non-thread channel
# <<REDACTED 2 lines>>
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 2 lines>>
            config = mock_run.call_args[0][0]
            assert config.session_dir_manager is sdm
            assert config.tmux_manager is tmux

    @pytest.mark.asyncio
    async def test_unbound_channel_returns_error(self) -> None:
        """Unbound channel (no /clord-init) returns an error."""
        # ChannelRepoCog returns None for both managers
        cc = _make_channel_cog_mock()
        # <<REDACTED 1 lines>>
        cog.bot.get_cog = MagicMock(return_value=cc)
# <<REDACTED 2 lines>>
        mock_channel.id = 777  # unbound
        # <<REDACTED 2 lines>>
        interaction = _make_interaction()  # non-thread channel

        # <<REDACTED 1 lines>>

        # Should have sent error mentioning /clord-init
        # <<REDACTED 1 lines>>
        msg = interaction.followup.send.call_args.args[0]
        assert "/clord-init" in msg


# ---------------------------------------------------------------------------
# !skill text/mention twin (E2E-testable via webhook)
# ---------------------------------------------------------------------------


def _make_ctx(channel: MagicMock | None = None, author_id: int = 1) -> MagicMock:
    """Return a mocked commands.Context for the !skill text twin.

    ``ctx.message`` is a *human* message: since #508 the text twin authorizes on
    the message (webhook / trusted bot / allowlisted human), so leaving it an
    unconfigured mock would make every author look like a webhook.
    """
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.id = author_id
    ctx.message = MagicMock(spec=discord.Message)
    ctx.message.webhook_id = None
    ctx.message.author = ctx.author
    ctx.message.author.bot = False
    if channel is not None:
        ctx.channel = channel
    else:
        ctx.channel = MagicMock(spec=discord.TextChannel)
    return ctx


class TestSkillTextCommand:
    """The !skill text command mirrors /skill but is invokable from webhooks."""

    @pytest.mark.asyncio
    async def test_missing_name_shows_usage(self) -> None:
        cog = _make_cog(skills=[])
        ctx = _make_ctx()
        await cog.run_skill_text.callback(cog, ctx, name=None, args=None)
        ctx.send.assert_called_once()
        assert "Usage" in ctx.send.call_args.args[0]

    # <<REDACTED 3 lines>>
        ctx = _make_ctx(author_id=99)
        await cog.run_skill_text.callback(cog, ctx, name="test", args=None)
        ctx.send.assert_called_once()
        assert "permission" in ctx.send.call_args.args[0].lower()
# <<REDACTED 4 lines>>
        ctx = _make_ctx()
        await cog.run_skill_text.callback(cog, ctx, name="bad;name", args=None)
        assert "Invalid" in ctx.send.call_args.args[0]
# <<REDACTED 4 lines>>
        ctx = _make_ctx()
        await cog.run_skill_text.callback(cog, ctx, name="missing", args=None)
        assert "not found" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_in_thread_runs_with_prompt_and_args(self) -> None:
        # <<REDACTED 1 lines>>
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
        cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())
        cog._make_runner = MagicMock(return_value=MagicMock())
        # <<REDACTED 1 lines>>
        ctx = _make_ctx(channel=thread)
        # <<REDACTED 3 lines>>
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 1 lines>>
            await cog.run_skill_text.callback(cog, ctx, name="recall", args="filter today")
            config = mock_run.call_args[0][0]
            assert config.prompt == "/recall filter today"
            assert config.thread is thread
            assert config.session_id is None
