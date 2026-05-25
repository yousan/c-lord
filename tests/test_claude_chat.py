"""Tests for ClaudeChatCog: /stop command, attachment handling, and interrupt-on-new-message."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.cogs.claude_chat import (
    _MAX_ATTACHMENT_BYTES,
    _MAX_ATTACHMENTS,
    _MAX_TOTAL_BYTES,
    ClaudeChatCog,
)
from c_lord.concurrency import SessionRegistry
from c_lord.coordination.service import CoordinationService


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
    """Return a ClaudeChatCog with minimal mocked dependencies."""
    bot = MagicMock()
    bot.channel_id = 999
    # Default get_context returns a non-command context (valid=False)
    _default_ctx = MagicMock()
    _default_ctx.valid = False
    bot.get_context = AsyncMock(return_value=_default_ctx)
    # By default, ChannelRepoCog returns None (no binding).
    # Tests that need a binding should pass a channel_cog mock.
    bot.get_cog = MagicMock(return_value=channel_cog)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    repo.delete = AsyncMock(return_value=True)
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner)


def _make_thread_interaction(thread_id: int = 12345) -> MagicMock:
    """Return an Interaction whose channel is a discord.Thread."""
    interaction = MagicMock(spec=discord.Interaction)
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    interaction.channel = thread
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_channel_interaction() -> MagicMock:
    """Return an Interaction whose channel is NOT a thread."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


class TestStopCommand:
    @pytest.mark.asyncio
    async def test_stop_outside_thread_sends_ephemeral(self) -> None:
        """Using /stop outside a thread sends an ephemeral error."""
        cog = _make_cog()
        interaction = _make_channel_interaction()

        await cog.stop_session.callback(cog, interaction)

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_stop_no_active_runner_sends_ephemeral(self) -> None:
        """Using /stop when nothing is running sends an ephemeral notice."""
        cog = _make_cog()
        interaction = _make_thread_interaction(thread_id=12345)

        # _active_runners is empty — no session running
        await cog.stop_session.callback(cog, interaction)

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_stop_calls_runner_interrupt(self) -> None:
        """Using /stop with an active runner calls runner.interrupt()."""
        cog = _make_cog()
        thread_id = 12345
        interaction = _make_thread_interaction(thread_id=thread_id)

        mock_runner = MagicMock()
        mock_runner.interrupt = AsyncMock()
        cog._active_runners[thread_id] = mock_runner

        await cog.stop_session.callback(cog, interaction)

        mock_runner.interrupt.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_does_not_delete_session_from_db(self) -> None:
        """/stop must NOT delete the session from the DB (so resume works)."""
        cog = _make_cog()
        thread_id = 12345
        interaction = _make_thread_interaction(thread_id=thread_id)

        mock_runner = MagicMock()
        mock_runner.interrupt = AsyncMock()
        cog._active_runners[thread_id] = mock_runner

        await cog.stop_session.callback(cog, interaction)

        # repo.delete should NEVER be called by /stop
        cog.repo.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_does_not_remove_from_active_runners(self) -> None:
        """/stop should leave _active_runners cleanup to _run_claude's finally."""
        cog = _make_cog()
        thread_id = 12345
        interaction = _make_thread_interaction(thread_id=thread_id)

        mock_runner = MagicMock()
        mock_runner.interrupt = AsyncMock()
        cog._active_runners[thread_id] = mock_runner

        await cog.stop_session.callback(cog, interaction)

        # Still in dict — _run_claude's finally handles removal
        assert thread_id in cog._active_runners

    @pytest.mark.asyncio
    async def test_stop_sends_stopped_embed(self) -> None:
        """/stop success response should use the stopped_embed (orange, not red)."""
        cog = _make_cog()
        thread_id = 12345
        interaction = _make_thread_interaction(thread_id=thread_id)

        mock_runner = MagicMock()
        mock_runner.interrupt = AsyncMock()
        cog._active_runners[thread_id] = mock_runner

        await cog.stop_session.callback(cog, interaction)

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args.kwargs
        embed = call_kwargs.get("embed")
        assert embed is not None
        assert "stopped" in embed.title.lower()
        # Orange color (not red error)
        assert embed.color.value == 0xFFA500


class TestActiveCountAlias:
    """Tests for ClaudeChatCog.active_count (DrainAware alias)."""

    def test_active_count_equals_active_session_count(self) -> None:
        """active_count should be an alias for active_session_count."""
        cog = _make_cog()
        assert cog.active_count == 0
        assert cog.active_count == cog.active_session_count

        # Add a fake runner
        cog._active_runners[1] = MagicMock()
        assert cog.active_count == 1
        assert cog.active_count == cog.active_session_count

        cog._active_runners[2] = MagicMock()
        assert cog.active_count == 2
        assert cog.active_count == cog.active_session_count


class TestBuildPrompt:
    """Tests for the _build_prompt method (attachment handling)."""

    @staticmethod
    def _make_attachment(
        filename: str = "test.txt",
        content_type: str = "text/plain",
        size: int = 100,
        content: bytes = b"hello world",
        url: str = "https://cdn.discordapp.com/attachments/123/456/test.txt?ex=abc&is=def&hm=xyz",
    ) -> MagicMock:
        att = MagicMock(spec=discord.Attachment)
        att.filename = filename
        att.content_type = content_type
        att.size = size
        att.url = url
        att.read = AsyncMock(return_value=content)
        return att

    @staticmethod
    def _make_message(content: str = "my message", attachments: list | None = None) -> MagicMock:
        msg = MagicMock(spec=discord.Message)
        msg.content = content
        msg.attachments = attachments or []
        return msg

    @pytest.mark.asyncio
    async def test_no_attachments_returns_content(self) -> None:
        cog = _make_cog()
        msg = self._make_message(content="hello")
        result = await cog._build_prompt(msg)
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_text_attachment_appended(self) -> None:
        cog = _make_cog()
        att = self._make_attachment(filename="notes.txt", content=b"file content here")
        msg = self._make_message(content="check this", attachments=[att])

        result = await cog._build_prompt(msg)

        assert "check this" in result
        assert "notes.txt" in result
        assert "file content here" in result

    @pytest.mark.asyncio
    async def test_image_attachment_url_embedded_in_prompt(self) -> None:
        """Images are NOT downloaded; their CDN URL is embedded in the prompt."""
        image_url = "https://cdn.discordapp.com/attachments/1/2/image.png?ex=abc&is=def&hm=xyz"
        cog = _make_cog()
        att = self._make_attachment(
            filename="image.png",
            content_type="image/png",
            size=100,
            content=b"\x89PNG...",
            url=image_url,
        )
        msg = self._make_message(content="see image", attachments=[att])

        prompt, image_paths = await cog._build_prompt_and_images(msg)

        # URL is embedded in the prompt text.
        assert image_url in prompt
        assert "image.png" in prompt
        # No tempfiles — images are not downloaded.
        assert image_paths == []
        att.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_binary_non_image_url_embedded(self) -> None:
        """Non-image binary files (e.g. zip, PDF) have their URL embedded."""
        file_url = "https://cdn.discordapp.com/attachments/1/2/archive.zip?ex=abc&is=def&hm=xyz"
        cog = _make_cog()
        att = self._make_attachment(
            filename="archive.zip",
            content_type="application/zip",
            content=b"PK...",
            url=file_url,
        )
        msg = self._make_message(content="see zip", attachments=[att])

        result = await cog._build_prompt(msg)

        assert file_url in result
        assert "archive.zip" in result
        att.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_images_all_urls_embedded(self) -> None:
        """Multiple image attachments all get their URLs embedded."""
        cog = _make_cog()
        images = [
            self._make_attachment(
                filename=f"img{i}.png",
                content_type="image/png",
                url=f"https://cdn.discordapp.com/attachments/1/2/img{i}.png?ex=abc",
            )
            for i in range(3)
        ]
        msg = self._make_message(content="three images", attachments=images)

        prompt, image_paths = await cog._build_prompt_and_images(msg)

        for i in range(3):
            assert f"img{i}.png" in prompt
            assert f"img{i}.png?ex=abc" in prompt
        assert image_paths == []
        for att in images:
            att.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_image_and_text_attachment_combined(self) -> None:
        """Image URL + text file content both appear in the prompt."""
        image_url = "https://cdn.discordapp.com/attachments/1/2/photo.jpg?ex=abc"
        cog = _make_cog()
        img_att = self._make_attachment(
            filename="photo.jpg",
            content_type="image/jpeg",
            url=image_url,
        )
        txt_att = self._make_attachment(filename="notes.txt", content=b"my notes")
        msg = self._make_message(content="check these", attachments=[img_att, txt_att])

        prompt, image_paths = await cog._build_prompt_and_images(msg)

        assert image_url in prompt
        assert "my notes" in prompt
        assert image_paths == []

    @pytest.mark.asyncio
    async def test_oversized_attachment_skipped(self) -> None:
        cog = _make_cog()
        att = self._make_attachment(
            filename="huge.txt",
            content_type="text/plain",
            size=_MAX_ATTACHMENT_BYTES + 1,
        )
        msg = self._make_message(content="big file", attachments=[att])

        result = await cog._build_prompt(msg)

        assert result == "big file"
        att.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_content_with_attachment(self) -> None:
        """Message with only an attachment (no text) should still work."""
        cog = _make_cog()
        att = self._make_attachment(
            filename="code.py", content_type="text/x-python", content=b"print('hi')"
        )
        msg = self._make_message(content="", attachments=[att])

        result = await cog._build_prompt(msg)

        assert "code.py" in result
        assert "print('hi')" in result

    @pytest.mark.asyncio
    async def test_max_attachments_limit(self) -> None:
        """Only the first _MAX_ATTACHMENTS files should be processed."""
        cog = _make_cog()
        attachments = [
            self._make_attachment(filename=f"file{i}.txt", content=f"content{i}".encode())
            for i in range(_MAX_ATTACHMENTS + 2)
        ]
        msg = self._make_message(attachments=attachments)

        await cog._build_prompt(msg)

        # Extra attachments beyond the limit should not be read
        for att in attachments[_MAX_ATTACHMENTS:]:
            att.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_total_size_limit_stops_processing(self) -> None:
        """Processing stops when cumulative size exceeds _MAX_TOTAL_BYTES."""
        cog = _make_cog()
        # Each file is just under the per-file limit but together they exceed total
        chunk = _MAX_ATTACHMENT_BYTES - 100  # 49.9 KB
        attachments = [
            self._make_attachment(
                filename=f"file{i}.txt",
                size=chunk,
                content=b"x" * chunk,
            )
            for i in range(10)
        ]
        msg = self._make_message(attachments=attachments)

        await cog._build_prompt(msg)

        # Should stop after total exceeds _MAX_TOTAL_BYTES (~2 files)
        read_count = sum(1 for att in attachments if att.read.called)
        expected_max = (_MAX_TOTAL_BYTES // chunk) + 1
        assert read_count <= expected_max

    @pytest.mark.asyncio
    async def test_json_attachment_included(self) -> None:
        """application/json is in the allowed types."""
        cog = _make_cog()
        att = self._make_attachment(
            filename="config.json",
            content_type="application/json",
            content=b'{"key": "value"}',
        )
        msg = self._make_message(content="here is config", attachments=[att])

        result = await cog._build_prompt(msg)

        assert "config.json" in result
        assert '{"key": "value"}' in result

    @pytest.mark.asyncio
    async def test_multiple_text_attachments(self) -> None:
        """Multiple allowed attachments should all be included."""
        cog = _make_cog()
        attachments = [
            self._make_attachment(filename="a.txt", content=b"alpha"),
            self._make_attachment(filename="b.md", content_type="text/markdown", content=b"beta"),
        ]
        msg = self._make_message(content="two files", attachments=attachments)

        result = await cog._build_prompt(msg)

        assert "a.txt" in result
        assert "alpha" in result
        assert "b.md" in result
        assert "beta" in result


class TestRegistryAutoDiscovery:
    """Registry should be auto-discovered from bot.session_registry."""

    def test_auto_discovers_from_bot(self) -> None:
        """When registry=None, Cog picks up bot.session_registry."""
        bot = MagicMock()
        bot.session_registry = SessionRegistry()
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        assert cog._registry is bot.session_registry

    def test_explicit_registry_takes_precedence(self) -> None:
        """When registry is explicitly passed, it wins over bot attribute."""
        bot = MagicMock()
        bot.session_registry = SessionRegistry()
        explicit = SessionRegistry()
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), registry=explicit)
        assert cog._registry is explicit

    def test_no_bot_attribute_falls_back_to_none(self) -> None:
        """When bot has no session_registry, _registry stays None."""
        bot = MagicMock(spec=[])  # no attributes
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        assert cog._registry is None


class TestInterruptOnNewMessage:
    """New message in active thread should interrupt the running session."""

    def _make_thread_message(self, thread_id: int = 42) -> MagicMock:
        """Return a discord.Message inside a Thread."""
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.parent_id = 999
        thread.send = AsyncMock()
        msg = MagicMock(spec=discord.Message)
        msg.channel = thread
        msg.content = "new instruction"
        msg.attachments = []
        msg.author = MagicMock()
        msg.author.bot = False
        return msg

    @pytest.mark.asyncio
    async def test_interrupt_called_when_runner_active(self) -> None:
        """When a runner is active for a thread, _handle_thread_reply must interrupt it."""
        cog = _make_cog()
        thread_id = 42
        message = self._make_thread_message(thread_id)

        # Plant an active runner in the cog
        existing_runner = MagicMock()
        existing_runner.interrupt = AsyncMock()
        cog._active_runners[thread_id] = existing_runner

        # Stub _run_claude so we don't actually spawn Claude
        cog._run_claude = AsyncMock()

        await cog._handle_thread_reply(message)

        existing_runner.interrupt.assert_called_once()

    @pytest.mark.asyncio
    async def test_interrupt_message_sent_to_thread(self) -> None:
        """The thread should receive a notification when the session is interrupted."""
        cog = _make_cog()
        thread_id = 42
        message = self._make_thread_message(thread_id)
        thread = message.channel

        existing_runner = MagicMock()
        existing_runner.interrupt = AsyncMock()
        cog._active_runners[thread_id] = existing_runner
        cog._run_claude = AsyncMock()

        await cog._handle_thread_reply(message)

        thread.send.assert_called_once()
        sent_text: str = thread.send.call_args.args[0]
        assert "interrupted" in sent_text.lower() or "⚡" in sent_text

    @pytest.mark.asyncio
    async def test_no_interrupt_when_no_active_runner(self) -> None:
        """When no runner is active, _handle_thread_reply skips interrupt."""
        cog = _make_cog()
        message = self._make_thread_message(thread_id=42)
        thread = message.channel

        cog._run_claude = AsyncMock()

        await cog._handle_thread_reply(message)

        # No notification message sent for interruption
        thread.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_awaits_existing_task_before_new_session(self) -> None:
        """_handle_thread_reply must await the existing task to ensure cleanup completes."""
        cog = _make_cog()
        thread_id = 42
        message = self._make_thread_message(thread_id)

        existing_runner = MagicMock()
        existing_runner.interrupt = AsyncMock()
        cog._active_runners[thread_id] = existing_runner

        # A future that we can control to simulate a running task
        cleanup_done = asyncio.Event()
        call_order: list[str] = []

        async def slow_task() -> None:
            await cleanup_done.wait()
            call_order.append("task_done")

        task = asyncio.ensure_future(slow_task())
        cog._active_tasks[thread_id] = task

        async def run_claude_stub(*args, **kwargs) -> None:
            call_order.append("new_session_started")

        cog._run_claude = run_claude_stub

        # Let cleanup complete so the await resolves
        cleanup_done.set()

        await cog._handle_thread_reply(message)

        assert call_order == ["task_done", "new_session_started"]

    @pytest.mark.asyncio
    async def test_run_claude_called_with_session_id_after_interrupt(self) -> None:
        """After interrupt, _run_claude is called with the session_id from the DB."""
        cog = _make_cog()
        thread_id = 42
        message = self._make_thread_message(thread_id)

        # Simulate a saved session in DB
        record = MagicMock()
        record.session_id = "abc-123"
        cog.repo.get = AsyncMock(return_value=record)

        existing_runner = MagicMock()
        existing_runner.interrupt = AsyncMock()
        cog._active_runners[thread_id] = existing_runner
        cog._run_claude = AsyncMock()

        await cog._handle_thread_reply(message)

        cog._run_claude.assert_called_once()
        _, kwargs = cog._run_claude.call_args
        assert kwargs.get("session_id") == "abc-123"

    @pytest.mark.asyncio
    async def test_active_tasks_dict_initialized(self) -> None:
        """ClaudeChatCog must initialize _active_tasks as an empty dict."""
        cog = _make_cog()
        assert hasattr(cog, "_active_tasks")
        assert isinstance(cog._active_tasks, dict)
        assert len(cog._active_tasks) == 0


class TestZeroConfigCoordination:
    """_get_coordination() must work without any consumer wiring (Zero-Config Principle).

    Consumers must get new features by updating the package alone —
    no code changes, no bot.coordination wiring required.
    """

    def test_auto_creates_from_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No bot.coordination → auto-creates CoordinationService from env var."""
        monkeypatch.setenv("COORDINATION_CHANNEL_ID", "1234567890")
        bot = MagicMock(spec=[])  # no attributes at all
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        svc = cog._get_coordination()
        assert isinstance(svc, CoordinationService)
        assert svc.enabled is True

    def test_no_op_when_env_var_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env var → returns a no-op CoordinationService (never None)."""
        monkeypatch.delenv("COORDINATION_CHANNEL_ID", raising=False)
        bot = MagicMock(spec=[])
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        svc = cog._get_coordination()
        assert isinstance(svc, CoordinationService)
        assert svc.enabled is False  # no-op, but not None

    def test_bot_attribute_takes_precedence(self) -> None:
        """Explicitly set bot.coordination wins over env var auto-creation."""
        explicit = CoordinationService(MagicMock(), channel_id=9999)
        bot = MagicMock()
        bot.coordination = explicit
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        assert cog._get_coordination() is explicit

    def test_constructor_arg_takes_precedence(self) -> None:
        """Explicitly passed coordination= wins over everything."""
        explicit = CoordinationService(MagicMock(), channel_id=9999)
        bot = MagicMock()
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), coordination=explicit)
        assert cog._get_coordination() is explicit

    def test_result_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Second call returns same object (no repeated env var reads)."""
        monkeypatch.setenv("COORDINATION_CHANNEL_ID", "1234567890")
        bot = MagicMock(spec=[])
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        assert cog._get_coordination() is cog._get_coordination()


class TestSpawnSession:
    """Tests for ClaudeChatCog.spawn_session()."""

    @pytest.mark.asyncio
    async def test_spawn_creates_thread_and_returns_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn_session creates a thread with the right name and returns it."""
        from unittest.mock import AsyncMock, MagicMock, patch

        import discord

        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.name = "Test spawn"
        thread.send = AsyncMock()

        channel = MagicMock()
        channel.create_thread = AsyncMock(return_value=thread)

        bot = MagicMock()
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())

        with patch.object(cog, "_run_claude", new=AsyncMock()):
            result = await cog.spawn_session(channel, "Do the thing")

        assert result is thread
        channel.create_thread.assert_called_once()
        call_kwargs = channel.create_thread.call_args.kwargs
        assert call_kwargs["name"] == "Do the thing"
        assert call_kwargs["type"] == discord.ChannelType.public_thread

    @pytest.mark.asyncio
    async def test_spawn_uses_custom_thread_name(self) -> None:
        """thread_name overrides the default (prompt[:100])."""
        from unittest.mock import AsyncMock, MagicMock, patch

        import discord

        thread = MagicMock(spec=discord.Thread)
        thread.send = AsyncMock()

        channel = MagicMock()
        channel.create_thread = AsyncMock(return_value=thread)

        bot = MagicMock()
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())

        with patch.object(cog, "_run_claude", new=AsyncMock()):
            await cog.spawn_session(channel, "Very long prompt text", thread_name="Short name")

        kwargs = channel.create_thread.call_args.kwargs
        assert kwargs["name"] == "Short name"

    @pytest.mark.asyncio
    async def test_spawn_posts_seed_message(self) -> None:
        """spawn_session sends the prompt as the first thread message."""
        from unittest.mock import AsyncMock, MagicMock, patch

        import discord

        thread = MagicMock(spec=discord.Thread)
        seed_msg = MagicMock()
        thread.send = AsyncMock(return_value=seed_msg)

        channel = MagicMock()
        channel.create_thread = AsyncMock(return_value=thread)

        bot = MagicMock()
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())

        mock_run = AsyncMock()
        with patch.object(cog, "_run_claude", new=mock_run):
            await cog.spawn_session(channel, "Hello Claude")

        thread.send.assert_called_once_with("Hello Claude")
        # _run_claude receives the seed message (not a user message)
        user_msg_arg = mock_run.call_args.args[0]
        assert user_msg_arg is seed_msg


class TestOnReady:
    """Tests for ClaudeChatCog.on_ready — startup session resume logic."""

    @pytest.mark.asyncio
    async def test_on_ready_no_resume_repo_is_noop(self) -> None:
        """If resume_repo is not set, on_ready should do nothing."""
        # spec=[] prevents MagicMock from auto-generating resume_repo attribute
        bot = MagicMock(spec=[])
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        assert cog._resume_repo is None
        # Should complete without error and without touching bot
        await cog.on_ready()

    @pytest.mark.asyncio
    async def test_on_ready_no_pending_is_noop(self) -> None:
        """If resume_repo returns no pending entries, on_ready does nothing."""
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.database.resume_repo import PendingResumeRepository

        resume_repo = MagicMock(spec=PendingResumeRepository)
        resume_repo.get_pending = AsyncMock(return_value=[])

        bot = MagicMock()
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), resume_repo=resume_repo)
        await cog.on_ready()
        bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_ready_deletes_before_spawning(self) -> None:
        """Row must be deleted BEFORE _run_claude is called (single-fire guarantee)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        import discord

        from c_lord.database.resume_repo import PendingResume, PendingResumeRepository

        entry = PendingResume(
            id=7,
            thread_id=555,
            session_id="sess-abc",
            reason="self_restart",
            resume_prompt="Continue please.",
            created_at="2026-02-21 20:00:00",
        )
        resume_repo = MagicMock(spec=PendingResumeRepository)
        resume_repo.get_pending = AsyncMock(return_value=[entry])
        resume_repo.delete = AsyncMock()

        thread = MagicMock(spec=discord.Thread)
        thread.id = 555
        thread.send = AsyncMock(return_value=MagicMock())
        parent = MagicMock(spec=discord.TextChannel)
        thread.parent = parent

        bot = MagicMock()
        bot.get_channel.return_value = thread

        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), resume_repo=resume_repo)

        call_order: list[str] = []
        resume_repo.delete.side_effect = lambda _: call_order.append("delete")

        async def fake_run_claude(*args, **kwargs):
            call_order.append("run_claude")

        with patch.object(cog, "_run_claude", side_effect=fake_run_claude):
            await cog.on_ready()
            # create_task schedules the coroutine; yield to the event loop so it runs.
            await asyncio.sleep(0)

        assert call_order == ["delete", "run_claude"], (
            "delete() must be called before _run_claude to prevent double-resume"
        )

    @pytest.mark.asyncio
    async def test_on_ready_skips_non_thread_channels(self) -> None:
        """If get_channel returns a non-Thread, skip gracefully."""
        from unittest.mock import AsyncMock, MagicMock

        import discord

        from c_lord.database.resume_repo import PendingResume, PendingResumeRepository

        entry = PendingResume(
            id=1,
            thread_id=100,
            session_id=None,
            reason="self_restart",
            resume_prompt=None,
            created_at="2026-02-21 20:00:00",
        )
        resume_repo = MagicMock(spec=PendingResumeRepository)
        resume_repo.get_pending = AsyncMock(return_value=[entry])
        resume_repo.delete = AsyncMock()

        # Return a TextChannel (not a Thread) — should be skipped
        bot = MagicMock()
        bot.get_channel.return_value = MagicMock(spec=discord.TextChannel)

        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), resume_repo=resume_repo)
        # Should not raise
        await cog.on_ready()
        # delete was still called (single-fire)
        resume_repo.delete.assert_called_once_with(1)


class TestCogUnloadMarkForResume:
    """Tests for cog_unload() auto-marking active sessions for restart-resume."""

    def _make_cog_with_resume_repo(self) -> tuple[ClaudeChatCog, MagicMock, MagicMock]:
        """Return (cog, repo, resume_repo) with resume_repo configured."""
        bot = MagicMock()
        bot.channel_id = 999
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        resume_repo = MagicMock()
        resume_repo.mark = AsyncMock(return_value=1)
        cog = ClaudeChatCog(bot=bot, repo=repo, runner=MagicMock(), resume_repo=resume_repo)
        return cog, repo, resume_repo

    @pytest.mark.asyncio
    async def test_no_op_when_no_active_runners(self) -> None:
        """cog_unload is a no-op when no sessions are running."""
        cog, _, resume_repo = self._make_cog_with_resume_repo()
        assert len(cog._active_runners) == 0

        await cog.cog_unload()

        resume_repo.mark.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_op_when_no_resume_repo(self) -> None:
        """cog_unload is a no-op when resume_repo is not configured."""
        cog = _make_cog()  # no resume_repo
        cog._active_runners[111] = MagicMock()

        await cog.cog_unload()  # Should not raise

    @pytest.mark.asyncio
    async def test_marks_each_active_runner(self) -> None:
        """Calls resume_repo.mark() for every thread in _active_runners."""
        cog, repo, resume_repo = self._make_cog_with_resume_repo()
        cog._active_runners[111] = MagicMock()
        cog._active_runners[222] = MagicMock()

        await cog.cog_unload()

        assert resume_repo.mark.call_count == 2
        called_thread_ids = {call.args[0] for call in resume_repo.mark.call_args_list}
        assert called_thread_ids == {111, 222}

    @pytest.mark.asyncio
    async def test_uses_bot_shutdown_reason(self) -> None:
        """Marks sessions with reason='bot_shutdown'."""
        cog, _, resume_repo = self._make_cog_with_resume_repo()
        cog._active_runners[333] = MagicMock()

        await cog.cog_unload()

        call_kwargs = resume_repo.mark.call_args.kwargs
        assert call_kwargs["reason"] == "bot_shutdown"

    @pytest.mark.asyncio
    async def test_resolves_session_id_from_repo(self) -> None:
        """Looks up session_id from self.repo for --resume continuity."""
        cog, repo, resume_repo = self._make_cog_with_resume_repo()
        session_record = MagicMock()
        session_record.session_id = "test-session-xyz"
        repo.get = AsyncMock(return_value=session_record)

        cog._active_runners[444] = MagicMock()
        await cog.cog_unload()

        repo.get.assert_awaited_once_with(444)
        assert resume_repo.mark.call_args.kwargs["session_id"] == "test-session-xyz"

    @pytest.mark.asyncio
    async def test_continues_on_mark_failure(self) -> None:
        """Failure to mark one thread does not prevent marking others."""
        cog, _, resume_repo = self._make_cog_with_resume_repo()
        resume_repo.mark = AsyncMock(side_effect=[RuntimeError("db error"), 2])
        cog._active_runners[111] = MagicMock()
        cog._active_runners[222] = MagicMock()

        # Should not raise
        await cog.cog_unload()

        assert resume_repo.mark.call_count == 2

    @pytest.mark.asyncio
    async def test_uses_none_session_id_when_repo_has_no_record(self) -> None:
        """Falls back to session_id=None when no session record exists."""
        cog, repo, resume_repo = self._make_cog_with_resume_repo()
        repo.get = AsyncMock(return_value=None)
        cog._active_runners[555] = MagicMock()

        await cog.cog_unload()

        assert resume_repo.mark.call_args.kwargs["session_id"] is None


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

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_no_allowed_user_ids_allows_everyone(self) -> None:
        """When allowed_user_ids is None, any user can use /claude."""
        cc = _make_channel_cog_mock(tmux_manager=MagicMock())
        cog = _make_cog(channel_cog=cc)  # allowed_user_ids=None
        interaction = _make_channel_interaction()
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
        cog.spawn_session.assert_called_once_with(channel, "build a feature")
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
        cog._run_claude.assert_called_once()
        _, kwargs = cog._run_claude.call_args
        assert kwargs["session_id"] == "sess-abc"
        assert kwargs["prompt"] == "continue this"

    @pytest.mark.asyncio
    async def test_thread_execution_no_existing_session(self) -> None:
        """Running /claude in a thread with no DB record passes session_id=None."""
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

        cog.repo.get = AsyncMock(return_value=None)
        cog._run_claude = AsyncMock()

        await cog.start_session.callback(cog, interaction, prompt="start fresh")

        _, kwargs = cog._run_claude.call_args
        assert kwargs["session_id"] is None

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
        cog.spawn_session.assert_called_once_with(channel, "hello")


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

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs.get("ephemeral") is True

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

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_attach_no_tmux_manager(self) -> None:
        """When tmux_manager is not configured, sends ephemeral error."""
        cog = _make_cog()
        cog.bot.tmux_manager = None  # explicitly no tmux
        interaction = _make_thread_interaction()
        interaction.user = MagicMock()
        interaction.user.id = 42

        await cog.attach_window.callback(cog, interaction, window="work1")

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_attach_success(self) -> None:
        """Successful remap responds with confirmation and saves DB record."""
        tmux_mgr = MagicMock()
        tmux_mgr.remap_window.return_value = True
        cc = _make_channel_cog_mock(tmux_manager=tmux_mgr)
        cog = _make_cog(channel_cog=cc)

        interaction = _make_thread_interaction(thread_id=12345)
        interaction.user = MagicMock()
        interaction.user.id = 42

        await cog.attach_window.callback(cog, interaction, window="work3")

        tmux_mgr.remap_window.assert_called_once_with(12345, "work3")
        interaction.response.send_message.assert_called_once()
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

        interaction = _make_thread_interaction(thread_id=12345)
        interaction.user = MagicMock()
        interaction.user.id = 42

        await cog.attach_window.callback(cog, interaction, window="nonexistent")

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs.get("ephemeral") is True
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
        return ctx

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
        msg.channel = thread
        msg.content = "!attach work13"
        msg.attachments = []
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
        msg.channel = thread
        msg.content = "hello Claude"
        msg.attachments = []
        msg.author = MagicMock()
        msg.author.bot = False
        msg.author.id = 42
        msg.webhook_id = None

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


class TestMultiChannelOnMessage:
    """on_message should handle threads with existing sessions in any channel."""

    def _make_thread_message(self, thread_id: int = 42, parent_id: int = 7777) -> MagicMock:
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.parent_id = parent_id
        thread.send = AsyncMock()
        msg = MagicMock(spec=discord.Message)
        msg.channel = thread
        msg.content = "follow up"
        msg.attachments = []
        msg.author = MagicMock()
        msg.author.bot = False
        msg.webhook_id = None
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

        # With serialization, call_log should be [start, end, start, end]
        # Without it, it would be [start, start, end, end]
        assert call_log == ["start", "end", "start", "end"]


class TestClearCommand:
    """Tests for /clear command (issue #117, #123)."""

    @pytest.mark.asyncio
    async def test_clear_outside_thread_sends_ephemeral(self) -> None:
        """/clear outside a thread sends an ephemeral error."""
        cog = _make_cog()
        interaction = _make_channel_interaction()

        await cog.clear_session.callback(cog, interaction)

        interaction.response.send_message.assert_called_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_clear_kills_active_runner(self) -> None:
        """/clear kills an active runner when one is present."""
        tmux_manager = MagicMock()
        tmux_manager.kill_session = MagicMock(return_value=True)
        channel_cog = _make_channel_cog_mock(tmux_manager=tmux_manager)
        cog = _make_cog(channel_cog=channel_cog)
        thread_id = 12345

        runner = AsyncMock()
        cog._active_runners[thread_id] = runner
        cog.repo.reset = AsyncMock(return_value=True)

        interaction = _make_thread_interaction(thread_id)
        thread = interaction.channel
        thread.parent_id = 999

        await cog.clear_session.callback(cog, interaction)

        runner.kill.assert_called_once()
        assert thread_id not in cog._active_runners

    @pytest.mark.asyncio
    async def test_clear_kills_tmux_window_for_idle_session(self) -> None:
        """Issue #123: /clear on idle thread (no active runner) must still kill the tmux window.

        Before the fix, kill_session was only called when runner was in _active_runners.
        After the fix, it is called unconditionally when tmux_manager is available.
        """
        tmux_manager = MagicMock()
        tmux_manager.kill_session = MagicMock(return_value=True)
        channel_cog = _make_channel_cog_mock(tmux_manager=tmux_manager)
        cog = _make_cog(channel_cog=channel_cog)
        thread_id = 12345

        # No active runner — this is the "idle session" case
        assert thread_id not in cog._active_runners
        cog.repo.reset = AsyncMock(return_value=True)

        interaction = _make_thread_interaction(thread_id)
        thread = interaction.channel
        thread.parent_id = 999

        await cog.clear_session.callback(cog, interaction)

        # tmux window must be killed even though there was no active runner
        tmux_manager.kill_session.assert_called_once_with(thread_id)

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

        interaction.response.send_message.assert_called_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
