"""Tests for ClaudeDiscordBot."""
# <<REDACTED 8 lines>>
from discord import app_commands

from c_lord.bot import ClaudeDiscordBot


class TestProcessCommands:
    """process_commands override: allow webhook messages, block normal bot messages.

    Every message here is posted in the bot's *own* channel, so the ownership
    gate (#596) passes and these stay tests of the author check alone. The gate
    itself is covered by ``tests/test_command_gate.py``.
    """

    @pytest.mark.asyncio
    async def test_webhook_message_not_blocked(self) -> None:
        """Webhook messages (author.bot=True, webhook_id set) should be processed."""
        bot = ClaudeDiscordBot(channel_id=123)
        bot.get_context = AsyncMock()
        bot.invoke = AsyncMock()

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.bot = True
        msg.webhook_id = 999  # webhook message
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.id = 123  # this bot's channel

        await bot.process_commands(msg)

        bot.get_context.assert_called_once_with(msg)
        bot.invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_bot_message_blocked(self) -> None:
        """Normal bot messages (author.bot=True, no webhook_id) should be ignored."""
        bot = ClaudeDiscordBot(channel_id=123)
        bot.get_context = AsyncMock()
        bot.invoke = AsyncMock()

        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.bot = True
        msg.webhook_id = None

        await bot.process_commands(msg)

        bot.get_context.assert_not_called()
        bot.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_human_message_processed(self) -> None:
        """Normal human messages should be processed."""
        bot = ClaudeDiscordBot(channel_id=123)
        bot.get_context = AsyncMock()
        bot.invoke = AsyncMock()

        msg = MagicMock(spec=discord.Message)
        # <<REDACTED 2 lines>>
        msg.webhook_id = None
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.id = 123  # this bot's channel

        await bot.process_commands(msg)

        bot.get_context.assert_called_once_with(msg)
        bot.invoke.assert_called_once()


class TestOnAppCommandError:
    """on_app_command_error handler tests."""

    @pytest.mark.asyncio
    async def test_handler_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """on_app_command_error should log the error at ERROR level.

        #444 also makes it reply to the user, so the interaction's response
        methods must be awaitable mocks (see test_error_safety_net.py for the
        reply-behavior assertions).
        """
        bot = ClaudeDiscordBot(channel_id=123)
        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        error = app_commands.AppCommandError("test failure")

        with caplog.at_level(logging.ERROR, logger="c_lord.bot"):
            await bot.on_app_command_error(interaction, error)

        assert any("test failure" in record.message for record in caplog.records)


class TestIdentityFailFast:
    """Issue #323: refuse to run when the logged-in identity differs from the expected one.

    Guards against the env-contamination incident class (#322): a "staging"
    launch that inherited the production token must die at startup instead of
    silently running as a second production bot.
    """

    def test_mismatch_exits_process(self) -> None:
        """expected_bot_user_id set + different login identity -> sys.exit(1)."""
        import sys
        from unittest.mock import PropertyMock, patch

        bot = ClaudeDiscordBot(channel_id=123, expected_bot_user_id=111)
        user = MagicMock()
        user.id = 222

        with (
            patch.object(ClaudeDiscordBot, "user", new_callable=PropertyMock, return_value=user),
            patch.object(sys, "exit") as mock_exit,
        ):
            bot._assert_expected_identity()

        mock_exit.assert_called_once_with(1)

    def test_mismatch_logs_expected_and_actual(self, caplog: pytest.LogCaptureFixture) -> None:
        """The failure log must show BOTH the expected and the actual bot user id."""
        import sys
        from unittest.mock import PropertyMock, patch

        bot = ClaudeDiscordBot(channel_id=123, expected_bot_user_id=111)
        user = MagicMock()
        user.id = 222

        with (
            caplog.at_level(logging.CRITICAL, logger="c_lord.bot"),
            patch.object(ClaudeDiscordBot, "user", new_callable=PropertyMock, return_value=user),
            patch.object(sys, "exit"),
        ):
            bot._assert_expected_identity()

        joined = " ".join(r.message for r in caplog.records)
        assert "111" in joined
        assert "222" in joined

    def test_match_does_not_exit(self) -> None:
        """expected_bot_user_id set + matching identity -> bot keeps running."""
        import sys
        from unittest.mock import PropertyMock, patch

        bot = ClaudeDiscordBot(channel_id=123, expected_bot_user_id=111)
        user = MagicMock()
        user.id = 111

        with (
            patch.object(ClaudeDiscordBot, "user", new_callable=PropertyMock, return_value=user),
            patch.object(sys, "exit") as mock_exit,
        ):
            bot._assert_expected_identity()

        mock_exit.assert_not_called()

    def test_unset_is_backward_compatible(self) -> None:
        """No expected_bot_user_id (default) -> never exits, any identity allowed."""
        import sys
        from unittest.mock import PropertyMock, patch

        bot = ClaudeDiscordBot(channel_id=123)
        user = MagicMock()
        user.id = 222

        with (
            patch.object(ClaudeDiscordBot, "user", new_callable=PropertyMock, return_value=user),
            patch.object(sys, "exit") as mock_exit,
        ):
            bot._assert_expected_identity()

        mock_exit.assert_not_called()


class TestOnError:
    """on_error zombie-detection tests (Issue #91)."""

    @pytest.mark.asyncio
    async def test_session_closed_exits_process(self) -> None:
        """RuntimeError('Session is closed') must trigger sys.exit(1)."""
        import sys
        from unittest.mock import patch

        bot = ClaudeDiscordBot(channel_id=123)

        with patch.object(sys, "exit") as mock_exit:
            try:
                raise RuntimeError("Session is closed")
            except RuntimeError:
                await bot.on_error("on_message")

        mock_exit.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_other_runtime_error_does_not_exit(self) -> None:
        """Non-zombie RuntimeError must NOT call sys.exit."""
        import sys
        from unittest.mock import patch

        bot = ClaudeDiscordBot(channel_id=123)

        with patch.object(sys, "exit") as mock_exit:
            try:
                raise RuntimeError("something else")
            except RuntimeError:
                await bot.on_error("on_message")

        mock_exit.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_closed_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """RuntimeError('Session is closed') must log at ERROR level."""
        import sys
        from unittest.mock import patch

        bot = ClaudeDiscordBot(channel_id=123)

        with caplog.at_level(logging.ERROR, logger="c_lord.bot"), patch.object(sys, "exit"):
            try:
                raise RuntimeError("Session is closed")
            except RuntimeError:
                await bot.on_error("on_message")

        assert any(r.levelno >= logging.ERROR for r in caplog.records)
        assert any("zombie" in r.message.lower() for r in caplog.records)


async def _no_messages():
    for _ in ():
        yield _


class TestDashboardOnReady:
    """#720: on_ready must not add a Session Status board on every reconnect.

    ``on_ready`` fires again on every RESUME/IDENTIFY, not only at startup.
    Rebuilding the dashboard there threw away the live session states and — by
    way of the unconditional ``channel.send`` in ``initialize()`` — left another
    dead board in the channel each time.
    """

    def _make_bot(self) -> tuple:
        bot = ClaudeDiscordBot(channel_id=123)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 123
        channel.guild = MagicMock()
        board = MagicMock(spec=discord.Message)
        board.id = 1
        board.pinned = False
        board.pin = AsyncMock()
        board.edit = AsyncMock()
        channel.send = AsyncMock(return_value=board)
        bot.tree.clear_commands = MagicMock()
        bot.tree.sync = AsyncMock(return_value=[])
        bot.get_channel = MagicMock(return_value=channel)
        return bot, channel

    @pytest.mark.asyncio
    async def test_default_start_posts_no_board(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#761: the board is opt-in — a plain start leaves the channel alone."""
        from unittest.mock import patch

        monkeypatch.delenv("CLORD_SESSION_STATUS_BOARD", raising=False)
        bot, channel = self._make_bot()
        channel.history = MagicMock(side_effect=lambda **_kw: _no_messages())
        channel.pins = MagicMock(side_effect=lambda **_kw: _no_messages())

        with patch.object(bot, "_assert_expected_identity"):
            await bot.on_ready()

        channel.send.assert_not_awaited()
        # The turn-end ping lives on the same object, so it must still exist.
        assert bot.thread_dashboard is not None

    @pytest.mark.asyncio
    async def test_second_on_ready_keeps_the_same_dashboard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import patch

        monkeypatch.setenv("CLORD_SESSION_STATUS_BOARD", "1")
        bot, channel = self._make_bot()

        with patch.object(bot, "_assert_expected_identity"):
            await bot.on_ready()
            first = bot.thread_dashboard
            await bot.on_ready()

        assert bot.thread_dashboard is first
        assert channel.send.await_count == 1


class TestSlashCommandSync:
    """Slash command sync must register GLOBALLY so commands work in every guild.

    #462 regression: #461 registered commands only on the one guild derived from
    channel_id (and wiped globals), so the bot — which serves multiple servers —
    lost its slash commands on every OTHER server. The fix registers globally
    (Discord's multi-guild standard) and clears the legacy guild-scoped commands
    on the primary guild so they don't duplicate against the global ones.
    """

    def _make_bot_with_mocks(self) -> tuple:
        from unittest.mock import AsyncMock, MagicMock

        bot = ClaudeDiscordBot(channel_id=123)
        guild = MagicMock()
        channel = MagicMock()
        channel.guild = guild
        bot.tree.copy_global_to = MagicMock()
        bot.tree.clear_commands = MagicMock()
        bot.tree.sync = AsyncMock(return_value=[])
        bot.get_channel = MagicMock(return_value=channel)
        return bot, guild

    @pytest.mark.asyncio
    async def test_global_sync_called(self) -> None:
        """tree.sync() with NO guild kwarg must be called to register globally."""
        from unittest.mock import call, patch

        bot, guild = self._make_bot_with_mocks()

        with (
            patch.object(bot, "_assert_expected_identity"),
            patch("c_lord.bot.isinstance", return_value=False),
        ):
            await bot.on_ready()

        # A global sync (no guild kwarg) must have happened
        assert call() in bot.tree.sync.call_args_list

    @pytest.mark.asyncio
    async def test_copy_global_to_not_called(self) -> None:
        """copy_global_to must NOT be used — that re-creates the per-guild duplicates."""
        from unittest.mock import patch

        bot, guild = self._make_bot_with_mocks()

        with (
            patch.object(bot, "_assert_expected_identity"),
            patch("c_lord.bot.isinstance", return_value=False),
        ):
            await bot.on_ready()

        bot.tree.copy_global_to.assert_not_called()

    @pytest.mark.asyncio
    async def test_legacy_guild_commands_cleared(self) -> None:
        """clear_commands(guild=guild) + sync(guild=guild) must wipe legacy guild commands."""
        from unittest.mock import patch

        bot, guild = self._make_bot_with_mocks()

        with (
            patch.object(bot, "_assert_expected_identity"),
            patch("c_lord.bot.isinstance", return_value=False),
        ):
            await bot.on_ready()

        bot.tree.clear_commands.assert_called_once_with(guild=guild)
        # A guild-scoped sync (to push the now-empty guild command set) must have happened
        guild_syncs = [c for c in bot.tree.sync.call_args_list if c.kwargs.get("guild") == guild]
        assert len(guild_syncs) == 1

    @pytest.mark.asyncio
    async def test_sync_order_guild_clear_before_global(self) -> None:
        """Legacy guild clear must happen before the global registration."""
        from unittest.mock import patch

        bot, guild = self._make_bot_with_mocks()
        call_order: list[str] = []

        async def tracking_sync(**kwargs: object) -> list:
            if kwargs.get("guild") is not None:
                call_order.append("guild_clear")
            else:
                call_order.append("global")
            return []

        bot.tree.sync.side_effect = tracking_sync

        with (
            patch.object(bot, "_assert_expected_identity"),
            patch("c_lord.bot.isinstance", return_value=False),
        ):
            await bot.on_ready()

        assert call_order == ["guild_clear", "global"]

    @pytest.mark.asyncio
    async def test_global_sync_runs_even_without_guild(self) -> None:
        """If the channel's guild can't be resolved, global sync must still run."""
        from unittest.mock import AsyncMock, MagicMock, call, patch

        bot = ClaudeDiscordBot(channel_id=123)
        channel = MagicMock()
        channel.guild = None  # cannot resolve guild
        bot.tree.clear_commands = MagicMock()
        bot.tree.sync = AsyncMock(return_value=[])
        bot.get_channel = MagicMock(return_value=channel)

        with (
            patch.object(bot, "_assert_expected_identity"),
            patch("c_lord.bot.isinstance", return_value=False),
        ):
            await bot.on_ready()

        # Global sync still happened so commands are registered everywhere
        assert call() in bot.tree.sync.call_args_list


class TestDroppedBotCommandLog:
    """#1140: a non-webhook bot's ``!command`` is still dropped (#740), but logged."""

    @staticmethod
    def _bot() -> ClaudeDiscordBot:
        from discord.ext import commands

        bot = ClaudeDiscordBot(channel_id=123)

        @commands.command(name="clord")
        async def _clord(ctx: commands.Context) -> None:  # pragma: no cover - never run
            raise AssertionError("must not run")

        bot.add_command(_clord)
        bot.get_context = AsyncMock()
        bot.invoke = AsyncMock()
        return bot

    @staticmethod
    def _msg(content: str, *, author_id: int = 111, channel_id: int = 123) -> MagicMock:
        msg = MagicMock(spec=discord.Message)
        msg.author = MagicMock()
        msg.author.bot = True
        msg.author.id = author_id
        msg.author.name = "opu"
        msg.webhook_id = None
        msg.content = content
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.id = channel_id
        return msg

    @staticmethod
    def _dropped(caplog: pytest.LogCaptureFixture) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "c_lord.bot" and r.levelno == logging.INFO and "#1140" in r.getMessage()
        ]

    @pytest.mark.asyncio
    async def test_bot_clord_is_dropped_with_an_info_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bot = self._bot()
        caplog.set_level(logging.INFO, logger="c_lord.bot")

        await bot.process_commands(self._msg("!clord ok とだけ返して"))

        bot.get_context.assert_not_called()
        bot.invoke.assert_not_called()  # #740: still not run
        lines = self._dropped(caplog)
        assert len(lines) == 1
        assert "!clord" in lines[0] and "id=111" in lines[0] and "channel=123" in lines[0]
        assert "webhook" in lines[0]

    @pytest.mark.asyncio
    async def test_chatter_and_unknown_commands_are_not_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bot = self._bot()
        caplog.set_level(logging.INFO, logger="c_lord.bot")

        for text in ("おはようございます", "!nosuchcommand x", "!", "clord ok", ""):
            await bot.process_commands(self._msg(text))

        assert self._dropped(caplog) == []

    @pytest.mark.asyncio
    async def test_own_messages_are_not_logged(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bot = self._bot()
        me = MagicMock()
        me.id = 555
        monkeypatch.setattr(type(bot), "user", property(lambda _self: me))
        caplog.set_level(logging.INFO, logger="c_lord.bot")

        await bot.process_commands(self._msg("!clord ok", author_id=555))

        assert self._dropped(caplog) == []

    @pytest.mark.asyncio
    async def test_repeats_in_one_channel_are_sampled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bot = self._bot()
        caplog.set_level(logging.INFO, logger="c_lord.bot")

        for _ in range(5):
            await bot.process_commands(self._msg("!clord again"))
        await bot.process_commands(self._msg("!clord other channel", channel_id=456))

        lines = self._dropped(caplog)
        assert len(lines) == 2  # one for 123, one for 456
        assert "channel=456" in lines[1]
