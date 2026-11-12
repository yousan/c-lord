"""ClaudeChatCog: the commands, the message path, and what survives a restart."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.concurrency import SessionRegistry
from c_lord.coordination.service import CoordinationService
from c_lord.discord_ui.authorization import Authorizer


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


def _make_cog(*, channel_cog: MagicMock | None = None) -> ClaudeChatCog:
    """A cog on a mocked bot whose own channel is 999 and whose gate is open."""
    bot = MagicMock()
    bot.channel_id = 999
    # No settings repo → thread auto-archive resolver uses the 3-day default.
    bot.settings_repo = None
    # Default get_context returns a non-command context (valid=False)
    _default_ctx = MagicMock()
    _default_ctx.valid = False
    bot.get_context = AsyncMock(return_value=_default_ctx)
    # By default, ChannelRepoCog returns None (no binding).
    # Tests that need a binding should pass a channel_cog mock.
    bot.get_cog = MagicMock(return_value=channel_cog)
    repo = MagicMock()
    # No session row unless a test puts one there.
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    runner = MagicMock()
    # #713: this file is not about *who* may drive c-lord. The shipped
    # default is owner-only, resolved from Discord at on_ready — which a
    # unit test never reaches — so the gate is opened explicitly here and
    # the rule itself is pinned in tests/test_default_authorization.py.
    return ClaudeChatCog(
        bot=bot, repo=repo, runner=runner, authorizer=Authorizer(allow_anyone=True)
    )


def _slash_in(channel: MagicMock) -> MagicMock:
    """A slash interaction issued in *channel*, with every reply path awaitable."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = channel
    interaction.user = MagicMock()
    interaction.user.id = 1
    interaction.extras = {}
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_thread_interaction(thread_id: int = 12345) -> MagicMock:
    """A slash interaction issued inside the thread *thread_id*."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    return _slash_in(thread)


def _make_channel_interaction() -> MagicMock:
    """A slash interaction issued in a plain text channel — not a thread."""
    return _slash_in(MagicMock(spec=discord.TextChannel))


def _first_reply(interaction: MagicMock):
    """The one ``interaction.response.send_message`` call a command made."""
    interaction.response.send_message.assert_awaited_once()
    return interaction.response.send_message.call_args


class TestStopCommand:
    """/stop interrupts the running turn and leaves everything needed to resume."""

    @staticmethod
    def _running_turn(cog: ClaudeChatCog, thread_id: int = 12345) -> MagicMock:
        """Register a runner for *thread_id*, as a turn in flight would have."""
        runner = MagicMock()
        runner.interrupt = AsyncMock()
        cog._active_runners[thread_id] = runner
        return runner

    @pytest.mark.asyncio
    async def test_outside_a_thread_it_is_refused_privately(self) -> None:
        cog = _make_cog()
        interaction = _make_channel_interaction()

        await cog.stop_session.callback(cog, interaction)

        reply = _first_reply(interaction)
        assert reply.kwargs.get("ephemeral") is True
        assert "thread" in reply.args[0].lower()

    @pytest.mark.asyncio
    async def test_with_nothing_running_it_says_so_privately(self) -> None:
        cog = _make_cog()
        interaction = _make_thread_interaction(12345)

        await cog.stop_session.callback(cog, interaction)

        reply = _first_reply(interaction)
        assert reply.kwargs.get("ephemeral") is True
        assert reply.kwargs.get("embed") is None

    @pytest.mark.asyncio
    async def test_the_running_turn_is_interrupted(self) -> None:
        cog = _make_cog()
        runner = self._running_turn(cog)

        await cog.stop_session.callback(cog, _make_thread_interaction(12345))

        runner.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_another_threads_turn_is_left_running(self) -> None:
        cog = _make_cog()
        elsewhere = self._running_turn(cog, thread_id=777)

        await cog.stop_session.callback(cog, _make_thread_interaction(12345))

        elsewhere.interrupt.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_session_row_is_not_touched(self) -> None:
        """What separates /stop from /clear: the next message resumes the session."""
        cog = _make_cog()
        cog.repo.delete = AsyncMock()
        cog.repo.reset = AsyncMock()
        self._running_turn(cog)

        await cog.stop_session.callback(cog, _make_thread_interaction(12345))

        cog.repo.delete.assert_not_called()
        cog.repo.reset.assert_not_called()
        cog.repo.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_runner_stays_registered(self) -> None:
        """Unregistering belongs to the turn's own cleanup, not to /stop."""
        cog = _make_cog()
        runner = self._running_turn(cog)

        await cog.stop_session.callback(cog, _make_thread_interaction(12345))

        assert cog._active_runners[12345] is runner

    @pytest.mark.asyncio
    async def test_a_stop_is_confirmed_with_a_public_embed(self) -> None:
        cog = _make_cog()
        self._running_turn(cog)
        interaction = _make_thread_interaction(12345)

        await cog.stop_session.callback(cog, interaction)

        reply = _first_reply(interaction)
        embed = reply.kwargs.get("embed")
        assert embed is not None and "stopped" in embed.title.lower()
        assert reply.kwargs.get("ephemeral") is False

    @pytest.mark.asyncio
    async def test_the_stop_embed_is_orange(self) -> None:
        """Orange — neither the red of an error nor the green of a finished turn."""
        cog = _make_cog()
        self._running_turn(cog)
        interaction = _make_thread_interaction(12345)

        await cog.stop_session.callback(cog, interaction)

        embed = _first_reply(interaction).kwargs["embed"]
        assert embed.color is not None
        assert embed.color.value == 0xFFA500

    @pytest.mark.asyncio
    async def test_a_stranger_stops_nothing(self) -> None:
        cog = _make_cog()
        cog._authorizer = Authorizer(allowed_user_ids={42})
        runner = self._running_turn(cog)
        interaction = _make_thread_interaction(12345)  # user id 1

        await cog.stop_session.callback(cog, interaction)

        runner.interrupt.assert_not_awaited()
        reply = _first_reply(interaction)
        assert reply.kwargs.get("ephemeral") is True
        assert "not authorized" in reply.args[0].lower()


def _attach_human_message(ctx: MagicMock, webhook_id: int | None = None) -> MagicMock:
    """Give *ctx* a realistic ``ctx.message``.

    Without this, ``ctx.message.webhook_id`` and ``ctx.author.bot`` are
    auto-created (truthy) MagicMocks, so a context meant to represent a *human*
    would be read as a webhook / bot by ``_is_message_authorized`` (#507).
    A real human message has ``webhook_id is None`` and ``author.bot is False``.
    """
    ctx.author.bot = webhook_id is not None
    ctx.message = MagicMock(spec=discord.Message)
    ctx.message.webhook_id = webhook_id
    ctx.message.author = ctx.author
    return ctx


def _make_thread_ctx(
    thread_id: int = 12345, parent_id: int | None = None, webhook_id: int | None = None
) -> MagicMock:
    """Return a mocked commands.Context whose channel is a discord.Thread."""
    ctx = MagicMock()
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    ctx.channel = thread
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.id = 1
    return _attach_human_message(ctx, webhook_id)


def _make_channel_ctx(webhook_id: int | None = None) -> MagicMock:
    """Return a mocked commands.Context whose channel is NOT a thread."""
    ctx = MagicMock()
    ctx.channel = MagicMock(spec=discord.TextChannel)
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.id = 1
    return _attach_human_message(ctx, webhook_id)


class TestStopTextCommand:
    """!stop mirrors /stop but is invokable from webhooks (E2E)."""

    @pytest.mark.asyncio
    async def test_outside_thread(self) -> None:
        cog = _make_cog()
        ctx = _make_channel_ctx()
        await cog.stop_text.callback(cog, ctx)
        ctx.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_active_runner(self) -> None:
        cog = _make_cog()
        ctx = _make_thread_ctx(thread_id=12345)
        await cog.stop_text.callback(cog, ctx)
        ctx.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_runner_interrupt(self) -> None:
        cog = _make_cog()
        ctx = _make_thread_ctx(thread_id=12345)
        mock_runner = MagicMock()
        mock_runner.interrupt = AsyncMock()
        cog._active_runners[12345] = mock_runner
        await cog.stop_text.callback(cog, ctx)
        mock_runner.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_delete_session(self) -> None:
        cog = _make_cog()
        ctx = _make_thread_ctx(thread_id=12345)
        mock_runner = MagicMock()
        mock_runner.interrupt = AsyncMock()
        cog._active_runners[12345] = mock_runner
        await cog.stop_text.callback(cog, ctx)
        cog.repo.delete.assert_not_called()


class TestClearTextCommand:
    """!clear mirrors /clear but is invokable from webhooks (E2E)."""

    @pytest.mark.asyncio
    async def test_outside_thread(self) -> None:
        cog = _make_cog()
        ctx = _make_channel_ctx()
        await cog.clear_text.callback(cog, ctx)
        ctx.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_types_clear_and_leaves_the_row_alone(self) -> None:
        """#803: ``!clear`` types ``/clear`` into the TUI; the session row is not reset.

        The full behaviour (wake / interrupt / claim) lives in
        ``tests/test_clear_via_tui.py``.
        """
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=MagicMock(closed_at=None))
        cog.repo.reset = AsyncMock(return_value=True)
        tmux_manager = MagicMock()
        tmux_manager.is_claude_running = MagicMock(return_value=True)
        tmux_manager.send_literal = MagicMock(return_value=True)
        tmux_manager.project_dir_for = MagicMock(return_value=None)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_manager)
        ctx = _make_thread_ctx(thread_id=12345)
        await cog.clear_text.callback(cog, ctx)
        tmux_manager.send_literal.assert_called_once_with(12345, "/clear")
        tmux_manager.kill_session.assert_not_called()
        cog.repo.reset.assert_not_called()
        ctx.send.assert_called_once()


class TestRestartClaudeCommand:
    """/claude-restart restarts the Claude process while KEEPING the conversation.

    The defining property vs /clear: it must NOT reset the session row. Killing
    the tmux window leaves a live session_id on disk, so the next message
    resumes via ``--continue`` (the #270 dead-pane path) and context survives.
    """

    @pytest.mark.asyncio
    async def test_outside_thread_sends_ephemeral(self) -> None:
        cog = _make_cog()
        interaction = _make_channel_interaction()
        await cog.claude_restart.callback(cog, interaction)
        kwargs = interaction.response.send_message.call_args.kwargs
        assert kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_kills_window_but_keeps_session(self) -> None:
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=MagicMock())  # a session exists
        cog.repo.reset = AsyncMock()
        tmux = MagicMock()
        tmux.kill_session = MagicMock(return_value=True)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        interaction = _make_thread_interaction(12345)

        await cog.claude_restart.callback(cog, interaction)

        tmux.kill_session.assert_called_once_with(12345)
        # KEY: the session row must survive so --continue can resume it.
        cog.repo.reset.assert_not_called()
        interaction.response.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kills_active_runner(self) -> None:
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=MagicMock())
        cog._resolve_tmux_manager = AsyncMock(return_value=None)
        mock_runner = MagicMock()
        mock_runner.kill = AsyncMock()
        cog._active_runners[12345] = mock_runner
        interaction = _make_thread_interaction(12345)

        await cog.claude_restart.callback(cog, interaction)

        mock_runner.kill.assert_called_once()
        assert 12345 not in cog._active_runners

    @pytest.mark.asyncio
    async def test_no_session_reports_nothing_to_restart(self) -> None:
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=None)  # no session
        cog.repo.reset = AsyncMock()
        tmux = MagicMock()
        tmux.kill_session = MagicMock()
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        interaction = _make_thread_interaction(12345)

        await cog.claude_restart.callback(cog, interaction)

        kwargs = interaction.response.send_message.call_args.kwargs
        assert kwargs.get("ephemeral") is True
        cog.repo.reset.assert_not_called()
        tmux.kill_session.assert_not_called()


class TestRestartClaimAliasKeepsWorking:
    """旧名 `/restart-claude` は #578 の改名後も同じことをする。

    指が覚えているコマンドを壊さないのが Zero-Config Principle。エイリアスは
    新しい実装をそのまま呼ぶので、ここで見るのは「同じ副作用が出るか」だけ。
    """

    @pytest.mark.asyncio
    async def test_slash_alias_kills_window_but_keeps_session(self) -> None:
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=MagicMock())
        cog.repo.reset = AsyncMock()
        tmux = MagicMock()
        tmux.kill_session = MagicMock(return_value=True)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        interaction = _make_thread_interaction(12345)

        await cog.restart_claude.callback(cog, interaction)

        tmux.kill_session.assert_called_once_with(12345)
        cog.repo.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_alias_kills_window_but_keeps_session(self) -> None:
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=MagicMock())
        cog.repo.reset = AsyncMock()
        tmux = MagicMock()
        tmux.kill_session = MagicMock(return_value=True)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        ctx = _make_thread_ctx(thread_id=12345)

        await cog.restart_claude_text.callback(cog, ctx)

        tmux.kill_session.assert_called_once_with(12345)
        cog.repo.reset.assert_not_called()


class TestRestartClaudeTextCommand:
    """!claude-restart mirrors /claude-restart but is invokable from webhooks."""

    @pytest.mark.asyncio
    async def test_outside_thread(self) -> None:
        cog = _make_cog()
        ctx = _make_channel_ctx()
        await cog.claude_restart_text.callback(cog, ctx)
        ctx.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_restarts_keeping_session(self) -> None:
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=MagicMock())
        cog.repo.reset = AsyncMock()
        tmux = MagicMock()
        tmux.kill_session = MagicMock(return_value=True)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        ctx = _make_thread_ctx(thread_id=12345)

        await cog.claude_restart_text.callback(cog, ctx)

        tmux.kill_session.assert_called_once_with(12345)
        cog.repo.reset.assert_not_called()
        ctx.send.assert_called_once()


class TestActiveCountAlias:
    """``active_count`` is the ``DrainAware`` spelling of ``active_session_count``."""

    def test_both_names_count_the_turns_in_flight(self) -> None:
        cog = _make_cog()
        assert cog.active_session_count == 0
        assert cog.active_count == 0

        cog._active_runners[1] = MagicMock()
        cog._active_runners[2] = MagicMock()

        assert cog.active_session_count == 2
        assert cog.active_count == cog.active_session_count

    def test_a_turn_that_ended_is_no_longer_counted(self) -> None:
        cog = _make_cog()
        cog._active_runners[1] = MagicMock()
        cog._active_runners[2] = MagicMock()
        assert cog.active_session_count == 2

        del cog._active_runners[1]

        assert cog.active_session_count == 1
        assert cog.active_count == 1


class TestBuildPrompt:
    """_build_prompt now carries the message text only (#528).

    Attachments used to be pasted in here (small ones) or dropped (large ones).
    They are written to disk by ``_stage_attachments`` instead — covered in
    tests/test_attachment_staging.py — so the only thing left to pin down is
    that the prompt no longer grows with the file.
    """

    @staticmethod
    def _make_attachment(
        filename: str = "test.txt",
        content_type: str = "text/plain",
        size: int = 100,
        body: bytes = b"file content here",
        url: str = "https://cdn.discordapp.com/attachments/123/456/test.txt?ex=abc&is=def&hm=xyz",
    ) -> MagicMock:
        att = MagicMock(spec=discord.Attachment)
        att.filename = filename
        att.content_type = content_type
        att.size = size
        att.url = url
        att.read = AsyncMock(return_value=body)
        return att

    @staticmethod
    def _make_message(content: str = "", attachments: list | None = None) -> MagicMock:
        msg = MagicMock(spec=discord.Message)
        msg.id = 4242
        msg.content = content
        msg.attachments = attachments or []
        return msg

    @pytest.mark.asyncio
    async def test_plain_text_is_the_prompt(self) -> None:
        cog = _make_cog()
        assert await cog._build_prompt(self._make_message(content="hello")) == "hello"

    @pytest.mark.asyncio
    async def test_a_message_without_text_gives_an_empty_prompt(self) -> None:
        cog = _make_cog()
        msg = self._make_message()
        msg.content = None
        assert await cog._build_prompt(msg) == ""

    @pytest.mark.asyncio
    async def test_attachment_contents_are_not_inlined(self) -> None:
        """Inlining is what pushed a 20KB .md past tmux's input cap (#527)."""
        cog = _make_cog()
        att = self._make_attachment()
        msg = self._make_message(content="check this", attachments=[att])

        result = await cog._build_prompt(msg)
        assert result == "check this"
        assert "file content here" not in result
        att.read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_image_url_is_not_embedded(self) -> None:
        """Images are saved as files too — no signed CDN URL to expire (#529)."""
        cog = _make_cog()
        att = self._make_attachment(filename="image.png", content_type="image/png")
        msg = self._make_message(content="look", attachments=[att])

        prompt, image_paths = await cog._build_prompt_and_images(msg)

        assert prompt == "look"
        assert "cdn.discordapp.com" not in prompt
        assert image_paths == []


class TestRegistryAutoDiscovery:
    """Zero-config: the cog finds the bot's ``SessionRegistry`` without being handed it."""

    def test_the_registry_on_the_bot_is_used(self) -> None:
        bot = MagicMock()
        bot.session_registry = SessionRegistry()

        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())

        assert cog._registry is bot.session_registry

    def test_a_registry_passed_in_wins_over_the_bots(self) -> None:
        bot = MagicMock()
        bot.session_registry = SessionRegistry()
        mine = SessionRegistry()

        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), registry=mine)

        assert cog._registry is mine

    def test_a_bot_without_a_registry_leaves_it_unset(self) -> None:
        bot = MagicMock(spec=["channel_id"])  # a bot that carries nothing else

        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())

        assert cog._registry is None


class TestInterruptOnNewMessage:
    """A message that arrives mid-turn replaces the turn in flight (#315)."""

    def _make_thread_message(self, thread_id: int = 42) -> MagicMock:
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.parent_id = 999
        thread.send = AsyncMock()
        msg = MagicMock(spec=discord.Message)
        msg.id = 1
        msg.channel = thread
        msg.content = "new instruction"
        msg.attachments = []
        msg.reference = None  # no Discord-reply → enrich_discord_references is a no-op
        return msg

    @staticmethod
    async def _let_the_new_turn_run(cog: ClaudeChatCog, thread_id: int) -> None:
        """The turn is dispatched as its own task off the lock — wait for it."""
        spawned = cog._active_tasks.get(thread_id)
        assert spawned is not None
        await asyncio.wait_for(spawned, timeout=1.0)

    def _plant_active_turn(self, cog: ClaudeChatCog, thread_id: int) -> MagicMock:
        """Make ``thread_id`` look like it has an in-flight turn.

        Plants an active task plus a runner whose ``interrupt`` lets the task
        finish (mirroring a SIGINT breaking the runner's poll loop).  Returns the
        runner so the caller can assert it was interrupted (#315).
        """
        runner = MagicMock()
        done = asyncio.Event()
        runner.interrupt = AsyncMock(side_effect=lambda *a, **k: done.set())
        cog._active_runners[thread_id] = runner

        async def active_turn() -> None:
            try:
                await done.wait()
            finally:
                cog._active_runners.pop(thread_id, None)
                cog._active_tasks.pop(thread_id, None)

        cog._active_tasks[thread_id] = asyncio.ensure_future(active_turn())
        return runner

    @pytest.mark.asyncio
    async def test_interrupt_called_when_prior_turn_active(self) -> None:
        """A new message interrupts the in-flight turn (USER_GUIDE behaviour, #315)."""
        cog = _make_cog()
        thread_id = 42
        cog._run_claude = AsyncMock()
        existing_runner = self._plant_active_turn(cog, thread_id)

        await cog._handle_thread_reply(self._make_thread_message(thread_id))
        await self._let_the_new_turn_run(cog, thread_id)

        existing_runner.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interrupt_message_sent_when_preempting(self) -> None:
        """The thread is notified when an in-flight turn is interrupted (#315)."""
        cog = _make_cog()
        thread_id = 42
        cog._run_claude = AsyncMock()
        msg = self._make_thread_message(thread_id)
        self._plant_active_turn(cog, thread_id)

        await cog._handle_thread_reply(msg)
        await self._let_the_new_turn_run(cog, thread_id)

        notices = [c.args[0] for c in msg.channel.send.call_args_list if c.args]
        assert len(notices) == 1
        assert "Interrupted" in notices[0]

    @pytest.mark.asyncio
    async def test_an_idle_thread_gets_no_interrupt(self) -> None:
        """Nothing in flight: the message just runs, and nobody is told otherwise."""
        cog = _make_cog()
        thread_id = 42
        cog._run_claude = AsyncMock()
        bystander = MagicMock()
        bystander.interrupt = AsyncMock()
        cog._active_runners[777] = bystander  # another thread's turn
        msg = self._make_thread_message(thread_id)

        await cog._handle_thread_reply(msg)
        await self._let_the_new_turn_run(cog, thread_id)

        bystander.interrupt.assert_not_awaited()
        msg.channel.send.assert_not_called()
        cog._run_claude.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_finished_turn_left_registered_is_not_interrupted(self) -> None:
        """Only a turn still running counts as in flight."""
        cog = _make_cog()
        thread_id = 42
        cog._run_claude = AsyncMock()

        async def already_over() -> None:
            return None

        finished = asyncio.ensure_future(already_over())
        await finished
        cog._active_tasks[thread_id] = finished
        msg = self._make_thread_message(thread_id)

        await cog._handle_thread_reply(msg)
        await self._let_the_new_turn_run(cog, thread_id)

        msg.channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_new_turn_starts_only_after_the_old_one_has_finished(self) -> None:
        """The replaced turn's teardown completes before the new turn begins."""
        cog = _make_cog()
        thread_id = 42
        order: list[str] = []
        interrupted = asyncio.Event()
        runner = MagicMock()
        runner.interrupt = AsyncMock(side_effect=lambda *a, **k: interrupted.set())
        cog._active_runners[thread_id] = runner

        async def old_turn() -> None:
            await interrupted.wait()
            await asyncio.sleep(0)  # its teardown takes a moment
            order.append("old turn finished")
            cog._active_runners.pop(thread_id, None)

        async def new_turn(*args, **kwargs) -> None:
            order.append("new turn started")

        cog._active_tasks[thread_id] = asyncio.ensure_future(old_turn())
        cog._run_claude = new_turn

        await cog._handle_thread_reply(self._make_thread_message(thread_id))

        # The new turn now runs as its own task off the lock (#315) — let it run.
        spawned = cog._active_tasks.get(thread_id)
        assert spawned is not None
        await asyncio.wait_for(spawned, timeout=1.0)

        assert order == ["old turn finished", "new turn started"]

    @pytest.mark.asyncio
    async def test_the_new_turn_continues_the_same_session(self) -> None:
        """Interrupting does not start a new conversation: the session id is handed on."""
        cog = _make_cog()
        thread_id = 42
        record = MagicMock()
        record.session_id = "sess-keep"
        record.closed_at = None
        record.slept_at = None
        record.working_dir = None
        cog.repo.get = AsyncMock(return_value=record)
        cog._run_claude = AsyncMock()
        self._plant_active_turn(cog, thread_id)
        msg = self._make_thread_message(thread_id)

        await cog._handle_thread_reply(msg)
        await self._let_the_new_turn_run(cog, thread_id)

        cog._run_claude.assert_awaited_once()
        call = cog._run_claude.await_args
        assert call.args[:3] == (msg, msg.channel, "new instruction")
        assert call.kwargs["session_id"] == "sess-keep"
        # The pane is alive — it was only interrupted — so nothing is resumed from disk.
        assert call.kwargs["try_continue"] is False

    @pytest.mark.asyncio
    async def test_parked_menu_run_does_not_wedge_thread(self) -> None:
        """A turn parked on a bridged menu must not wedge the thread (#315).

        Production repro of the deadlock: the per-thread lock used to be held
        across the *entire* ``_run_claude``, so a turn parked on an
        AskUserQuestion / Plan-approval menu bridged to Discord buttons and never
        clicked (``AskView`` is ``timeout=None``, up to 24h) held the lock
        forever — every later message blocked on ``async with lock`` and never
        reached tmux, with no error and no log. The fix dispatches the run off the
        lock and pre-empts a menu-parked turn (detected via the ask-bus) so the
        new message runs.
        """
        from c_lord.discord_ui.ask_bus import ask_bus

        cog = _make_cog()
        thread_id = 42
        run_calls: list[str] = []

        async def run_claude_stub(user_message, thread, prompt, **kwargs) -> None:
            run_calls.append(prompt)
            if len(run_calls) == 1:
                # Model a turn parked on a bridged menu: register an ask-bus
                # waiter (as bridge_pane_ask does) and block on the Discord button.
                runner = MagicMock()
                runner.interrupt = AsyncMock()
                cog._active_runners[thread_id] = runner
                cog._active_tasks[thread_id] = asyncio.current_task()
                answer_q = ask_bus.register(thread_id)
                try:
                    await answer_q.get()  # timeout=None — the #315 park point
                finally:
                    ask_bus.unregister(thread_id)
                    cog._active_runners.pop(thread_id, None)
                    cog._active_tasks.pop(thread_id, None)

        cog._run_claude = run_claude_stub

        # First message starts the turn, which parks on the bridged menu.
        task1 = asyncio.ensure_future(
            cog._handle_thread_reply(self._make_thread_message(thread_id))
        )
        await asyncio.sleep(0.05)
        assert run_calls == ["new instruction"], "first turn should start and park"
        assert ask_bus.is_active(thread_id), "first turn should be parked on the menu"
        existing_runner = cog._active_runners[thread_id]

        # Second message must pre-empt the menu-parked turn and run — not hang on
        # the (previously held-across-run) per-thread lock.
        try:
            await asyncio.wait_for(
                cog._handle_thread_reply(self._make_thread_message(thread_id)),
                timeout=2.0,
            )
        except TimeoutError:
            ask_bus.unregister(thread_id)
            task1.cancel()
            pytest.fail(
                "DEADLOCK: a menu-parked turn blocked a new message — Discord "
                "input would never reach tmux (#315 repro)."
            )

        existing_runner.interrupt.assert_awaited_once()
        await asyncio.sleep(0.05)
        assert len(run_calls) == 2, "the new message's own turn should have run"
        assert not ask_bus.is_active(thread_id), "the bridged menu should be dismissed"
        await asyncio.wait_for(task1, timeout=1.0)

    def test_a_new_cog_tracks_no_turns(self) -> None:
        """Both per-thread tables exist from the start, and start empty."""
        cog = _make_cog()
        assert cog._active_tasks == {}
        assert cog._active_runners == {}
        assert cog.is_processing(42) is False

    def test_is_processing_reflects_active_tasks(self) -> None:
        """is_processing() is the lamp's source of truth for 🟢 running (#236):
        True iff a Claude turn is currently registered for the thread."""
        cog = _make_cog()
        assert cog.is_processing(42) is False
        cog._active_tasks[42] = MagicMock()
        assert cog.is_processing(42) is True
        del cog._active_tasks[42]
        assert cog.is_processing(42) is False


class TestResumeAfterPaneDeath:
    """#270: a normal thread reply must resume the on-disk session via --continue
    when the tmux pane has died (bot restart / kill -9 / tmux-server death), instead
    of starting fresh and discarding the transcript that is still on disk.

    The decision surfaces as ``_run_claude(..., try_continue=...)``:
      - pane dead + prior session exists → ``try_continue=True``  (resume via --continue)
      - tmux pane still alive            → ``try_continue=False`` (live send_input)
      - session cleared (/clear)         → ``try_continue=False`` (stay fresh, #123 Part 1)

    Refs #123 Part 2 — the existing --continue fallback only covers the
    restart-resume path (on_ready → pending_resumes); the ordinary
    message-triggered reply path is uncovered.
    """

    def _make_thread_message(self, thread_id: int = 42) -> MagicMock:
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.parent_id = 999
        thread.send = AsyncMock()
        msg = MagicMock(spec=discord.Message)
        msg.channel = thread
        msg.content = "what is my name?"
        msg.attachments = []
        msg.reference = None
        return msg

    @staticmethod
    def _record(session_id: str) -> MagicMock:
        """A session row for this thread.

        ``slept_at`` is spelled out because a bare ``MagicMock`` attribute is
        truthy, and the reply path reads that field to decide whether to
        announce the recovery (#572): an unset mock would silently claim every
        one of these threads had been put to sleep.
        """
        record = MagicMock()
        record.session_id = session_id
        record.slept_at = None
        return record

    def _cog_with_pane(self, *, running: bool) -> ClaudeChatCog:
        """Cog whose tmux pane for the thread is alive (running) or dead."""
        cog = _make_cog()
        tmux_mgr = MagicMock()
        tmux_mgr.is_claude_running.return_value = running
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)
        cog._run_claude = AsyncMock()
        return cog

    @pytest.mark.asyncio
    async def test_resumes_with_continue_when_pane_dead_and_prior_session(self) -> None:
        """RED (#270): pane died but a prior session exists → must pass try_continue=True."""
        cog = self._cog_with_pane(running=False)
        record = self._record("abc-123")
        cog.repo.get = AsyncMock(return_value=record)

        await cog._handle_thread_reply(self._make_thread_message())

        cog._run_claude.assert_called_once()
        kwargs = cog._run_claude.call_args.kwargs
        assert kwargs.get("try_continue") is True

    @pytest.mark.asyncio
    async def test_recovery_is_announced_to_user(self) -> None:
        """#464 ②: when the dead pane is auto-resumed, tell the user.

        During the 2026-06-25 incident the tmux server was gone and the reply
        path silently re-emitted the prior turn's output via ``--continue``,
        which looked like the bot was broken / replaying garbage.  A visible
        notice makes the recovery legible instead of confusing.
        """
        cog = self._cog_with_pane(running=False)
        record = self._record("abc-123")
        cog.repo.get = AsyncMock(return_value=record)
        msg = self._make_thread_message()

        await cog._handle_thread_reply(msg)

        sent = [c.args[0] for c in msg.channel.send.call_args_list if c.args]
        assert any("復元" in s for s in sent), f"no recovery notice sent; got {sent}"

    @pytest.mark.asyncio
    async def test_no_recovery_notice_when_pane_alive(self) -> None:
        """Pane alive → normal live continuation; must NOT post a recovery notice."""
        cog = self._cog_with_pane(running=True)
        record = self._record("abc-123")
        cog.repo.get = AsyncMock(return_value=record)
        msg = self._make_thread_message()

        await cog._handle_thread_reply(msg)

        sent = [c.args[0] for c in msg.channel.send.call_args_list if c.args]
        assert not any("復元" in s for s in sent), f"unexpected recovery notice: {sent}"

    @pytest.mark.asyncio
    async def test_no_continue_when_session_cleared(self) -> None:
        """/clear invariant (#123 Part 1): session was reset → stay fresh, never --continue."""
        cog = self._cog_with_pane(running=False)
        record = self._record("")  # /clear resets session_id to empty
        cog.repo.get = AsyncMock(return_value=record)

        await cog._handle_thread_reply(self._make_thread_message())

        cog._run_claude.assert_called_once()
        kwargs = cog._run_claude.call_args.kwargs
        assert not kwargs.get("try_continue")

    @pytest.mark.asyncio
    async def test_no_continue_when_pane_alive(self) -> None:
        """Pane still alive → live send_input continues context; must not force --continue."""
        cog = self._cog_with_pane(running=True)
        record = self._record("abc-123")
        cog.repo.get = AsyncMock(return_value=record)

        await cog._handle_thread_reply(self._make_thread_message())

        cog._run_claude.assert_called_once()
        kwargs = cog._run_claude.call_args.kwargs
        assert not kwargs.get("try_continue")


class TestZeroConfigCoordination:
    """The coordination service needs no wiring from the consumer.
    Consumers must get new features by updating the package alone —
    so the cog finds a ``CoordinationService`` on its own, or builds one.
    """

    ENV = "COORDINATION_CHANNEL_ID"

    @staticmethod
    def _cog(bot_service: object = None, **kwargs: object) -> ClaudeChatCog:
        bot = MagicMock()
        bot.coordination = bot_service
        return ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), **kwargs)

    def test_one_is_built_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv(self.ENV, "4242")
        cog = self._cog()

        service = cog._get_coordination()

        assert isinstance(service, CoordinationService)
        assert service.channel_id == 4242
        assert service.enabled is True
        assert service.bot is cog.bot

    def test_without_the_variable_the_service_does_nothing(self, monkeypatch) -> None:
        """There is always a service to call — unconfigured, it is just inert."""
        monkeypatch.delenv(self.ENV, raising=False)

        service = self._cog()._get_coordination()

        assert isinstance(service, CoordinationService)
        assert service.channel_id is None
        assert service.enabled is False

    def test_a_bot_with_no_coordination_attribute_at_all_is_fine(self, monkeypatch) -> None:
        """A consumer's own bot class need not know the attribute exists."""
        monkeypatch.setenv(self.ENV, "4242")
        bare_bot = MagicMock(spec=[])
        assert not hasattr(bare_bot, "coordination")
        cog = ClaudeChatCog(bot=bare_bot, repo=MagicMock(), runner=MagicMock())

        service = cog._get_coordination()

        assert isinstance(service, CoordinationService)
        assert service.channel_id == 4242
        assert service.bot is bare_bot

    def test_the_bots_service_wins_over_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv(self.ENV, "4242")
        on_the_bot = CoordinationService(MagicMock(), 7)

        assert self._cog(on_the_bot)._get_coordination() is on_the_bot

    def test_the_constructor_argument_wins_over_the_bots(self, monkeypatch) -> None:
        monkeypatch.setenv(self.ENV, "4242")
        on_the_bot = CoordinationService(MagicMock(), 7)
        handed_in = CoordinationService(MagicMock(), 8)

        cog = self._cog(on_the_bot, coordination=handed_in)

        assert cog._get_coordination() is handed_in

    def test_the_service_is_built_once(self, monkeypatch) -> None:
        monkeypatch.setenv(self.ENV, "4242")
        cog = self._cog()

        first = cog._get_coordination()
        monkeypatch.setenv(self.ENV, "9999")  # a later change is not picked up

        assert cog._get_coordination() is first
        assert first.channel_id == 4242


class TestSpawnSession:
    """``spawn_session`` — the door for callers that are code rather than people."""

    @staticmethod
    def _thread_under_channel(thread_id: int = 4300) -> tuple[MagicMock, MagicMock]:
        """A thread, and the channel whose ``create_thread`` hands it out."""
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 999
        channel.create_thread = AsyncMock(return_value=thread)
        return thread, channel

    @pytest.mark.asyncio
    async def test_a_public_thread_is_created_and_handed_back(self) -> None:
        bot = MagicMock()
        bot.settings_repo = None
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        thread, channel = self._thread_under_channel()

        with patch.object(cog, "_run_claude", new=AsyncMock()):
            returned = await cog.spawn_session(channel, "fix the login bug")
            await asyncio.sleep(0)  # let the background turn start

        assert returned is thread
        channel.create_thread.assert_awaited_once()
        call_kwargs = channel.create_thread.call_args.kwargs
        assert call_kwargs["type"] == discord.ChannelType.public_thread
        assert call_kwargs["name"] == "fix the login bug"
        # Defaults to 3 days (4320 min) when no setting is configured.
        assert call_kwargs["auto_archive_duration"] == 4320

    @pytest.mark.asyncio
    async def test_a_given_thread_name_is_used(self) -> None:
        bot = MagicMock()
        bot.settings_repo = None
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        _thread, channel = self._thread_under_channel()

        with patch.object(cog, "_run_claude", new=AsyncMock()):
            await cog.spawn_session(channel, "a long prompt nobody wants as a title", "Nightly")
            await asyncio.sleep(0)

        assert channel.create_thread.call_args.kwargs["name"] == "Nightly"

    @pytest.mark.asyncio
    async def test_the_prompt_is_posted_and_the_turn_runs_on_that_message(self) -> None:
        """The status reactions need a real message, so the bot posts the prompt
        itself and Claude is started on it — in the background."""
        bot = MagicMock()
        bot.settings_repo = None
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        thread, channel = self._thread_under_channel()
        seed = thread.send.return_value

        with patch.object(cog, "_run_claude", new=AsyncMock()) as run:
            await cog.spawn_session(channel, "do the thing", session_id="sess-9")
            await asyncio.sleep(0)

        thread.send.assert_awaited_once_with("do the thing")
        run.assert_awaited_once_with(
            seed, thread, "do the thing", session_id="sess-9", requester=None, started_by=None
        )

    @pytest.mark.asyncio
    async def test_a_named_repo_is_bound_to_the_thread_before_claude_starts(self) -> None:
        """#514: the binding decides where the session dir is cloned from."""
        channel_cog = _make_channel_cog_mock()
        order: list[str] = []
        channel_cog.bind_thread = AsyncMock(side_effect=lambda *a, **k: order.append("bind"))
        cog = _make_cog(channel_cog=channel_cog)
        thread, channel = self._thread_under_channel()
        thread.send = AsyncMock(side_effect=lambda *a, **k: order.append("seed"))

        with patch.object(cog, "_run_claude", new=AsyncMock()):
            await cog.spawn_session(channel, "go", repo="https://github.com/x/y")
            await asyncio.sleep(0)

        channel_cog.bind_thread.assert_awaited_once_with(4300, "https://github.com/x/y", 999)
        assert order == ["bind", "seed"]

    @pytest.mark.asyncio
    async def test_a_refused_thread_creation_is_reported_as_such(self) -> None:
        """#443: the caller is told it was a permission problem, and nothing runs."""
        from c_lord.cogs.claude_chat import ThreadCreateForbiddenError

        cog = _make_cog()
        _thread, channel = self._thread_under_channel()
        channel.create_thread = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "Missing Permissions")
        )

        with (
            patch.object(cog, "_run_claude", new=AsyncMock()) as run,
            pytest.raises(ThreadCreateForbiddenError),
        ):
            await cog.spawn_session(channel, "go")

        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refused_seed_message_is_explained_in_the_channel(self) -> None:
        """#75: the thread exists but cannot be written to — say so where it can be read."""
        cog = _make_cog()
        thread, channel = self._thread_under_channel()
        thread.send = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "Missing Permissions")
        )
        channel.send = AsyncMock()

        with (
            patch.object(cog, "_run_claude", new=AsyncMock()) as run,
            pytest.raises(discord.Forbidden),
        ):
            await cog.spawn_session(channel, "go")

        channel.send.assert_awaited_once()
        run.assert_not_called()


class TestDefaultThreadName:
    """#721: the name a new thread is opened with must be readable.

    Both creation paths used to write ``prompt[:100]`` straight into the thread
    name, so a dispatch prompt (instruction line + URL line + ``**title**``)
    became one unreadable run that also stops mid-word — and that raw cut stays
    in the channel forever on the "started a thread" line.
    """

    DISPATCH = (
        "#999 を担当してください。\n"
        "https://github.com/yousan/c-lord/issues/999\n"
        "**bug(P1): とても長いタイトルがここに延々と続きます**"
    )

    @pytest.mark.asyncio
    async def test_message_triggered_thread_gets_a_readable_name(self) -> None:
        cog = _make_cog()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        message = MagicMock(spec=discord.Message)
        message.id = 7
        message.content = self.DISPATCH
        message.create_thread = AsyncMock(return_value=thread)
        cog._build_prompt_and_images = AsyncMock(return_value=("p", []))
        cog._run_claude = AsyncMock()

        await cog._handle_new_conversation(message)

        name = message.create_thread.await_args.kwargs["name"]
        assert name == "#999 を担当してください。"

    @pytest.mark.asyncio
    async def test_spawned_thread_gets_a_readable_name(self) -> None:
        from unittest.mock import patch

        cog = _make_cog()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 43
        thread.send = AsyncMock()
        channel = MagicMock()
        channel.create_thread = AsyncMock(return_value=thread)

        with patch.object(cog, "_run_claude", new=AsyncMock()):
            await cog.spawn_session(channel, self.DISPATCH)

        name = channel.create_thread.await_args.kwargs["name"]
        assert name == "#999 を担当してください。"

    @pytest.mark.asyncio
    async def test_explicit_thread_name_is_still_honoured(self) -> None:
        """An explicit name is the caller's words — only newlines are dropped."""
        from unittest.mock import patch

        cog = _make_cog()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 44
        thread.send = AsyncMock()
        channel = MagicMock()
        channel.create_thread = AsyncMock(return_value=thread)

        with patch.object(cog, "_run_claude", new=AsyncMock()):
            await cog.spawn_session(channel, self.DISPATCH, thread_name="手で付けた名前")

        assert channel.create_thread.await_args.kwargs["name"] == "手で付けた名前"


class TestOnReady:
    """What ``on_ready`` does with the restart records the last shutdown left."""

    @staticmethod
    def _thread(thread_id: int) -> MagicMock:
        """A thread under a text channel — the only kind a notice is posted to."""
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.send = AsyncMock()
        thread.parent = MagicMock(spec=discord.TextChannel)
        return thread

    @pytest.mark.asyncio
    async def test_without_a_resume_repo_nothing_is_looked_up(self) -> None:
        cog = _make_cog()
        cog._resume_repo = None
        cog.bot.get_channel = MagicMock(return_value=None)

        await cog.on_ready()

        assert cog._lamp_recovery_tasks == set()
        cog.bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_bot_with_no_optional_attributes_starts_up_cleanly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Zero-config: a bot carrying no ``resume_repo`` / ``ask_repo`` — not even
        a ``user`` yet — still gets through ``on_ready`` and its startup recovery.

        Each recovery step has an "there is nothing to do" way out for exactly
        this bot. Missing one of them does not crash startup (the steps are
        isolated) — it shows up as a WARNING about something that could not be
        read, so that is what is watched for.
        """
        bare_bot = MagicMock(spec=[])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        cog = ClaudeChatCog(
            bot=bare_bot,
            repo=repo,
            runner=MagicMock(),
            authorizer=Authorizer(allow_anyone=True),
        )
        assert cog._resume_repo is None
        assert cog._ask_repo is None

        recovery_loggers = (
            "c_lord.ask_menu_recovery",
            "c_lord.stale_stop_buttons",
            "c_lord.startup_recovery",
        )
        with caplog.at_level(logging.DEBUG):
            await cog.on_ready()
            await asyncio.wait_for(cog._stop_sweep_task, timeout=5)

        assert cog._stop_sweep_task.exception() is None
        assert cog._lamp_recovery_tasks == set()
        complaints = [
            r.getMessage()
            for r in caplog.records
            if r.name in recovery_loggers and r.levelno >= logging.WARNING
        ]
        assert complaints == []
        # The dead-button sweep says why it did nothing: nobody is logged in.
        assert any(
            r.name == "c_lord.stale_stop_buttons" and "not logged in" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_with_no_records_nothing_is_posted(self) -> None:
        from c_lord.database.resume_repo import PendingResumeRepository

        resume_repo = MagicMock(spec=PendingResumeRepository)
        resume_repo.get_pending = AsyncMock(return_value=[])
        resume_repo.delete = AsyncMock()
        cog = _make_cog()
        cog._resume_repo = resume_repo
        cog.bot.get_channel = MagicMock(return_value=None)

        await cog.on_ready()

        resume_repo.get_pending.assert_awaited_once()
        resume_repo.delete.assert_not_called()
        assert cog._lamp_recovery_tasks == set()
        # No record, no thread to look for.
        cog.bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_ready_deletes_before_acting(self) -> None:
        """Row must be deleted BEFORE acting on it (single-fire guarantee).

        #406: the "action" is now posting a quiet restart notice (no re-prompt),
        but the delete-first ordering that prevents a double-fire across a racy
        reconnect must be preserved.
        """
        from c_lord.database.resume_repo import PendingResume, PendingResumeRepository

        call_order: list[str] = []
        pending = PendingResume(
            id=1,
            thread_id=6100,
            session_id="sess-6100",
            reason="bot_shutdown",
            resume_prompt=None,
            created_at="2026-06-12 05:00:00",
        )
        resume_repo = MagicMock(spec=PendingResumeRepository)
        resume_repo.get_pending = AsyncMock(return_value=[pending])
        resume_repo.delete = AsyncMock(side_effect=lambda *a, **k: call_order.append("delete"))
        thread = self._thread(6100)
        cog = _make_cog()
        cog._resume_repo = resume_repo
        cog.bot.get_channel = MagicMock(return_value=thread)
        thread.send = AsyncMock(side_effect=lambda *a, **k: call_order.append("notice"))

        with patch.object(cog, "_run_claude", new=AsyncMock()) as mock_run:
            await cog.on_ready()
            await asyncio.gather(*cog._lamp_recovery_tasks)

        assert call_order == ["delete", "notice"], (
            "delete() must be called before the restart notice to prevent double-fire"
        )
        # #406: no re-prompt — Claude is never re-run on restart.
        mock_run.assert_not_called()

    def _cog_with_records(self, *pairs: tuple[int, int]) -> tuple[ClaudeChatCog, MagicMock]:
        """A cog whose resume repo holds one record per (record id, thread id) pair."""
        from c_lord.database.resume_repo import PendingResume, PendingResumeRepository

        records = [
            PendingResume(
                id=record_id,
                thread_id=thread_id,
                session_id=f"sess-{thread_id}",
                reason="bot_shutdown",
                resume_prompt=None,
                created_at="2026-06-12 05:00:00",
            )
            for record_id, thread_id in pairs
        ]
        resume_repo = MagicMock(spec=PendingResumeRepository)
        resume_repo.get_pending = AsyncMock(return_value=records)
        resume_repo.delete = AsyncMock()
        cog = _make_cog()
        cog._resume_repo = resume_repo
        return cog, resume_repo

    @pytest.mark.asyncio
    async def test_a_record_for_a_plain_channel_is_dropped_without_a_notice(self) -> None:
        """Only threads get a notice — but the record is used up all the same."""
        cog, resume_repo = self._cog_with_records((5, 6300))
        not_a_thread = MagicMock(spec=discord.TextChannel)
        not_a_thread.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=not_a_thread)

        await cog.on_ready()

        resume_repo.delete.assert_awaited_once_with(5)
        not_a_thread.send.assert_not_called()
        assert cog._lamp_recovery_tasks == set()

    @pytest.mark.asyncio
    async def test_a_thread_outside_a_text_channel_is_skipped(self) -> None:
        cog, resume_repo = self._cog_with_records((6, 6400))
        thread = self._thread(6400)
        thread.parent = MagicMock(spec=discord.ForumChannel)
        cog.bot.get_channel = MagicMock(return_value=thread)

        await cog.on_ready()

        resume_repo.delete.assert_awaited_once_with(6)
        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_thread_missing_from_the_cache_is_fetched(self) -> None:
        cog, _resume_repo = self._cog_with_records((7, 6500))
        thread = self._thread(6500)
        cog.bot.get_channel = MagicMock(return_value=None)
        cog.bot.fetch_channel = AsyncMock(return_value=thread)

        await cog.on_ready()
        await asyncio.gather(*cog._lamp_recovery_tasks)

        cog.bot.fetch_channel.assert_awaited_once_with(6500)
        thread.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_one_unreachable_thread_does_not_cost_the_others_their_notice(self) -> None:
        cog, resume_repo = self._cog_with_records((8, 6600), (9, 6700))
        reachable = self._thread(6700)
        cog.bot.get_channel = MagicMock(return_value=None)
        cog.bot.fetch_channel = AsyncMock(
            side_effect=[discord.NotFound(MagicMock(status=404), "Unknown Channel"), reachable]
        )

        await cog.on_ready()
        await asyncio.gather(*cog._lamp_recovery_tasks)

        assert [c.args[0] for c in resume_repo.delete.await_args_list] == [8, 9]
        reachable.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_notice_that_cannot_be_posted_does_not_stop_startup(self) -> None:
        cog, _resume_repo = self._cog_with_records((10, 6800), (11, 6900))
        broken, fine = self._thread(6800), self._thread(6900)
        broken.send = AsyncMock(side_effect=RuntimeError("discord is having a day"))
        cog.bot.get_channel = MagicMock(side_effect={6800: broken, 6900: fine}.get)

        await cog.on_ready()
        await asyncio.gather(*cog._lamp_recovery_tasks)

        fine.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_ready_posts_quiet_notice_and_does_not_reprompt(self) -> None:
        """#406: restart must NOT re-prompt Claude — only post a quiet notice.

        The ``claude`` process in tmux survives a bot restart, and the observers
        (TranscriptMirror + menu watchdog) self-restore on startup.  Re-running
        Claude with a "continue your work" prompt therefore only causes unwanted
        autonomous progress (意図しない前進) and makes the conversation appear to
        "jump".  on_ready must instead post a subtle ``-#`` subtext notice and
        leave the live session alone.
        """
        from c_lord.database.resume_repo import PendingResume, PendingResumeRepository

        pending = PendingResume(
            id=9,
            thread_id=6200,
            session_id="sess-6200",
            reason="bot_shutdown",
            resume_prompt=(
                "ボットが再起動しました。前の作業の続きを確認し、必要な残作業があれば完了してください。"
            ),
            created_at="2026-06-12 05:00:00",
        )
        resume_repo = MagicMock(spec=PendingResumeRepository)
        resume_repo.get_pending = AsyncMock(return_value=[pending])
        resume_repo.delete = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 6200
        thread.send = AsyncMock()
        thread.parent = MagicMock(spec=discord.TextChannel)

        cog = _make_cog()
        cog._resume_repo = resume_repo
        cog.bot.get_channel = MagicMock(return_value=thread)

        with patch.object(cog, "_run_claude", new=AsyncMock()) as mock_run:
            await cog.on_ready()
            # create_task (if any) is scheduled; yield so it would run.
            await asyncio.sleep(0)

        # No re-prompt: Claude must not be re-run on restart.
        mock_run.assert_not_called()
        # Single-fire still guaranteed (row deleted before acting).
        resume_repo.delete.assert_called_once_with(9)
        # A quiet (-# subtext) notice was posted — and it is NOT the old loud
        # "complete your remaining work" instruction injected into the session.
        thread.send.assert_called_once()
        sent = thread.send.call_args.args[0]
        assert sent.startswith("-#"), f"notice must be quiet -# subtext, got: {sent!r}"
        assert "完了してください" not in sent
        assert "残作業" not in sent


class TestCogUnloadMarkForResume:
    """At shutdown, every turn still running is written down for a restart notice."""

    def _make_cog_with_resume_repo(self) -> tuple[ClaudeChatCog, MagicMock, MagicMock]:
        """Return (cog, session repo, resume repo); the session repo has no rows."""
        from c_lord.database.resume_repo import PendingResumeRepository

        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        repo.flush_unapplied_clears = AsyncMock()
        resume_repo = MagicMock(spec=PendingResumeRepository)
        resume_repo.mark = AsyncMock()
        cog = ClaudeChatCog(bot=MagicMock(), repo=repo, runner=MagicMock(), resume_repo=resume_repo)
        return cog, repo, resume_repo

    @pytest.mark.asyncio
    async def test_with_no_turn_running_nothing_is_recorded(self) -> None:
        cog, repo, resume_repo = self._make_cog_with_resume_repo()

        await cog.cog_unload()

        resume_repo.mark.assert_not_called()
        repo.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_without_a_resume_repo_shutdown_still_completes(self) -> None:
        cog, repo, _resume_repo = self._make_cog_with_resume_repo()
        cog._resume_repo = None
        cog._active_runners[111] = MagicMock()

        await cog.cog_unload()

        repo.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_every_running_thread_is_recorded_once(self) -> None:
        cog, _repo, resume_repo = self._make_cog_with_resume_repo()
        cog._active_runners[111] = MagicMock()
        cog._active_runners[222] = MagicMock()

        await cog.cog_unload()

        assert [c.args[0] for c in resume_repo.mark.await_args_list] == [111, 222]

    @pytest.mark.asyncio
    async def test_a_thread_both_running_and_adopted_is_recorded_once(self) -> None:
        cog, repo, resume_repo = self._make_cog_with_resume_repo()
        repo.update_trigger_message = AsyncMock()
        cog._active_runners[111] = MagicMock()
        cog._adopted_lamps[111] = 4444

        await cog.cog_unload()

        resume_repo.mark.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_adopted_lamp_is_recorded_again(self) -> None:
        """#870 A: a turn whose lamp was taken back after the last restart has
        no runner in this process — but it is still running, and the next
        restart must not orphan its lamp a second time."""
        cog, repo, resume_repo = self._make_cog_with_resume_repo()
        repo.update_trigger_message = AsyncMock()
        cog._adopted_lamps[333] = 4444

        await cog.cog_unload()

        assert {c.args[0] for c in resume_repo.mark.call_args_list} == {333}
        repo.update_trigger_message.assert_awaited_once_with(333, 4444)

    @pytest.mark.asyncio
    async def test_the_running_turns_trigger_is_saved_at_shutdown(self) -> None:
        """#870 B: a thread's first turn writes its trigger before the session
        row exists, so it is written again at shutdown, when the row does."""
        cog, repo, resume_repo = self._make_cog_with_resume_repo()
        repo.update_trigger_message = AsyncMock()
        cog._active_runners[111] = MagicMock()
        cog._turn_triggers[111] = 5555

        await cog.cog_unload()

        repo.update_trigger_message.assert_awaited_once_with(111, 5555)

    @pytest.mark.asyncio
    async def test_the_record_says_shutdown_and_carries_no_prompt(self) -> None:
        """#406: the notice is all a restart posts — there is no prompt to replay."""
        cog, _repo, resume_repo = self._make_cog_with_resume_repo()
        cog._active_runners[111] = MagicMock()

        await cog.cog_unload()

        kwargs = resume_repo.mark.await_args.kwargs
        assert kwargs["reason"] == "bot_shutdown"
        assert kwargs["resume_prompt"] is None

    @pytest.mark.asyncio
    async def test_the_session_id_comes_from_the_session_row(self) -> None:
        cog, repo, resume_repo = self._make_cog_with_resume_repo()
        repo.get = AsyncMock(return_value=MagicMock(session_id="sess-on-record"))
        cog._active_runners[111] = MagicMock()

        await cog.cog_unload()

        repo.get.assert_awaited_once_with(111)
        assert resume_repo.mark.await_args.kwargs["session_id"] == "sess-on-record"

    @pytest.mark.asyncio
    async def test_a_thread_without_a_row_is_recorded_with_no_session_id(self) -> None:
        cog, _repo, resume_repo = self._make_cog_with_resume_repo()
        cog._active_runners[111] = MagicMock()

        await cog.cog_unload()

        assert resume_repo.mark.await_args.kwargs["session_id"] is None

    @pytest.mark.asyncio
    async def test_one_failed_record_does_not_cost_the_others_theirs(self) -> None:
        cog, _repo, resume_repo = self._make_cog_with_resume_repo()
        resume_repo.mark = AsyncMock(side_effect=[RuntimeError("database is locked"), None])
        cog._active_runners[111] = MagicMock()
        cog._active_runners[222] = MagicMock()

        await cog.cog_unload()  # must not raise

        assert [c.args[0] for c in resume_repo.mark.await_args_list] == [111, 222]

    @pytest.mark.asyncio
    async def test_a_lamp_that_cannot_be_saved_does_not_cost_the_notice(self) -> None:
        cog, repo, resume_repo = self._make_cog_with_resume_repo()
        repo.update_trigger_message = AsyncMock(side_effect=RuntimeError("database is locked"))
        cog._active_runners[111] = MagicMock()
        cog._turn_triggers[111] = 5555

        await cog.cog_unload()

        resume_repo.mark.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_trigger_whose_clear_is_waiting_is_not_written_back(self) -> None:
        """#1262: a trigger the turn already forgot must not come back at shutdown."""
        cog, repo, resume_repo = self._make_cog_with_resume_repo()
        repo.update_trigger_message = AsyncMock()
        repo.clear_is_waiting = MagicMock(return_value=True)
        cog._active_runners[111] = MagicMock()
        cog._turn_triggers[111] = 5555

        await cog.cog_unload()

        repo.clear_is_waiting.assert_called_once_with(111, 5555)
        repo.update_trigger_message.assert_not_called()
        resume_repo.mark.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_waiting_trigger_clears_are_flushed_last(self) -> None:
        cog, repo, resume_repo = self._make_cog_with_resume_repo()
        order: list[str] = []
        resume_repo.mark = AsyncMock(side_effect=lambda *a, **k: order.append("mark"))
        repo.flush_unapplied_clears = AsyncMock(side_effect=lambda: order.append("flush"))
        cog._active_runners[111] = MagicMock()

        await cog.cog_unload()

        assert order == ["mark", "flush"]


class TestStartSessionCommand:
    """/claude slash command tests."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self) -> None:
        """When allowed_user_ids is set, unauthorized users get ephemeral error."""
        bot = MagicMock()
        bot.channel_id = 999
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), allowed_user_ids={111})
        interaction = _make_channel_interaction()
        interaction.user = MagicMock()
        interaction.user.id = 999  # not in allowed set

        await cog.start_session.callback(cog, interaction, prompt="hello")

        reply = _first_reply(interaction)
        assert reply.kwargs.get("ephemeral") is True
        assert "not authorized" in reply.args[0].lower()

    @pytest.mark.asyncio
    async def test_an_authorized_user_reaches_the_command(self) -> None:
        """An allowed user is not stopped by the gate (#713 renamed this).

        It used to be called ``test_no_allowed_user_ids_allows_everyone`` and
        relied on the unconfigured default being "everyone".  That default is
        now the app owner only, and *which* users are allowed is pinned in
        tests/test_default_authorization.py — here we only need one who is.
        """
        cc = _make_channel_cog_mock(tmux_manager=MagicMock())
        cog = _make_cog(channel_cog=cc)
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 42
        interaction.channel = MagicMock(spec=discord.TextChannel)
        interaction.channel.id = 999  # matches bot.channel_id
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.mention = "<#thread>"
        cog.spawn_session = AsyncMock(return_value=thread)

        await cog.start_session.callback(cog, interaction, prompt="hello")

        # Should proceed (not reject) — spawn_session called
        cog.spawn_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_channel_execution_calls_spawn_session(self) -> None:
        """Running /claude in a channel creates a new thread via spawn_session."""
        cc = _make_channel_cog_mock(tmux_manager=MagicMock())
        cog = _make_cog(channel_cog=cc)
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 42
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 999  # matches bot.channel_id
        interaction.channel = channel
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.mention = "<#12345>"
        cog.spawn_session = AsyncMock(return_value=thread)

        await cog.start_session.callback(cog, interaction, prompt="build a feature")

        interaction.response.defer.assert_called_once()
        cog.spawn_session.assert_called_once_with(
            channel, "build a feature", repo=None, requester=interaction.user, started_by=ANY
        )
        interaction.followup.send.assert_called_once()
        sent_text = interaction.followup.send.call_args.args[0]
        assert "<#12345>" in sent_text

    @pytest.mark.asyncio
    async def test_thread_execution_calls_run_claude(self) -> None:
        """Running /claude inside a thread continues the session via _run_claude."""
        cc = _make_channel_cog_mock(session_dir_manager=MagicMock())
        cog = _make_cog(channel_cog=cc)
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 42
        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 999
        thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        interaction.channel = thread
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        record = MagicMock()
        record.session_id = "sess-abc"
        cog.repo.get = AsyncMock(return_value=record)
        cog._run_claude = AsyncMock()

        await cog.start_session.callback(cog, interaction, prompt="continue this")

        interaction.response.defer.assert_called_once()
        cog._run_claude.assert_awaited_once()
        kwargs = cog._run_claude.await_args.kwargs
        assert kwargs["session_id"] == "sess-abc"
        assert kwargs["prompt"] == "continue this"

    @pytest.mark.asyncio
    async def test_thread_with_no_trace_of_clord_is_refused(self) -> None:
        """#551: a thread with no session row *and* no sign it was ever c-lord's
        is not ours, so /clord refuses instead of taking it over.

        This used to run with ``session_id=None`` — which is how an ordinary
        human conversation thread became a Claude session, after which every
        message in it went to Claude. (A thread that merely lost its row to the
        30-day sweep takes the other branch and is offered a reconnect.)
        """
        cc = _make_channel_cog_mock(session_dir_manager=MagicMock())
        cog = _make_cog(channel_cog=cc)
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 42
        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 999
        thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        interaction.channel = thread
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        # The refusal lands before the defer, so it comes back through
        # ``interaction.response`` rather than a followup.
        interaction.response.send_message = AsyncMock()
        cog.repo.get = AsyncMock(return_value=None)
        cog._run_claude = AsyncMock()
        cog._was_ever_our_thread = AsyncMock(return_value=False)

        await cog.start_session.callback(cog, interaction, prompt="start fresh")

        cog._run_claude.assert_not_called()
        interaction.response.defer.assert_not_called()
        said = str(interaction.response.send_message.call_args.args[0])
        assert "c-lord のスレッドではない" in said, said
        # Somebody else's conversation: the refusal goes to the caller alone.
        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_clords_thread_names_the_owner(self) -> None:
        """#811: another c-lord's thread is a c-lord thread — just not this bot's.

        Saying 「c-lord のスレッドではない」 there is false; the owner is named
        instead, and nothing is taken over.
        """
        other = 1517328788164182116
        cc = _make_channel_cog_mock(session_dir_manager=MagicMock())
        cog = _make_cog(channel_cog=cc)
        cog.bot.user.id = 1475105094071750818
        cog.bot.get_user = MagicMock(return_value=MagicMock(bot=True))
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 42
        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.parent_id = 999
        thread.owner_id = other
        thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        interaction.channel = thread
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        cog.repo.get = AsyncMock(return_value=None)
        cog._run_claude = AsyncMock()
        cog._was_ever_our_thread = AsyncMock(return_value=False)

        await cog.start_session.callback(cog, interaction, prompt="start fresh")

        cog._run_claude.assert_not_called()
        interaction.response.defer.assert_not_called()
        said = str(interaction.response.send_message.call_args.args[0])
        assert f"<@{other}>" in said, said
        assert "c-lord のスレッドではない" not in said, said
        # Nothing is posted into the other bot's thread.
        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_any_channel_allowed(self) -> None:
        """/clord works in any channel, not just the bot's configured channel."""
        cc = _make_channel_cog_mock(tmux_manager=MagicMock())
        cog = _make_cog(channel_cog=cc)  # bot.channel_id = 999
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 42
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 7777  # different from bot.channel_id
        interaction.channel = channel
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.mention = "<#thread>"
        cog.spawn_session = AsyncMock(return_value=thread)

        await cog.start_session.callback(cog, interaction, prompt="hello")

        interaction.response.defer.assert_called_once()
        cog.spawn_session.assert_called_once_with(
            channel, "hello", repo=None, requester=interaction.user, started_by=ANY
        )


class TestClordTextCommand:
    """!clord mirrors /clord but is invokable from webhooks (E2E)."""

    @pytest.mark.asyncio
    async def test_missing_prompt_shows_usage(self) -> None:
        cog = _make_cog()
        ctx = _make_channel_ctx()
        await cog.clord_text.callback(cog, ctx, prompt=None)
        ctx.send.assert_called_once()
        assert "Usage" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self) -> None:
        bot = MagicMock()
        bot.channel_id = 999
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), allowed_user_ids={111})
        ctx = _make_channel_ctx()  # author.id = 1, not in {111}
        await cog.clord_text.callback(cog, ctx, prompt="hello")
        ctx.send.assert_called_once()
        assert "not authorized" in ctx.send.call_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_channel_spawns_session(self) -> None:
        cc = _make_channel_cog_mock(tmux_manager=MagicMock())
        cog = _make_cog(channel_cog=cc)
        ctx = _make_channel_ctx()
        ctx.channel.id = 999
        thread = MagicMock(spec=discord.Thread)
        thread.mention = "<#12345>"
        cog.spawn_session = AsyncMock(return_value=thread)

        await cog.clord_text.callback(cog, ctx, prompt="build a feature")

        cog.spawn_session.assert_called_once_with(
            ctx.channel, "build a feature", repo=None, requester=ctx.author, started_by=ANY
        )
        assert "<#12345>" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_thread_continues_session(self) -> None:
        cc = _make_channel_cog_mock(session_dir_manager=MagicMock())
        cog = _make_cog(channel_cog=cc)
        ctx = _make_thread_ctx(thread_id=555, parent_id=999)
        ctx.channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        record = MagicMock()
        record.session_id = "sess-abc"
        cog.repo.get = AsyncMock(return_value=record)
        cog._run_claude = AsyncMock()

        await cog.clord_text.callback(cog, ctx, prompt="continue this")

        cog._run_claude.assert_awaited_once()
        kwargs = cog._run_claude.await_args.kwargs
        assert kwargs["session_id"] == "sess-abc"
        assert kwargs["prompt"] == "continue this"

    @pytest.mark.asyncio
    async def test_unbound_channel_rejected(self) -> None:
        cc = _make_channel_cog_mock()  # both managers None
        cog = _make_cog(channel_cog=cc)
        ctx = _make_channel_ctx()
        # This instance's own channel (== bot.channel_id): #522 keeps the
        # guidance here and only silences channels belonging to other instances.
        ctx.channel.id = 999
        cog.spawn_session = AsyncMock()

        await cog.clord_text.callback(cog, ctx, prompt="hello")

        cog.spawn_session.assert_not_called()
        assert "clord-init" in ctx.send.call_args.args[0]


class TestAttachWindowCommand:
    """/clord-attach slash command tests."""

    @pytest.mark.asyncio
    async def test_attach_outside_thread_sends_ephemeral(self) -> None:
        """Using /clord-attach outside a thread sends an ephemeral error."""
        cog = _make_cog()
        interaction = _make_channel_interaction()
        interaction.user = MagicMock()
        interaction.user.id = 42

        await cog.attach_window.callback(cog, interaction, window="work1")

        reply = _first_reply(interaction)
        assert reply.kwargs.get("ephemeral") is True
        assert "thread" in reply.args[0].lower()

    @pytest.mark.asyncio
    async def test_attach_unauthorized_user_rejected(self) -> None:
        """Unauthorized users get ephemeral error."""
        bot = MagicMock()
        bot.channel_id = 999
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), allowed_user_ids={111})
        interaction = _make_thread_interaction()
        interaction.user = MagicMock()
        interaction.user.id = 999  # not in allowed set

        await cog.attach_window.callback(cog, interaction, window="work1")

        reply = _first_reply(interaction)
        assert reply.kwargs.get("ephemeral") is True
        assert "not authorized" in reply.args[0].lower()

    @pytest.mark.asyncio
    async def test_attach_no_tmux_manager(self) -> None:
        """When tmux_manager is not configured, sends ephemeral error."""
        cog = _make_cog()
        cog.bot.tmux_manager = None  # explicitly no tmux
        interaction = _make_thread_interaction()
        interaction.user = MagicMock()
        interaction.user.id = 42

        await cog.attach_window.callback(cog, interaction, window="work1")

        reply = _first_reply(interaction)
        assert reply.kwargs.get("ephemeral") is True
        assert "tmux" in reply.args[0].lower()

    @pytest.mark.asyncio
    async def test_attach_success(self) -> None:
        """Successful remap responds with confirmation and saves DB record."""
        tmux_mgr = MagicMock()
        tmux_mgr.remap_window.return_value = True
        cc = _make_channel_cog_mock(tmux_manager=tmux_mgr)
        cog = _make_cog(channel_cog=cc)

        interaction = _make_thread_interaction(12345)
        interaction.user = MagicMock()
        interaction.user.id = 42

        await cog.attach_window.callback(cog, interaction, window="work3")

        tmux_mgr.remap_window.assert_called_once_with(12345, "work3")
        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.call_args.args[0]
        assert "work3" in msg
        # DB session record must be created so on_message picks up thread replies
        cog.repo.save.assert_called_once_with(12345, session_id="tmux-12345")

    @pytest.mark.asyncio
    async def test_attach_window_not_found(self) -> None:
        """When remap returns False, sends failure message."""
        tmux_mgr = MagicMock()
        tmux_mgr.remap_window.return_value = False
        cc = _make_channel_cog_mock(tmux_manager=tmux_mgr)
        cog = _make_cog(channel_cog=cc)

        interaction = _make_thread_interaction(12345)
        interaction.user = MagicMock()
        interaction.user.id = 42

        await cog.attach_window.callback(cog, interaction, window="nonexistent")

        tmux_mgr.remap_window.assert_called_once_with(12345, "nonexistent")
        cog.repo.save.assert_not_called()
        assert _first_reply(interaction).kwargs.get("ephemeral") is True
        msg = interaction.response.send_message.call_args.args[0]
        assert "nonexistent" in msg


class TestAttachTextCommand:
    """!attach text command tests (E2E-testable alternative to /clord-attach)."""

    def _make_ctx(
        self,
        *,
        thread_id: int = 12345,
        in_thread: bool = True,
        author_id: int = 42,
        webhook_id: int | None = None,
    ) -> MagicMock:
        """Return a commands.Context with a thread channel."""
        ctx = MagicMock()
        ctx.send = AsyncMock()
        ctx.author = MagicMock()
        ctx.author.id = author_id
        if in_thread:
            thread = MagicMock(spec=discord.Thread)
            thread.id = thread_id
            ctx.channel = thread
        else:
            ctx.channel = MagicMock(spec=discord.TextChannel)
        return _attach_human_message(ctx, webhook_id)

    @pytest.mark.asyncio
    async def test_attach_text_success(self) -> None:
        """!attach work13 → remap_window + repo.save called."""
        tmux_mgr = MagicMock()
        tmux_mgr.remap_window.return_value = True
        cc = _make_channel_cog_mock(tmux_manager=tmux_mgr)
        cog = _make_cog(channel_cog=cc)
        ctx = self._make_ctx(thread_id=12345)

        await cog.attach_text.callback(cog, ctx, window="work13")

        tmux_mgr.remap_window.assert_called_once_with(12345, "work13")
        cog.repo.save.assert_called_once_with(12345, session_id="tmux-12345")
        ctx.send.assert_called_once()
        msg = ctx.send.call_args.args[0]
        assert "work13" in msg

    @pytest.mark.asyncio
    async def test_attach_text_outside_thread(self) -> None:
        """!attach in a non-thread channel sends an error."""
        cog = _make_cog()
        cog.bot.tmux_manager = MagicMock()
        ctx = self._make_ctx(in_thread=False)

        await cog.attach_text.callback(cog, ctx, window="work1")

        ctx.send.assert_called_once()
        msg = ctx.send.call_args.args[0]
        assert "thread" in msg.lower()

    @pytest.mark.asyncio
    async def test_attach_text_no_tmux(self) -> None:
        """!attach when tmux_manager is None sends an error."""
        cog = _make_cog()
        cog.bot.tmux_manager = None
        ctx = self._make_ctx()

        await cog.attach_text.callback(cog, ctx, window="work1")

        ctx.send.assert_called_once()
        msg = ctx.send.call_args.args[0]
        assert "tmux" in msg.lower()

    @pytest.mark.asyncio
    async def test_attach_text_window_not_found(self) -> None:
        """!attach with non-existent window sends failure message."""
        tmux_mgr = MagicMock()
        tmux_mgr.remap_window.return_value = False
        cc = _make_channel_cog_mock(tmux_manager=tmux_mgr)
        cog = _make_cog(channel_cog=cc)
        ctx = self._make_ctx()

        await cog.attach_text.callback(cog, ctx, window="nonexistent")

        ctx.send.assert_called_once()
        msg = ctx.send.call_args.args[0]
        assert "nonexistent" in msg

    @pytest.mark.asyncio
    async def test_attach_text_unauthorized(self) -> None:
        """!attach from unauthorized user sends error."""
        bot = MagicMock()
        bot.channel_id = 999
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), allowed_user_ids={111})
        cog.bot.tmux_manager = MagicMock()
        ctx = self._make_ctx(author_id=999)

        await cog.attach_text.callback(cog, ctx, window="work1")

        ctx.send.assert_called_once()
        msg = ctx.send.call_args.args[0]
        assert "authorized" in msg.lower() or "permission" in msg.lower()


class TestOnMessageSkipsTextCommands:
    """on_message must not send !attach messages to _handle_thread_reply."""

    @pytest.mark.asyncio
    async def test_attach_command_not_forwarded_to_handle_thread_reply(self) -> None:
        """!attach in a thread should NOT trigger _handle_thread_reply."""
        cog = _make_cog()
        cog._handle_thread_reply = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.parent_id = 999  # matches bot.channel_id

        msg = MagicMock(spec=discord.Message)
        # An ordinary message type, so it is the command check that stops it.
        msg.type = discord.MessageType.default
        msg.content = "!attach work13"
        msg.channel = thread
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.id = 42
        msg.webhook_id = None

        # Mock get_context to return a valid context (simulating a text command)
        mock_ctx = MagicMock()
        mock_ctx.valid = True
        cog.bot.get_context = AsyncMock(return_value=mock_ctx)

        await cog.on_message(msg)

        cog._handle_thread_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_message_still_forwarded(self) -> None:
        """Regular messages in threads should still reach _handle_thread_reply."""
        cog = _make_cog()
        cog._handle_thread_reply = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.parent_id = 999  # matches bot.channel_id

        msg = MagicMock(spec=discord.Message)
        msg.id = 1
        msg.content = "hello Claude"
        msg.channel = thread
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.id = 42
        msg.webhook_id = None
        msg.type = discord.MessageType.default

        # DB session must exist for opt-in response
        record = MagicMock()
        record.session_id = "sess-123"
        cog.repo.get = AsyncMock(return_value=record)

        # Mock get_context to return an invalid context (not a command)
        mock_ctx = MagicMock()
        mock_ctx.valid = False
        cog.bot.get_context = AsyncMock(return_value=mock_ctx)

        await cog.on_message(msg)

        cog._handle_thread_reply.assert_called_once_with(msg)


class TestOnMessageSkipsSystemMessages:
    """on_message must not send Discord system messages to _handle_thread_reply."""

    def _make_thread_msg(self, msg_type: discord.MessageType) -> MagicMock:
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.parent_id = 999

        msg = MagicMock(spec=discord.Message)
        msg.id = 1
        msg.content = ""
        msg.channel = thread
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.id = 99
        msg.webhook_id = None
        msg.type = msg_type
        return msg

    @pytest.mark.asyncio
    async def test_channel_name_change_not_forwarded(self) -> None:
        """スレッド名変更通知は _handle_thread_reply に到達しない。"""
        cog = _make_cog()
        cog._handle_thread_reply = AsyncMock()
        record = MagicMock()
        cog.repo.get = AsyncMock(return_value=record)
        mock_ctx = MagicMock()
        mock_ctx.valid = False
        cog.bot.get_context = AsyncMock(return_value=mock_ctx)

        msg = self._make_thread_msg(discord.MessageType.channel_name_change)
        await cog.on_message(msg)

        cog._handle_thread_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_pins_add_not_forwarded(self) -> None:
        """ピン留め通知は _handle_thread_reply に到達しない。"""
        cog = _make_cog()
        cog._handle_thread_reply = AsyncMock()
        record = MagicMock()
        cog.repo.get = AsyncMock(return_value=record)
        mock_ctx = MagicMock()
        mock_ctx.valid = False
        cog.bot.get_context = AsyncMock(return_value=mock_ctx)

        msg = self._make_thread_msg(discord.MessageType.pins_add)
        await cog.on_message(msg)

        cog._handle_thread_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_message_forwarded(self) -> None:
        """default タイプは従来どおり _handle_thread_reply に到達する。"""
        cog = _make_cog()
        cog._handle_thread_reply = AsyncMock()
        record = MagicMock()
        record.session_id = "sess-123"
        cog.repo.get = AsyncMock(return_value=record)
        mock_ctx = MagicMock()
        mock_ctx.valid = False
        cog.bot.get_context = AsyncMock(return_value=mock_ctx)

        msg = self._make_thread_msg(discord.MessageType.default)
        msg.content = "hello"
        await cog.on_message(msg)

        cog._handle_thread_reply.assert_called_once_with(msg)

    @pytest.mark.asyncio
    async def test_reply_message_forwarded(self) -> None:
        """reply タイプは従来どおり _handle_thread_reply に到達する。"""
        cog = _make_cog()
        cog._handle_thread_reply = AsyncMock()
        record = MagicMock()
        record.session_id = "sess-456"
        cog.repo.get = AsyncMock(return_value=record)
        mock_ctx = MagicMock()
        mock_ctx.valid = False
        cog.bot.get_context = AsyncMock(return_value=mock_ctx)

        msg = self._make_thread_msg(discord.MessageType.reply)
        msg.content = "a reply"
        await cog.on_message(msg)

        cog._handle_thread_reply.assert_called_once_with(msg)


class TestMultiChannelOnMessage:
    """on_message should handle threads with existing sessions in any channel."""

    def _make_thread_message(self, thread_id: int = 42, parent_id: int = 7777) -> MagicMock:
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.parent_id = parent_id
        msg = MagicMock(spec=discord.Message)
        msg.id = 1
        msg.channel = thread
        msg.content = "follow up"
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.id = 42
        msg.webhook_id = None
        msg.type = discord.MessageType.default
        return msg

    @pytest.mark.asyncio
    async def test_thread_with_session_in_other_channel_handled(self) -> None:
        """A thread in a non-configured channel is handled if it has a DB session."""
        cog = _make_cog()  # bot.channel_id = 999
        message = self._make_thread_message(thread_id=42, parent_id=7777)

        record = MagicMock()
        record.session_id = "sess-xyz"
        cog.repo.get = AsyncMock(return_value=record)
        cog._handle_thread_reply = AsyncMock()

        await cog.on_message(message)

        cog._handle_thread_reply.assert_called_once_with(message)

    @pytest.mark.asyncio
    async def test_thread_without_session_in_other_channel_ignored(self) -> None:
        """A thread in a non-configured channel with no DB session is ignored."""
        cog = _make_cog()  # bot.channel_id = 999
        message = self._make_thread_message(thread_id=42, parent_id=7777)

        cog.repo.get = AsyncMock(return_value=None)
        cog._handle_thread_reply = AsyncMock()

        await cog.on_message(message)

        cog._handle_thread_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_configured_channel_thread_without_session_ignored(self) -> None:
        """Threads under the configured channel are ignored if no DB session exists."""
        cog = _make_cog()  # bot.channel_id = 999
        message = self._make_thread_message(thread_id=42, parent_id=999)

        cog.repo.get = AsyncMock(return_value=None)
        cog._handle_thread_reply = AsyncMock()

        await cog.on_message(message)

        cog._handle_thread_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_configured_channel_thread_with_session_handled(self) -> None:
        """Threads under the configured channel are handled if DB session exists."""
        cog = _make_cog()  # bot.channel_id = 999
        message = self._make_thread_message(thread_id=42, parent_id=999)

        record = MagicMock()
        record.session_id = "sess-abc"
        cog.repo.get = AsyncMock(return_value=record)
        cog._handle_thread_reply = AsyncMock()

        await cog.on_message(message)

        cog._handle_thread_reply.assert_called_once_with(message)


# -- Tests for per-thread Lock (duplicate prevention) -----------------------


class TestThreadLockSerialization:
    """Tests that _handle_thread_reply serializes concurrent calls per thread."""

    @pytest.mark.asyncio
    async def test_concurrent_messages_serialized(self) -> None:
        """Two on_message calls for the same thread should not run _run_claude concurrently.

        Without a per-thread lock, both messages could enter _run_claude before
        the first registers in _active_runners, causing duplicate responses.
        """
        channel_cog = _make_channel_cog_mock(tmux_manager=MagicMock())
        cog = _make_cog(channel_cog=channel_cog)

        # Track the order of _run_claude calls and ensure no overlap
        call_log: list[str] = []

        async def mock_run_claude(*args, **kwargs):
            call_log.append("start")
            await asyncio.sleep(0.1)  # simulate work
            call_log.append("end")

        cog._run_claude = mock_run_claude

        # Create two messages for the same thread
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.send = AsyncMock()

        msg1 = MagicMock(spec=discord.Message)
        msg1.channel = thread
        msg1.content = "first"
        msg1.attachments = []

        msg2 = MagicMock(spec=discord.Message)
        msg2.channel = thread
        msg2.content = "second"
        msg2.attachments = []

        record = MagicMock()
        record.session_id = "sess-1"
        cog.repo.get = AsyncMock(return_value=record)

        # Fire both concurrently
        t1 = asyncio.create_task(cog._handle_thread_reply(msg1))
        t2 = asyncio.create_task(cog._handle_thread_reply(msg2))
        await asyncio.gather(t1, t2)

        # Runs are dispatched as their own tasks off the lock now (#315); the
        # second message pre-empts the first, which is drained before the second
        # run starts — so the two never overlap.  Wait for the last spawned run.
        spawned = cog._active_tasks.get(42)
        if spawned is not None:
            await asyncio.wait_for(spawned, timeout=2.0)

        # No overlap: the runs execute one after the other, never concurrently.
        # With overlap it would be [start, start, end, end].
        assert call_log == ["start", "end", "start", "end"]


class TestClearCommand:
    """Tests for /clear command (issue #117, #123)."""

    @pytest.mark.asyncio
    async def test_clear_outside_thread_sends_ephemeral(self) -> None:
        """/clear outside a thread sends an ephemeral error."""
        cog = _make_cog()
        interaction = _make_channel_interaction()

        await cog.clear_session.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_clear_does_not_kill_the_window(self) -> None:
        """#803 replaces #123's kill: ``/clear`` is typed, the window stays up."""
        tmux_manager = MagicMock()
        tmux_manager.is_claude_running = MagicMock(return_value=True)
        tmux_manager.send_literal = MagicMock(return_value=True)
        tmux_manager.project_dir_for = MagicMock(return_value=None)
        channel_cog = _make_channel_cog_mock(tmux_manager=tmux_manager)
        cog = _make_cog(channel_cog=channel_cog)
        cog.repo.get = AsyncMock(return_value=MagicMock(closed_at=None))
        cog.repo.reset = AsyncMock(return_value=True)
        interaction = _make_thread_interaction(12345)
        interaction.channel.parent_id = 999

        await cog.clear_session.callback(cog, interaction)

        tmux_manager.send_literal.assert_called_once_with(12345, "/clear")
        tmux_manager.kill_session.assert_not_called()
        cog.repo.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_no_session_sends_ephemeral(self) -> None:
        """/clear when no DB row exists sends an ephemeral 'not found' reply."""
        tmux_manager = MagicMock()
        tmux_manager.kill_session = MagicMock(return_value=False)
        channel_cog = _make_channel_cog_mock(tmux_manager=tmux_manager)
        cog = _make_cog(channel_cog=channel_cog)
        thread_id = 99999

        cog.repo.reset = AsyncMock(return_value=False)

        interaction = _make_thread_interaction(thread_id)
        thread = interaction.channel
        thread.parent_id = 999

        await cog.clear_session.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


class TestCompactCommand:
    """Tests for /compact command — fires the TUI /compact via send_literal (#278).

    The whole point of #278 is that a normal message would go through
    ``send_input``, which prepends a zero-width-space, breaking the leading
    ``/``. ``/compact`` must therefore
    use ``send_literal`` (no ZWSP) + a separate Enter, mirroring the existing
    ``/context`` probe in ``tmux_runner.py``.
    """

    @staticmethod
    def _running_tmux() -> MagicMock:
        tmux_manager = MagicMock()
        tmux_manager.is_claude_running = MagicMock(return_value=True)
        tmux_manager.send_literal = MagicMock(return_value=True)
        tmux_manager.send_keys = MagicMock(return_value=True)
        tmux_manager.send_input = MagicMock(return_value=True)
        return tmux_manager

    @pytest.mark.asyncio
    async def test_compact_outside_thread_sends_ephemeral(self) -> None:
        """/compact outside a thread sends an ephemeral error."""
        cog = _make_cog()
        interaction = _make_channel_interaction()

        await cog.compact_session.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_compact_no_tmux_manager_sends_ephemeral(self) -> None:
        """/compact with no tmux binding sends an ephemeral error."""
        cog = _make_cog()  # channel_cog None → _resolve_tmux_manager returns None
        interaction = _make_thread_interaction(12345)
        interaction.channel.parent_id = 999

        await cog.compact_session.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_compact_not_running_sends_ephemeral(self) -> None:
        """/compact on a stopped, untracked thread sends an ephemeral notice (no send, #806)."""
        tmux_manager = self._running_tmux()
        tmux_manager.is_claude_running = MagicMock(return_value=False)
        channel_cog = _make_channel_cog_mock(tmux_manager=tmux_manager)
        cog = _make_cog(channel_cog=channel_cog)
        interaction = _make_thread_interaction(12345)
        interaction.channel.parent_id = 999

        await cog.compact_session.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
        tmux_manager.send_literal.assert_not_called()

    @pytest.mark.asyncio
    async def test_compact_uses_send_literal_not_send_input(self) -> None:
        """AC6: /compact must use send_literal (no ZWSP) + Enter, never send_input."""
        tmux_manager = self._running_tmux()
        channel_cog = _make_channel_cog_mock(tmux_manager=tmux_manager)
        cog = _make_cog(channel_cog=channel_cog)
        thread_id = 12345
        interaction = _make_thread_interaction(thread_id)
        interaction.channel.parent_id = 999

        await cog.compact_session.callback(cog, interaction)

        tmux_manager.send_literal.assert_called_once_with(thread_id, "/compact")
        tmux_manager.send_keys.assert_called_once_with(thread_id, "Enter")
        tmux_manager.send_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_compact_passes_instructions(self) -> None:
        """AC3: instructions are appended as '/compact <instructions>'."""
        tmux_manager = self._running_tmux()
        channel_cog = _make_channel_cog_mock(tmux_manager=tmux_manager)
        cog = _make_cog(channel_cog=channel_cog)
        thread_id = 12345
        interaction = _make_thread_interaction(thread_id)
        interaction.channel.parent_id = 999

        await cog.compact_session.callback(cog, interaction, instructions="keep open tasks")

        tmux_manager.send_literal.assert_called_once_with(thread_id, "/compact keep open tasks")

    @pytest.mark.asyncio
    async def test_compact_text_twin_uses_send_literal(self) -> None:
        """The !compact text twin fires the same send_literal path."""
        tmux_manager = self._running_tmux()
        channel_cog = _make_channel_cog_mock(tmux_manager=tmux_manager)
        cog = _make_cog(channel_cog=channel_cog)
        thread_id = 12345

        ctx = MagicMock()
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.parent_id = 999
        ctx.channel = thread
        ctx.send = AsyncMock()

        await cog.compact_text.callback(cog, ctx)

        tmux_manager.send_literal.assert_called_once_with(thread_id, "/compact")


class TestApplyThreadNamingRetitle:
    """Unit tests for the auto-retitle hook in _apply_thread_naming (#121)."""

    def _make_cog_with_repo(self, record):
        """Return (cog, thread, tmux_manager) with a pre-configured repo record."""

        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=record)
        cog.repo.set_topic = AsyncMock()
        cog.repo.set_tmux_window_id = AsyncMock()
        cog.repo.set_issue_ref = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 55555
        thread.name = "🟢 W1 │ 認証リファクタ"

        tmux_manager = MagicMock()
        tmux_manager.get_window_info = MagicMock(return_value=("@1", 1))

        return cog, thread, tmux_manager

    def _make_record(
        self, *, topic, locked=0, state="running", issue_ref=None, origin_issue_ref=None
    ):
        record = MagicMock()
        record.topic = topic
        record.auto_topic_locked = locked
        record.state = state
        record.tmux_window_id = "@1"
        record.issue_ref = issue_ref
        record.origin_issue_ref = origin_issue_ref
        return record

    @pytest.mark.asyncio
    async def test_retitle_updates_topic_and_renames_when_changed(self):
        """When maybe_retitle returns a new topic, DB is updated and thread renamed."""
        from unittest.mock import patch

        from c_lord.cogs import claude_chat as cc_module

        record = self._make_record(topic="認証リファクタ")
        cog, thread, tmux = self._make_cog_with_repo(record)
        cog._thread_retitle = True  # #414: retitle is opt-in; enable for this path
        thread.name = "🟢 W1 │ 認証リファクタ"

        instruction = "次はDockerfileを最適化してCI/CDを改善してください"

        with patch.object(
            cc_module.topic_module,
            "maybe_retitle",
            new=AsyncMock(return_value="Dockerfileの最適化"),
        ):
            await cog._apply_thread_naming(
                thread=thread, tmux_manager=tmux, first_message=instruction
            )

        cog.repo.set_topic.assert_awaited_once_with(
            55555, "Dockerfileの最適化", source="llm_retitle"
        )
        thread.edit.assert_awaited_once()
        call_name = thread.edit.await_args.kwargs.get(
            "name", thread.edit.await_args.args[0] if thread.edit.await_args.args else ""
        )
        assert "Dockerfileの最適化" in call_name

    @pytest.mark.asyncio
    async def test_retitle_skips_rename_when_verbatim(self):
        """When maybe_retitle returns None (verbatim), DB and rename are not called."""
        from unittest.mock import patch

        from c_lord.cogs import claude_chat as cc_module

        record = self._make_record(topic="認証リファクタ")
        cog, thread, tmux = self._make_cog_with_repo(record)
        thread.name = "🟢 W1 │ 認証リファクタ"

        instruction = "認証リファクタを引き続きお願いします"

        with patch.object(
            cc_module.topic_module, "maybe_retitle", new=AsyncMock(return_value=None)
        ):
            await cog._apply_thread_naming(
                thread=thread, tmux_manager=tmux, first_message=instruction
            )

        cog.repo.set_topic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_per_turn_rename_when_name_unchanged(self, monkeypatch):
        """#246: a continuing turn where the topic already matches the current
        thread name issues NO rename. The per-turn 🟢/🟡 lamp is now the
        StatusManager reaction; the thread name is only repainted by the
        state-sync poll, so claude_chat must not PATCH the thread every turn
        (that #236 churn saturated Discord's rename rate-limit, #241).

        #329: with the thread-name lamp off by default, the unchanged name has
        no leading status emoji (just ``W<N> │ <topic>``)."""
        from unittest.mock import patch

        from c_lord.cogs import claude_chat as cc_module

        monkeypatch.delenv("CLORD_THREAD_LAMP", raising=False)  # default: lamp off

        # Thread name already reflects the topic for the current state (lamp off
        # → no leading emoji).
        record = self._make_record(topic="認証リファクタ", state="running")
        cog, thread, tmux = self._make_cog_with_repo(record)
        thread.name = "W1 │ 認証リファクタ"

        with patch.object(
            cc_module.topic_module, "maybe_retitle", new=AsyncMock(return_value=None)
        ):
            await cog._apply_thread_naming(
                thread=thread, tmux_manager=tmux, first_message="続きをお願いします"
            )

        thread.edit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_lamp_state_removed(self):
        """#246: the per-turn lamp-rename helper is gone (replaced by reactions).

        Guards against re-introducing a turn-boundary ``thread.edit`` that would
        bring the 429 storm back.
        """
        cog = _make_cog()
        assert not hasattr(cog, "_set_lamp_state")

    @pytest.mark.asyncio
    async def test_retitle_skips_when_locked(self):
        """When auto_topic_locked=1, maybe_retitle must not be called."""
        from unittest.mock import patch

        from c_lord.cogs import claude_chat as cc_module

        record = self._make_record(topic="認証リファクタ", locked=1)
        cog, thread, tmux = self._make_cog_with_repo(record)

        with patch.object(
            cc_module.topic_module, "maybe_retitle", new=AsyncMock(return_value="new")
        ) as mock_retitle:
            await cog._apply_thread_naming(
                thread=thread, tmux_manager=tmux, first_message="何か作業してください"
            )

        mock_retitle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retitle_skips_when_no_existing_topic(self):
        """First message (no existing topic) should use initial generation, not retitle."""
        from unittest.mock import patch

        from c_lord.cogs import claude_chat as cc_module

        record = self._make_record(topic=None)
        cog, thread, tmux = self._make_cog_with_repo(record)

        with (
            patch.object(cc_module.topic_module, "maybe_retitle", new=AsyncMock()) as mock_retitle,
            patch.object(
                cc_module.topic_module,
                "generate_topic",
                new=AsyncMock(return_value=("初回トピック", "llm")),
            ),
        ):
            await cog._apply_thread_naming(
                thread=thread, tmux_manager=tmux, first_message="初回メッセージです"
            )

        mock_retitle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retitle_disabled_by_default(self):
        """#414: maybe_retitle is NOT called when thread_retitle is off (default)."""
        from unittest.mock import patch

        from c_lord.cogs import claude_chat as cc_module

        record = self._make_record(topic="認証リファクタ")
        cog, thread, tmux = self._make_cog_with_repo(record)
        assert cog._thread_retitle is False  # default off (#414)

        with patch.object(
            cc_module.topic_module, "maybe_retitle", new=AsyncMock(return_value="別タイトル")
        ) as mock_retitle:
            await cog._apply_thread_naming(
                thread=thread,
                tmux_manager=tmux,
                first_message="次はDockerを最適化してCIを改善してください",
            )

        mock_retitle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rename_failure_is_logged_not_silent(self, caplog):
        """#423: a swallowed thread.edit failure must surface in the log (not silent).

        Previously the rename was wrapped in ``contextlib.suppress`` so a stuck
        title (e.g. the monitoring thread) left no trace of *why* it failed.
        """
        import logging

        record = self._make_record(topic="挨拶")
        cog, thread, tmux = self._make_cog_with_repo(record)
        thread.name = "monitoring"  # differs from computed 'W1 │ 挨拶' → edit fires

        resp = MagicMock()
        resp.status = 403
        resp.reason = "Forbidden"
        thread.edit = AsyncMock(side_effect=discord.HTTPException(resp, "Missing Permissions"))

        with caplog.at_level(logging.WARNING):
            # Must NOT propagate — the response path keeps working.
            await cog._apply_thread_naming(thread=thread, tmux_manager=tmux, first_message="hi")

        assert any("rename failed" in r.getMessage() for r in caplog.records), caplog.text

    @pytest.mark.asyncio
    async def test_rename_403_posts_hint_once(self):
        """#429: a 403 (Missing Access) posts a one-time Manage-Threads hint."""
        record = self._make_record(topic="挨拶")
        cog, thread, tmux = self._make_cog_with_repo(record)
        thread.name = "monitoring"  # differs → edit fires
        thread.send = AsyncMock()
        resp = MagicMock()
        resp.status = 403
        resp.reason = "Forbidden"
        thread.edit = AsyncMock(side_effect=discord.Forbidden(resp, "Missing Access"))

        await cog._apply_thread_naming(thread=thread, tmux_manager=tmux, first_message="hi")
        assert thread.send.await_count == 1
        sent = thread.send.await_args.args[0] if thread.send.await_args.args else ""
        assert "Manage Threads" in sent or "スレッドの管理" in sent

        # Second 403 on the same thread must NOT re-post (one hint per process).
        await cog._apply_thread_naming(thread=thread, tmux_manager=tmux, first_message="hi again")
        assert thread.send.await_count == 1

    @pytest.mark.asyncio
    async def test_rename_timeout_does_not_post_hint(self):
        """#429: only 403 (permission) gets a hint — timeouts are not user-actionable."""
        record = self._make_record(topic="挨拶")
        cog, thread, tmux = self._make_cog_with_repo(record)
        thread.name = "monitoring"
        thread.send = AsyncMock()
        thread.edit = AsyncMock(side_effect=TimeoutError())

        await cog._apply_thread_naming(thread=thread, tmux_manager=tmux, first_message="hi")
        thread.send.assert_not_awaited()


class TestApplyThreadNamingIssueRef:
    """#414: the Issue/PR number is auto-detected from the branch / first message."""

    def _make_cog_with_repo(self, record):
        cog = _make_cog()
        cog.repo.get = AsyncMock(return_value=record)
        cog.repo.set_topic = AsyncMock()
        cog.repo.set_tmux_window_id = AsyncMock()
        cog.repo.set_issue_ref = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 55555
        thread.name = "W1 │ 認証リファクタ"

        tmux_manager = MagicMock()
        tmux_manager.get_window_info = MagicMock(return_value=("@1", 1))
        return cog, thread, tmux_manager

    def _make_record(self, *, topic="認証リファクタ", issue_ref=None, origin_issue_ref=None):
        record = MagicMock()
        record.topic = topic
        record.auto_topic_locked = 0
        record.state = "running"
        record.tmux_window_id = "@1"
        record.issue_ref = issue_ref
        record.origin_issue_ref = origin_issue_ref
        return record

    @pytest.mark.asyncio
    async def test_branch_number_persisted_and_shown_in_name(self):
        """A branch like fix/404-x yields #404 — persisted and rendered."""
        from unittest.mock import patch

        record = self._make_record(issue_ref=None)
        cog, thread, tmux = self._make_cog_with_repo(record)

        with patch.object(cog, "_git_current_branch", return_value="fix/404-add-thing"):
            await cog._apply_thread_naming(
                thread=thread,
                tmux_manager=tmux,
                first_message="続きをお願いします",
                working_dir="/tmp/clone",
            )

        cog.repo.set_issue_ref.assert_awaited_once_with(55555, "404")
        thread.edit.assert_awaited_once()
        name = thread.edit.await_args.kwargs.get("name", "")
        assert "#404" in name

    @pytest.mark.asyncio
    async def test_first_message_number_used_when_branch_has_none(self):
        """No branch number → the FIRST message's #NNN is used (#414/#428)."""
        from unittest.mock import patch

        from c_lord.cogs import claude_chat as cc_module

        # First message = no topic yet (topic gets generated this turn).
        record = self._make_record(topic=None, issue_ref=None)
        cog, thread, tmux = self._make_cog_with_repo(record)

        with (
            patch.object(cog, "_git_current_branch", return_value="main"),
            patch.object(
                cc_module.topic_module,
                "generate_topic",
                new=AsyncMock(return_value=("トピック", "llm")),
            ),
        ):
            await cog._apply_thread_naming(
                thread=thread,
                tmux_manager=tmux,
                first_message="#512 を直してください",
                working_dir="/tmp/clone",
            )

        cog.repo.set_issue_ref.assert_awaited_once_with(55555, "512")

    @pytest.mark.asyncio
    async def test_later_message_hashref_not_captured(self):
        """#428: a #NNN in a LATER message (topic already set) must NOT be captured.

        Text detection is first-message-only; otherwise a casual '#1' mid-thread
        sticks forever (the monitoring-thread false positive).
        """
        from unittest.mock import patch

        record = self._make_record(topic="既存トピック", issue_ref=None)
        cog, thread, tmux = self._make_cog_with_repo(record)

        with patch.object(cog, "_git_current_branch", return_value="main"):
            await cog._apply_thread_naming(
                thread=thread,
                tmux_manager=tmux,
                first_message="ついでに #999 も見ておいて",
                working_dir="/tmp/clone",
            )

        cog.repo.set_issue_ref.assert_not_awaited()  # #999 ignored (not first message)

    @pytest.mark.asyncio
    async def test_known_number_not_overwritten_by_casual_mention(self):
        """An already-known number is kept; a later #NNN in text does not replace it."""
        from unittest.mock import patch

        record = self._make_record(issue_ref="404")
        cog, thread, tmux = self._make_cog_with_repo(record)

        with patch.object(cog, "_git_current_branch", return_value="main"):
            await cog._apply_thread_naming(
                thread=thread,
                tmux_manager=tmux,
                first_message="ついでに #999 も見て",
                working_dir="/tmp/clone",
            )

        cog.repo.set_issue_ref.assert_not_awaited()  # #404 stays

    def test_git_current_branch_missing_repo_returns_none(self):
        """_git_current_branch never raises on a non-repo path."""
        cog = _make_cog()
        assert cog._git_current_branch("/nonexistent/path/xyz") is None


class TestThreadLampDisabled:
    """#329: thread-name status lamp (🟢🟡) is off by default.

    Renaming the thread to repaint the leading emoji saturates Discord's
    thread-rename rate-limit, so by default ``_apply_thread_naming`` keeps the
    topic but emits no status emoji (and the state-sync poll is skipped — see
    setup.py). Opt back in with ``CLORD_THREAD_LAMP=1``.
    """

    def _make_cog_and_thread(self, *, topic: str | None = "認証リファクタ"):
        cog = _make_cog()
        record = MagicMock()
        record.topic = topic
        record.state = "running"
        record.auto_topic_locked = 0
        record.tmux_window_id = "@1"
        cog.repo.get = AsyncMock(return_value=record)
        cog.repo.set_state = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 55555
        thread.name = "認証リファクタ"  # no lamp emoji

        tmux = MagicMock()
        tmux.get_window_info = MagicMock(return_value=("@1", 1))
        return cog, thread, tmux

    @pytest.mark.asyncio
    async def test_apply_thread_naming_omits_lamp_emoji_by_default(self, monkeypatch):
        """Default: topic naming still happens, but the name carries no status lamp."""
        from unittest.mock import patch

        from c_lord.cogs import claude_chat as cc_module
        from c_lord.thread_name import STATUS_EMOJI

        monkeypatch.delenv("CLORD_THREAD_LAMP", raising=False)
        # #705: the initial LLM summary is opt-in now. This test is about the
        # lamp, so opt in and keep exercising the "topic applied" half.
        monkeypatch.setenv("CLORD_AUTO_TOPIC", "1")
        cog, thread, tmux = self._make_cog_and_thread(topic=None)
        cog.repo.set_topic = AsyncMock()
        thread.name = "old"

        with patch.object(
            cc_module.topic_module,
            "generate_topic",
            new=AsyncMock(return_value=("初回トピック", "llm")),
        ):
            await cog._apply_thread_naming(
                thread=thread, tmux_manager=tmux, first_message="初回メッセージです"
            )

        thread.edit.assert_awaited_once()
        call_name = thread.edit.await_args.kwargs.get(
            "name", thread.edit.await_args.args[0] if thread.edit.await_args.args else ""
        )
        assert "初回トピック" in call_name  # topic still applied
        for emoji in STATUS_EMOJI.values():
            assert emoji not in call_name  # no lamp emoji

    @pytest.mark.asyncio
    async def test_apply_thread_naming_paints_lamp_when_opted_in(self, monkeypatch):
        """CLORD_THREAD_LAMP=1 restores the leading status emoji (backward compat)."""
        from unittest.mock import patch

        from c_lord.cogs import claude_chat as cc_module

        monkeypatch.setenv("CLORD_THREAD_LAMP", "1")
        monkeypatch.setenv("CLORD_AUTO_TOPIC", "1")  # #705 — see the test above
        cog, thread, tmux = self._make_cog_and_thread(topic=None)
        cog.repo.set_topic = AsyncMock()
        thread.name = "old"

        with patch.object(
            cc_module.topic_module,
            "generate_topic",
            new=AsyncMock(return_value=("初回トピック", "llm")),
        ):
            await cog._apply_thread_naming(
                thread=thread, tmux_manager=tmux, first_message="初回メッセージです"
            )

        thread.edit.assert_awaited_once()
        call_name = thread.edit.await_args.kwargs.get(
            "name", thread.edit.await_args.args[0] if thread.edit.await_args.args else ""
        )
        assert "初回トピック" in call_name
        assert call_name.startswith("🟢")  # running lamp painted


class TestDiscordLinkEnrichmentWireIn:
    """#318: pasted Discord links are resolved into the prompt that reaches Claude."""

    @staticmethod
    def _channel_with_message(content: str, *, name: str = "support") -> MagicMock:
        referenced = MagicMock()
        referenced.content = content
        referenced.author = MagicMock(display_name="bob", name="bob")
        referenced.created_at = "2026-06-05T00:00:00"
        channel = MagicMock()
        channel.name = name
        channel.fetch_message = AsyncMock(return_value=referenced)
        return channel

    @pytest.mark.asyncio
    async def test_thread_reply_inlines_referenced_message(self) -> None:
        cog = _make_cog()
        cog.bot.get_channel = MagicMock(
            return_value=self._channel_with_message("the linked content")
        )
        cog._run_claude = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.parent_id = 999
        thread.send = AsyncMock()
        msg = MagicMock(spec=discord.Message)
        msg.id = 1
        msg.channel = thread
        msg.reference = None
        msg.content = "look 👉 https://discord.com/channels/111/222/333"
        msg.attachments = []

        await cog._handle_thread_reply(msg)
        # The run is now dispatched as its own task off the lock (#315) — await it
        # so the enriched prompt actually reaches _run_claude.
        spawned = cog._active_tasks.get(42)
        assert spawned is not None
        await asyncio.wait_for(spawned, timeout=1.0)

        cog._run_claude.assert_awaited_once()
        prompt_arg = cog._run_claude.await_args.args[2]
        assert "the linked content" in prompt_arg  # enriched
        assert "look 👉" in prompt_arg  # original preserved

    @pytest.mark.asyncio
    async def test_new_conversation_inlines_referenced_message(self) -> None:
        cog = _make_cog()
        cog.bot.get_channel = MagicMock(
            return_value=self._channel_with_message("fresh-thread linked content")
        )
        cog._run_claude = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 7
        msg = MagicMock(spec=discord.Message)
        msg.id = 2
        msg.reference = None
        msg.content = "kick off https://discord.com/channels/111/222/333"
        msg.attachments = []
        msg.create_thread = AsyncMock(return_value=thread)

        await cog._handle_new_conversation(msg)

        cog._run_claude.assert_awaited_once()
        prompt_arg = cog._run_claude.await_args.args[2]
        assert "fresh-thread linked content" in prompt_arg

    @pytest.mark.asyncio
    async def test_spawn_session_seed_is_not_enriched(self) -> None:
        """Programmatic spawn (seed message) must NOT be enriched — only human turns are."""
        cog = _make_cog()
        # A channel that WOULD enrich if enrichment were (wrongly) called on the seed.
        cog.bot.get_channel = MagicMock(return_value=self._channel_with_message("LEAKED"))
        cog._run_claude = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 5
        thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 999
        channel.create_thread = AsyncMock(return_value=thread)
        prompt = "do it https://discord.com/channels/111/222/333"
        await cog.spawn_session(channel, prompt)
        await asyncio.sleep(0)  # let the create_task(_run_claude) run

        cog._run_claude.assert_awaited_once()
        prompt_arg = cog._run_claude.await_args.args[2]
        assert prompt_arg == prompt  # passed through unchanged
        assert "LEAKED" not in prompt_arg


class TestOnReadyLampRecovery:
    """#718: a turn in flight at shutdown gets its lamp taken back on startup."""

    @pytest.fixture(autouse=True)
    def _tmux_cannot_be_asked(self, monkeypatch):
        """Never read the host's real tmux: "could not tell" keeps the watch (#992)."""
        monkeypatch.setattr("c_lord.cogs.claude_chat.live_claude_panes", lambda: None)

    def _cog(self, record, thread):
        from c_lord.database.resume_repo import PendingResume, PendingResumeRepository

        pending = PendingResume(
            id=3,
            thread_id=thread.id,
            session_id="sess",
            reason="bot_shutdown",
            resume_prompt=None,
            created_at="2026-09-30 10:00:00",
        )
        resume_repo = MagicMock(spec=PendingResumeRepository)
        resume_repo.get_pending = AsyncMock(return_value=[pending])
        resume_repo.delete = AsyncMock()
        repo = MagicMock()
        repo.get = AsyncMock(return_value=record)
        bot = MagicMock()
        bot.get_channel = MagicMock(return_value=thread)
        return ClaudeChatCog(bot=bot, repo=repo, runner=MagicMock(), resume_repo=resume_repo)

    def _thread(self, thread_id: int = 7188):
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.send = AsyncMock()
        thread.parent = MagicMock(spec=discord.TextChannel)
        return thread

    @pytest.mark.asyncio
    async def test_the_trigger_messages_lamp_is_adopted(self, tmp_path) -> None:
        from unittest.mock import patch

        thread = self._thread()
        trigger = MagicMock()
        thread.fetch_message = AsyncMock(return_value=trigger)
        record = MagicMock(trigger_message_id=4242, working_dir=str(tmp_path))
        cog = self._cog(record, thread)

        adopt = AsyncMock()
        with patch("c_lord.cogs.claude_chat.adopt_orphaned_lamp", adopt):
            await cog.on_ready()
            await asyncio.gather(*cog._lamp_recovery_tasks)

        thread.fetch_message.assert_awaited_once_with(4242)
        adopt.assert_awaited_once()
        assert adopt.await_args.args[0] is trigger
        assert adopt.await_args.kwargs["thread_id"] == thread.id
        # With no transcript on disk the turn is not running.
        assert await adopt.await_args.kwargs["turn_running"]() is False

    @pytest.mark.asyncio
    async def test_a_thread_starter_trigger_is_read_from_the_parent(self, tmp_path) -> None:
        """A thread's first message lives in the parent channel (id == thread id)."""
        from unittest.mock import patch

        thread = self._thread(7189)
        starter = MagicMock()
        thread.fetch_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
        )
        thread.parent.fetch_message = AsyncMock(return_value=starter)
        record = MagicMock(trigger_message_id=7189, working_dir=str(tmp_path))
        cog = self._cog(record, thread)

        adopt = AsyncMock()
        with patch("c_lord.cogs.claude_chat.adopt_orphaned_lamp", adopt):
            await cog.on_ready()
            await asyncio.gather(*cog._lamp_recovery_tasks)

        assert adopt.await_args.args[0] is starter

    @staticmethod
    def _history(*messages):
        async def history(limit: int = 100):
            for m in messages:
                yield m

        return history

    @staticmethod
    def _msg(*lamps: str, me: bool = True):
        from types import SimpleNamespace

        m = MagicMock()
        m.reactions = [SimpleNamespace(emoji=e, me=me) for e in lamps]
        return m

    @pytest.mark.asyncio
    async def test_an_adoption_is_tracked_until_the_lamp_is_final(self, tmp_path) -> None:
        """#870 A: while the watcher runs, the next shutdown can see it."""
        from unittest.mock import patch

        thread = self._thread(7193)
        trigger = MagicMock(id=4242)
        thread.fetch_message = AsyncMock(return_value=trigger)
        record = MagicMock(trigger_message_id=4242, working_dir=str(tmp_path))
        cog = self._cog(record, thread)

        seen: dict = {}

        async def adopt(message, *, thread_id, turn_running, **_):
            seen.update(cog._adopted_lamps)

        with patch("c_lord.cogs.claude_chat.adopt_orphaned_lamp", adopt):
            await cog.on_ready()
            await asyncio.gather(*cog._lamp_recovery_tasks)

        assert seen == {7193: 4242}
        assert cog._adopted_lamps == {}

    @pytest.mark.asyncio
    async def test_a_first_turn_with_no_recorded_trigger_is_found_in_history(self) -> None:
        """A thread's first turn never gets ``trigger_message_id`` (the row does
        not exist yet when it is written) — its orphaned ⚠️ was still left on
        the seed message in production after #850. Newest-first history is
        searched for the bot's own open lamp instead."""
        from unittest.mock import patch

        thread = self._thread(7190)
        thread.fetch_message = AsyncMock()
        orphan = self._msg("⚠️")
        thread.history = self._history(self._msg(), orphan, self._msg("🟡"))
        cog = self._cog(MagicMock(trigger_message_id=None, working_dir="/x"), thread)

        adopt = AsyncMock()
        with patch("c_lord.cogs.claude_chat.adopt_orphaned_lamp", adopt):
            await cog.on_ready()
            await asyncio.gather(*cog._lamp_recovery_tasks)

        thread.fetch_message.assert_not_awaited()
        assert adopt.await_args.args[0] is orphan

    @pytest.mark.asyncio
    async def test_history_search_stops_at_the_newest_final_lamp(self) -> None:
        """An old ⚠️ above a 🟡 belongs to a turn that is long over — and to
        some other process's story; the newest lamp decides."""
        from unittest.mock import patch

        thread = self._thread(7191)
        thread.history = self._history(self._msg("🟡"), self._msg("⚠️"))
        cog = self._cog(MagicMock(trigger_message_id=None, working_dir="/x"), thread)

        adopt = AsyncMock()
        with patch("c_lord.cogs.claude_chat.adopt_orphaned_lamp", adopt):
            await cog.on_ready()
            await asyncio.gather(*cog._lamp_recovery_tasks)

        adopt.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_working_dir_means_nothing_to_adopt(self) -> None:
        from unittest.mock import patch

        thread = self._thread(7192)
        thread.fetch_message = AsyncMock()
        cog = self._cog(MagicMock(trigger_message_id=5, working_dir=None), thread)

        adopt = AsyncMock()
        with patch("c_lord.cogs.claude_chat.adopt_orphaned_lamp", adopt):
            await cog.on_ready()
            await asyncio.gather(*cog._lamp_recovery_tasks)

        adopt.assert_not_awaited()


class TestTurnEndAfterRestart:
    """#907: a turn that crossed a restart still closes with 📊 and 🟡 @requester.

    The run that would have posted them died with the old process
    (``outcome=cancelled``); the new process only adopts the lamp (#718). The
    lamp watcher is what sees the turn end, so it hands the closing on.
    """

    def _setup(self, tmp_path, *, author_bot: bool = False):
        thread = TestOnReadyLampRecovery()._thread(9075)
        trigger = MagicMock(id=4242)
        trigger.content = "60秒待ってから一言返して"
        trigger.author = MagicMock(id=777, bot=author_bot)
        thread.fetch_message = AsyncMock(return_value=trigger)
        record = MagicMock(
            trigger_message_id=4242, working_dir=str(tmp_path), session_id="sess-907"
        )
        cog = TestOnReadyLampRecovery()._cog(record, thread)
        dashboard = MagicMock()
        order: list[str] = []

        async def set_state(*args, **kwargs):
            order.append("summons")

        dashboard.set_state = AsyncMock(side_effect=set_state)
        cog._dashboard = dashboard

        async def footer(config, session_id):
            order.append("footer")

        return cog, thread, dashboard, order, footer

    async def _run(self, cog, adopt, footer) -> None:
        from unittest.mock import patch

        with (
            patch("c_lord.cogs.claude_chat.adopt_orphaned_lamp", adopt),
            patch("c_lord.cogs.claude_chat._post_context_usage", footer),
            patch("c_lord.cogs.claude_chat.RECOVERED_TURN_END_GRACE_SECONDS", 0),
        ):
            await cog.on_ready()
            await asyncio.gather(*cog._lamp_recovery_tasks)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how", ["ended", "over"])
    async def test_footer_then_summons_once(self, tmp_path, how) -> None:
        """AC1/AC2/AC3. RED before #907: neither is posted."""
        from c_lord.discord_ui.thread_dashboard import ThreadState

        cog, thread, dashboard, order, footer = self._setup(tmp_path)
        await self._run(cog, AsyncMock(return_value=how), footer)

        assert order == ["footer", "summons"]
        call = dashboard.set_state.await_args
        assert call.args[1] == ThreadState.WAITING_INPUT
        assert call.kwargs["thread"] is thread
        assert call.kwargs["notify_user_id"] == 777

    @pytest.mark.asyncio
    async def test_nothing_when_a_newer_turn_took_over(self, tmp_path) -> None:
        cog, _thread, dashboard, order, footer = self._setup(tmp_path)
        await self._run(cog, AsyncMock(return_value=None), footer)
        assert order == []

    @pytest.mark.asyncio
    async def test_nothing_when_a_turn_is_running_in_this_process(self, tmp_path) -> None:
        """The startup pick-up (#776) may already have started the next turn."""
        cog, thread, dashboard, order, footer = self._setup(tmp_path)

        async def adopt(message, *, thread_id, turn_running, **_):
            cog._active_tasks[thread_id] = MagicMock()
            return "over"

        await self._run(cog, adopt, footer)
        assert order == []

    @pytest.mark.asyncio
    async def test_webhook_turn_follows_the_owner_fallback(self, tmp_path) -> None:
        """AC4: same rule as a turn that ended in this process (``_notify_target``)."""
        from unittest.mock import patch

        cog, _thread, dashboard, order, footer = self._setup(tmp_path, author_bot=True)
        with patch("c_lord.cogs.claude_chat.owner_notify_id", return_value=None) as owner:
            await self._run(cog, AsyncMock(return_value="ended"), footer)
        owner.assert_called_with(cog.bot, kind="completion")
        assert dashboard.set_state.await_args.kwargs["notify_user_id"] is None


class TestOneStopPath:
    """#878: ⏹ Stop, /stop and !stop all go through ``_stop_thread_work``."""

    @pytest.mark.asyncio
    async def test_text_and_slash_stop_use_the_shared_core(self) -> None:
        from c_lord.session_stop import StopOutcome, StopResult

        cog = _make_cog()
        cog._stop_thread_work = AsyncMock(return_value=StopResult(StopOutcome.STOPPED))  # type: ignore[method-assign]
        ctx = _make_thread_ctx(thread_id=12345)
        await cog.stop_text.callback(cog, ctx)
        interaction = _make_thread_interaction(12345)
        await cog.stop_session.callback(cog, interaction)
        assert cog._stop_thread_work.await_count == 2

    @pytest.mark.asyncio
    async def test_stop_button_is_wired_to_the_shared_core(self) -> None:
        from c_lord.session_stop import StopOutcome, StopResult

        cog = _make_cog()
        cog._stop_thread_work = AsyncMock(return_value=StopResult(StopOutcome.STOPPED))  # type: ignore[method-assign]
        thread = MagicMock(spec=discord.Thread)
        thread.id = 12345
        view = cog._make_stop_view(MagicMock(), thread)

        btn = view.stop_button
        interaction = MagicMock(spec=discord.Interaction)
        interaction.response = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        await btn.callback(interaction)

        cog._stop_thread_work.assert_awaited_once_with(thread)

    @pytest.mark.asyncio
    async def test_self_started_turn_without_runner_is_stopped(self) -> None:
        """A turn woken by a task notification has no runner but must still stop."""
        tmux = MagicMock()
        tmux.capture_pane = MagicMock(return_value="✶ Infusing… (4s · ↓ 140 tokens)\n❯ \n")
        tmux.is_claude_running = MagicMock(return_value=True)
        tmux.send_interrupt = MagicMock(return_value=True)
        tmux.stop_tool_shells = MagicMock(return_value=0)
        cog = _make_cog(channel_cog=_make_channel_cog_mock(tmux_manager=tmux))
        cog._start_wakeup_guard = MagicMock()  # type: ignore[method-assign]
        ctx = _make_thread_ctx(thread_id=12345, parent_id=999)

        await cog.stop_text.callback(cog, ctx)

        tmux.send_interrupt.assert_called_once_with(12345)
        embed = ctx.send.call_args.kwargs.get("embed")
        assert embed is not None and "stopped" in embed.title.lower()

    @pytest.mark.asyncio
    async def test_unauthorized_stop_touches_nothing(self) -> None:
        cog = _make_cog()
        cog._authorizer = Authorizer(allowed_user_ids={42})
        cog._stop_thread_work = AsyncMock()  # type: ignore[method-assign]
        ctx = _make_thread_ctx(thread_id=12345)
        await cog.stop_text.callback(cog, ctx)
        cog._stop_thread_work.assert_not_called()


class TestRestartCrossedTurnReadsHowItEnded:
    """#978: the closing a restart hands on follows how the turn ended.

    ``_close_recovered_turn`` always said "Claude has finished" and sent no
    turn-end event: a turn that stopped on an API error or a plan limit was
    summoned as finished, and a dispatcher waiting for the turn's event waited
    forever (the old process's run ended ``cancelled``, which announces nothing).
    """

    async def _close(self, tmp_path, monkeypatch, ending):
        import json
        from unittest.mock import patch

        from c_lord import turn_events

        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("CLORD_EVENT_LOG", str(log))
        turn_events.reset()
        cog, thread, dashboard, order, footer = TestTurnEndAfterRestart()._setup(tmp_path)
        thread.guild = None
        thread.parent_id = 9000
        trigger = thread.fetch_message.return_value
        trigger.webhook_id = None
        trigger.created_at = None
        with patch("c_lord.cogs.claude_chat.recovered_turn_ending", return_value=ending, create=True):
            await TestTurnEndAfterRestart()._run(cog, AsyncMock(return_value="ended"), footer)
        with patch.object(turn_events, "_SETTLE_MAX_SECONDS", 0):
            await turn_events.drain()
        events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        turn_events.reset()
        return dashboard, events

    @pytest.mark.asyncio
    async def test_an_api_error_is_not_called_finished(self, tmp_path, monkeypatch) -> None:
        """AC1. RED: ``api_error`` was never passed."""
        from c_lord.host_restart_notice import TurnEnding

        dashboard, events = await self._close(
            tmp_path, monkeypatch, TurnEnding(api_error="API Error: 500 Internal server error")
        )
        kwargs = dashboard.set_state.await_args.kwargs
        assert kwargs["api_error"]
        assert [e["kind"] for e in events] == ["failure"]
        assert events[0]["reason"] == "api_error"

    @pytest.mark.asyncio
    async def test_a_plan_limit_says_so(self, tmp_path, monkeypatch) -> None:
        from c_lord.claude.types import UsageLimit
        from c_lord.host_restart_notice import TurnEnding

        limit = UsageLimit(scope="weekly limit", resets_at="3am", line="You've hit your limit")
        dashboard, events = await self._close(tmp_path, monkeypatch, TurnEnding(usage_limit=limit))
        kwargs = dashboard.set_state.await_args.kwargs
        assert kwargs["usage_limit"] == limit and kwargs["no_response"] is True
        assert [(e["kind"], e["reason"]) for e in events] == [("failure", "usage_limit")]

    @pytest.mark.asyncio
    async def test_a_plain_end_sends_one_completion(self, tmp_path, monkeypatch) -> None:
        """AC2. RED: no event at all."""
        from c_lord.host_restart_notice import TurnEnding

        dashboard, events = await self._close(tmp_path, monkeypatch, TurnEnding())
        kwargs = dashboard.set_state.await_args.kwargs
        assert kwargs.get("api_error") is None and kwargs.get("usage_limit") is None
        assert len(events) == 1
        event = events[0]
        assert event["kind"] == "completion" and event["reason"] == "restart_crossed"
        assert event["started_by"] == {"type": "user", "id": "777", "name": None}


class TestAdoptedLampOfADeadTurn(TestOnReadyLampRecovery):
    """#992 review: on_ready's adopted watch lets go once claude is gone from the pane.

    Otherwise the thread stays in ``_adopted_lamps``, is recorded again at every
    shutdown (#870) and re-adopted — with a 🔄 line — at every start, for good.
    """

    async def _adopt(self, tmp_path, live) -> dict:
        import inspect

        from c_lord.discord_ui import lamp_recovery
        from c_lord.discord_ui.status import EMOJI_RUNNING
        from tests.test_lamp_recovery import _Message

        real = lamp_recovery.adopt_orphaned_lamp

        async def fast_adopt(message, *, thread_id, turn_running, **kwargs):
            kw = dict(poll_seconds=0.01, recheck_seconds=0.02, max_seconds=0.05)
            if "slow_poll_seconds" in inspect.signature(real).parameters:
                kw.update(slow_poll_seconds=0.01, slow_recheck_seconds=0.02)
            return await real(
                message, thread_id=thread_id, turn_running=turn_running, **kwargs, **kw
            )

        thread = self._thread(9920)
        trigger = _Message(EMOJI_RUNNING)
        trigger.id = 4242
        thread.fetch_message = AsyncMock(return_value=trigger)
        record = MagicMock(trigger_message_id=4242, working_dir=str(tmp_path))
        cog = self._cog(record, thread)
        with (
            patch("c_lord.cogs.claude_chat.adopt_orphaned_lamp", fast_adopt),
            # the transcript ends on a tool_use: claude was killed mid-turn
            patch("c_lord.cogs.claude_chat.stopped_mid_turn", return_value=True),
            patch("c_lord.cogs.claude_chat.live_claude_panes", return_value=live),
        ):
            await cog.on_ready()
            await asyncio.sleep(0.5)  # 10x max_seconds
            still = dict(cog._adopted_lamps)
            for t in list(cog._lamp_recovery_tasks):
                t.cancel()
            await asyncio.gather(*cog._lamp_recovery_tasks, return_exceptions=True)
        return still

    @pytest.mark.asyncio
    async def test_a_dead_mid_turn_thread_is_not_carried_into_the_next_restart(
        self, tmp_path
    ) -> None:
        from c_lord.tmux import LiveClaude

        assert await self._adopt(tmp_path, LiveClaude(frozenset(), frozenset())) == {}

    @pytest.mark.asyncio
    async def test_a_pane_that_cannot_be_read_keeps_the_watch(self, tmp_path) -> None:
        """#510's asymmetry: "could not tell" is never read as "it died"."""
        assert await self._adopt(tmp_path, None) == {9920: 4242}


class TestSessionRunningNotice:
    """The line a turn posts its ⏹ Stop button on.

    The startup sweep (#634) recognises a dead button by this text, and a
    reader recognises a running turn by it — so the wording is pinned here, on
    the message a turn really posts.
    """

    class _ReachedClaude(BaseException):
        """Raised in place of the Claude run: everything before it has happened."""

    async def _notice_of_a_turn(self, window_name: str | None) -> tuple[str, object]:
        """Run a turn up to the Claude call; return the text and view of its Stop notice."""
        from c_lord.discord_ui.views import StopView

        bot = MagicMock()
        bot.channel_id = 999
        bot.settings_repo = None
        bot.transcript_mirror_cog = None
        bot.user = MagicMock(id=1111)
        bot.get_cog = MagicMock(return_value=None)
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        repo.save = AsyncMock()
        repo.update_trigger_message = AsyncMock()
        config = MagicMock()
        config.working_dir = "/tmp/work"
        config.model = None
        config.timeout_seconds = 60
        config.effort = None
        cog = ClaudeChatCog(bot=bot, repo=repo, runner=config)

        sessions = MagicMock()
        sessions.create_session_dir = MagicMock(return_value="/tmp/work")
        tmux = MagicMock()
        tmux.create_session = MagicMock(return_value=window_name)
        cog._resolve_session_dir_manager = AsyncMock(return_value=sessions)  # type: ignore[method-assign]
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
        cog._get_dashboard = MagicMock(return_value=None)  # type: ignore[method-assign]
        cog._get_coordination = MagicMock(return_value=None)  # type: ignore[method-assign]
        cog._get_current_model = AsyncMock(return_value=None)  # type: ignore[method-assign]
        cog._apply_thread_naming = AsyncMock()  # type: ignore[method-assign]

        author = MagicMock()
        author.id = 4242
        author.display_name = author.name = "someone"
        author.bot = False
        message = MagicMock(spec=discord.Message)
        message.id = 77
        message.author = author
        message.add_reaction = AsyncMock()
        message.remove_reaction = AsyncMock()
        message.clear_reaction = AsyncMock()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 501
        thread.parent_id = 500
        thread.send = AsyncMock(return_value=MagicMock())

        reached = AsyncMock(side_effect=self._ReachedClaude)
        with (
            patch("c_lord.cogs.claude_chat.run_claude_with_config", reached),
            contextlib.suppress(self._ReachedClaude),
        ):
            await cog._run_claude(message, thread, "hi", None)
        reached.assert_awaited_once()

        (notice,) = [
            call
            for call in thread.send.await_args_list
            if isinstance(call.kwargs.get("view"), StopView)
        ]
        return notice.args[0], notice.kwargs["view"]

    @pytest.mark.asyncio
    async def test_it_names_the_tmux_window_the_turn_runs_in(self) -> None:
        text, _view = await self._notice_of_a_turn("w7")

        assert text == "-# ⏺ Session running (`w7`)"

    @pytest.mark.asyncio
    async def test_without_a_window_name_it_is_the_bare_line(self) -> None:
        text, _view = await self._notice_of_a_turn(None)

        assert text == "-# ⏺ Session running"
