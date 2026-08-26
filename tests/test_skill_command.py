"""Tests for SkillCommandCog: parsing, autocomplete, args, in-thread, reload."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.skill_command import (
    SKILL_RELOAD_INTERVAL,
    SkillCommandCog,
    _load_skills,
    _parse_skill_meta,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cog(
    skills: list[dict[str, str]] | None = None,
    skills_dir: str | Path | None = None,
    allowed_user_ids: set[int] | None = None,
) -> SkillCommandCog:
    """Return a SkillCommandCog with mocked dependencies."""
    bot = MagicMock()
    # No settings repo → thread auto-archive resolver uses the 3-day default.
    bot.settings_repo = None
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.model = "sonnet"
    runner.working_dir = None
    runner.timeout_seconds = 300

    # Use a non-existent dir so _load_skills returns empty
    if skills_dir is None:
        skills_dir = "/nonexistent/skills"

    cog = SkillCommandCog(
        bot=bot,
        repo=repo,
        runner=runner,
        claude_channel_id=999,
        skills_dir=skills_dir,
        allowed_user_ids=allowed_user_ids,
    )
    # Override skills list for testing if provided
    if skills is not None:
        cog._skills = skills
    return cog


def _make_interaction(
    user_id: int = 1,
    channel: MagicMock | None = None,
) -> MagicMock:
    """Return a mocked discord.Interaction."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    if channel is not None:
        interaction.channel = channel
    else:
        interaction.channel = MagicMock(spec=discord.TextChannel)
    return interaction


def _make_thread(thread_id: int = 12345, parent_id: int = 999) -> MagicMock:
    """Return a mocked discord.Thread."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.mention = "<#12345>"
    thread.send = AsyncMock()
    return thread


# ---------------------------------------------------------------------------
# _parse_skill_meta
# ---------------------------------------------------------------------------


class TestParseSkillMeta:
    def test_valid_frontmatter(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: A cool skill\n---\n\nBody here."
        )
        result = _parse_skill_meta(skill_dir)
        assert result == {"name": "my-skill", "description": "A cool skill"}

    def test_name_defaults_to_dir_name(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "fallback-name"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\ndescription: Only desc\n---\n")
        result = _parse_skill_meta(skill_dir)
        assert result is not None
        assert result["name"] == "fallback-name"
        assert result["description"] == "Only desc"

    def test_no_skill_md(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "empty-skill"
        skill_dir.mkdir()
        assert _parse_skill_meta(skill_dir) is None

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "no-front"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Just a heading\nNo frontmatter here.")
        assert _parse_skill_meta(skill_dir) is None

    def test_empty_frontmatter(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "empty-front"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\n---\nBody.")
        # No name/desc fields → name defaults to dir name, desc is ""
        result = _parse_skill_meta(skill_dir)
        assert result is not None
        assert result["name"] == "empty-front"
        assert result["description"] == ""

    def test_os_error(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "broken"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("---\nname: x\n---\n")
        # Make unreadable
        skill_md.chmod(0o000)
        try:
            _parse_skill_meta(skill_dir)
            # On some systems this may still be readable by root
            # Either None or a valid result is acceptable
        finally:
            skill_md.chmod(0o644)


# ---------------------------------------------------------------------------
# _load_skills
# ---------------------------------------------------------------------------


class TestLoadSkills:
    def test_loads_multiple_skills(self, tmp_path: Path) -> None:
        for name in ["alpha", "beta", "gamma"]:
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Skill {name}\n---\n")
        # Add a file (not dir) — should be skipped
        (tmp_path / "not-a-dir.txt").write_text("skip me")
        skills = _load_skills(tmp_path)
        assert len(skills) == 3
        assert [s["name"] for s in skills] == ["alpha", "beta", "gamma"]

    def test_skips_invalid_skills(self, tmp_path: Path) -> None:
        # Valid
        d1 = tmp_path / "valid"
        d1.mkdir()
        (d1 / "SKILL.md").write_text("---\nname: valid\ndescription: OK\n---\n")
        # Invalid (no SKILL.md)
        (tmp_path / "invalid").mkdir()
        skills = _load_skills(tmp_path)
        assert len(skills) == 1
        assert skills[0]["name"] == "valid"

    def test_nonexistent_dir(self) -> None:
        skills = _load_skills(Path("/nonexistent/path"))
        assert skills == []


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------


class TestAutocomplete:
    @pytest.mark.asyncio
    async def test_filters_by_name(self) -> None:
        skills = [
            {"name": "todoist", "description": "Task management"},
            {"name": "goodmorning", "description": "Morning routine"},
            {"name": "recall", "description": "Context recovery"},
        ]
        cog = _make_cog(skills=skills)
        interaction = _make_interaction()
        choices = await cog._skill_name_autocomplete(interaction, "todo")
        assert len(choices) == 1
        assert choices[0].value == "todoist"

    @pytest.mark.asyncio
    async def test_filters_by_description(self) -> None:
        skills = [
            {"name": "todoist", "description": "Task management"},
            {"name": "goodmorning", "description": "Morning routine"},
        ]
        cog = _make_cog(skills=skills)
        interaction = _make_interaction()
        choices = await cog._skill_name_autocomplete(interaction, "morning")
        assert len(choices) == 1
        assert choices[0].value == "goodmorning"

    @pytest.mark.asyncio
    async def test_empty_query_returns_all(self) -> None:
        skills = [
            {"name": "a", "description": ""},
            {"name": "b", "description": ""},
        ]
        cog = _make_cog(skills=skills)
        interaction = _make_interaction()
        choices = await cog._skill_name_autocomplete(interaction, "")
        assert len(choices) == 2

    @pytest.mark.asyncio
    async def test_max_25_choices(self) -> None:
        skills = [{"name": f"skill-{i}", "description": ""} for i in range(30)]
        cog = _make_cog(skills=skills)
        interaction = _make_interaction()
        choices = await cog._skill_name_autocomplete(interaction, "")
        assert len(choices) == 25

    @pytest.mark.asyncio
    async def test_label_includes_description(self) -> None:
        skills = [{"name": "todoist", "description": "Manage tasks via API"}]
        cog = _make_cog(skills=skills)
        interaction = _make_interaction()
        choices = await cog._skill_name_autocomplete(interaction, "")
        assert "Manage tasks" in choices[0].name

    @pytest.mark.asyncio
    async def test_long_description_truncated(self) -> None:
        skills = [{"name": "x", "description": "A" * 100}]
        cog = _make_cog(skills=skills)
        interaction = _make_interaction()
        choices = await cog._skill_name_autocomplete(interaction, "")
        assert "…" in choices[0].name


# ---------------------------------------------------------------------------
# Lazy reload
# ---------------------------------------------------------------------------


class TestLazyReload:
    def test_no_reload_within_interval(self) -> None:
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        cog._last_loaded = time.monotonic()  # just loaded
        with patch("c_lord.cogs.skill_command._load_skills") as mock_load:
            cog._maybe_reload_skills()
            mock_load.assert_not_called()

    def test_reloads_after_interval(self) -> None:
        cog = _make_cog(skills=[])
        cog._last_loaded = time.monotonic() - SKILL_RELOAD_INTERVAL - 1
        new_skills = [{"name": "fresh", "description": "New skill"}]
        with patch("c_lord.cogs.skill_command._load_skills", return_value=new_skills) as mock_load:
            cog._maybe_reload_skills()
            mock_load.assert_called_once_with(cog._skills_dir)
        assert cog._skills == new_skills

    @pytest.mark.asyncio
    async def test_autocomplete_triggers_reload(self) -> None:
        cog = _make_cog(skills=[])
        cog._last_loaded = time.monotonic() - SKILL_RELOAD_INTERVAL - 1
        new_skills = [{"name": "new-skill", "description": "Appears after reload"}]
        interaction = _make_interaction()
        with patch("c_lord.cogs.skill_command._load_skills", return_value=new_skills):
            choices = await cog._skill_name_autocomplete(interaction, "new")
        assert len(choices) == 1
        assert choices[0].value == "new-skill"


# ---------------------------------------------------------------------------
# /skill command — authorization & validation
# ---------------------------------------------------------------------------


class TestRunSkillValidation:
    @pytest.mark.asyncio
    async def test_unauthorized_user(self) -> None:
        cog = _make_cog(allowed_user_ids={42})
        interaction = _make_interaction(user_id=99)
        await cog.run_skill.callback(cog, interaction, name="test", args=None)
        interaction.response.send_message.assert_called_once()
        assert "permission" in interaction.response.send_message.call_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_invalid_skill_name(self) -> None:
        cog = _make_cog(skills=[])
        interaction = _make_interaction()
        await cog.run_skill.callback(cog, interaction, name="bad;name", args=None)
        interaction.response.send_message.assert_called_once()
        assert "Invalid" in interaction.response.send_message.call_args.args[0]

    @pytest.mark.asyncio
    async def test_skill_not_found(self) -> None:
        cog = _make_cog(skills=[{"name": "existing", "description": ""}])
        interaction = _make_interaction()
        await cog.run_skill.callback(cog, interaction, name="missing", args=None)
        interaction.response.send_message.assert_called_once()
        assert "not found" in interaction.response.send_message.call_args.args[0]


# ---------------------------------------------------------------------------
# /skill — new thread mode (default)
# ---------------------------------------------------------------------------


class TestNewThreadMode:
    def _setup_bound_channel(self, cog: SkillCommandCog) -> None:
        """Set up ChannelRepoCog mock so the channel appears bound."""
        cc = _make_channel_cog_mock(
            session_dir_manager=MagicMock(), tmux_manager=MagicMock()
        )
        cog.bot.get_cog = MagicMock(return_value=cc)

    @pytest.mark.asyncio
    async def test_creates_thread_and_runs(self) -> None:
        cog = _make_cog(skills=[{"name": "todoist", "description": "Tasks"}])
        self._setup_bound_channel(cog)
        interaction = _make_interaction()

        # Set up the channel returned by bot.get_channel
        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_thread = _make_thread()
        mock_channel.create_thread = AsyncMock(return_value=mock_thread)
        cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="todoist", args=None)
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args[0][0]  # RunConfig object
            assert call_kwargs.prompt == "/todoist"
            assert call_kwargs.session_id is None

        # Thread was created
        mock_channel.create_thread.assert_called_once()
        # Followup sent
        interaction.followup.send.assert_called_once()
        assert "/todoist" in interaction.followup.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_args_included_in_prompt(self) -> None:
        cog = _make_cog(skills=[{"name": "todoist", "description": "Tasks"}])
        self._setup_bound_channel(cog)
        interaction = _make_interaction()

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_thread = _make_thread()
        mock_channel.create_thread = AsyncMock(return_value=mock_thread)
        cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="todoist", args='filter "today"')
            call_kwargs = mock_run.call_args[0][0]  # RunConfig object
            assert call_kwargs.prompt == '/todoist filter "today"'

    @pytest.mark.asyncio
    async def test_thread_name_includes_args(self) -> None:
        cog = _make_cog(skills=[{"name": "todoist", "description": "Tasks"}])
        self._setup_bound_channel(cog)
        interaction = _make_interaction()

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_thread = _make_thread()
        mock_channel.create_thread = AsyncMock(return_value=mock_thread)
        cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock):
            await cog.run_skill.callback(cog, interaction, name="todoist", args="search work")
        call_kwargs = mock_channel.create_thread.call_args.kwargs
        assert call_kwargs["name"] == "/todoist search work"
        # Defaults to 3 days (4320 min) when no setting is configured.
        assert call_kwargs["auto_archive_duration"] == 4320

    @pytest.mark.asyncio
    async def test_channel_not_found(self) -> None:
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        interaction = _make_interaction()
        cog.bot.get_channel = MagicMock(return_value=None)

        await cog.run_skill.callback(cog, interaction, name="test", args=None)
        interaction.followup.send.assert_called_once()
        assert "not found" in interaction.followup.send.call_args.args[0].lower()


# ---------------------------------------------------------------------------
# /skill — in-thread mode
# ---------------------------------------------------------------------------


class TestInThreadMode:
    def _setup_in_thread_cog(self, cog: SkillCommandCog) -> None:
        """Mock tmux/session_dir resolution for in-thread mode."""
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
        cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())

    @pytest.mark.asyncio
    async def test_resumes_session_in_thread(self) -> None:
        """When /skill is used inside a claude thread, resume the session."""
        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        self._setup_in_thread_cog(cog)
        thread = _make_thread(thread_id=5555, parent_id=999)
        interaction = _make_interaction(channel=thread)

        # Repo returns existing session
        record = MagicMock()
        record.session_id = "abc-123"
        cog.repo.get = AsyncMock(return_value=record)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="recall", args=None)
            call_kwargs = mock_run.call_args[0][0]  # RunConfig object
            assert call_kwargs.session_id == "abc-123"
            assert call_kwargs.prompt == "/recall"
            assert call_kwargs.thread is thread

    @pytest.mark.asyncio
    async def test_no_session_in_thread(self) -> None:
        """In-thread mode with no existing session passes session_id=None."""
        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        self._setup_in_thread_cog(cog)
        thread = _make_thread(thread_id=5555, parent_id=999)
        interaction = _make_interaction(channel=thread)
        cog.repo.get = AsyncMock(return_value=None)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="recall", args=None)
            call_kwargs = mock_run.call_args[0][0]  # RunConfig object
            assert call_kwargs.session_id is None

    @pytest.mark.asyncio
    async def test_non_claude_thread_creates_new(self) -> None:
        """A thread not under the claude channel creates a new thread."""
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        # Set up ChannelRepoCog so channel appears bound
        cc = _make_channel_cog_mock(
            session_dir_manager=MagicMock(), tmux_manager=MagicMock()
        )
        cog.bot.get_cog = MagicMock(return_value=cc)
        # Thread with different parent_id
        thread = _make_thread(thread_id=5555, parent_id=888)
        interaction = _make_interaction(channel=thread)

        mock_channel = MagicMock(spec=discord.TextChannel)
        new_thread = _make_thread()
        mock_channel.create_thread = AsyncMock(return_value=new_thread)
        cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="test", args=None)
            # Should have created a new thread, not used the existing one
            mock_channel.create_thread.assert_called_once()
            call_kwargs = mock_run.call_args[0][0]  # RunConfig object
            assert call_kwargs.session_id is None

    @pytest.mark.asyncio
    async def test_in_thread_with_args(self) -> None:
        """In-thread mode includes args in prompt."""
        cog = _make_cog(skills=[{"name": "todoist", "description": ""}])
        self._setup_in_thread_cog(cog)
        thread = _make_thread(thread_id=5555, parent_id=999)
        interaction = _make_interaction(channel=thread)
        cog.repo.get = AsyncMock(return_value=None)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="todoist", args="filter today")
            call_kwargs = mock_run.call_args[0][0]  # RunConfig object
            assert call_kwargs.prompt == "/todoist filter today"


# ---------------------------------------------------------------------------
# _is_claude_thread
# ---------------------------------------------------------------------------


class TestIsClaudeThread:
    def test_thread_under_claude_channel(self) -> None:
        cog = _make_cog()
        thread = _make_thread(parent_id=999)
        assert cog._is_claude_thread(thread) is True

    def test_thread_under_other_channel(self) -> None:
        cog = _make_cog()
        thread = _make_thread(parent_id=888)
        assert cog._is_claude_thread(thread) is False

    def test_not_a_thread(self) -> None:
        cog = _make_cog()
        channel = MagicMock(spec=discord.TextChannel)
        assert cog._is_claude_thread(channel) is False


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

        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        cog.bot.get_cog = MagicMock(return_value=cc)
        thread = _make_thread(thread_id=5555, parent_id=999)
        interaction = _make_interaction(channel=thread)
        cog.repo.get = AsyncMock(return_value=None)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="recall", args=None)
            config = mock_run.call_args[0][0]
            assert config.session_dir_manager is sdm
            assert config.tmux_manager is tmux

    @pytest.mark.asyncio
    async def test_new_thread_resolves_per_channel(self) -> None:
        """New-thread mode uses ChannelRepoCog to resolve managers."""
        sdm = MagicMock()
        tmux = MagicMock()
        cc = _make_channel_cog_mock(session_dir_manager=sdm, tmux_manager=tmux)

        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        cog.bot.get_cog = MagicMock(return_value=cc)

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 999
        new_thread = _make_thread()
        mock_channel.create_thread = AsyncMock(return_value=new_thread)
        cog.bot.get_channel = MagicMock(return_value=mock_channel)

        interaction = _make_interaction()  # non-thread channel

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill.callback(cog, interaction, name="test", args=None)
            config = mock_run.call_args[0][0]
            assert config.session_dir_manager is sdm
            assert config.tmux_manager is tmux

    @pytest.mark.asyncio
    async def test_unbound_channel_returns_error(self) -> None:
        """Unbound channel (no /clord-init) returns an error."""
        # ChannelRepoCog returns None for both managers
        cc = _make_channel_cog_mock()
        cog = _make_cog(skills=[{"name": "test", "description": ""}])
        cog.bot.get_cog = MagicMock(return_value=cc)

        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.id = 777  # unbound
        cog.bot.get_channel = MagicMock(return_value=mock_channel)

        interaction = _make_interaction()  # non-thread channel

        await cog.run_skill.callback(cog, interaction, name="test", args=None)

        # Should have sent error mentioning /clord-init
        interaction.followup.send.assert_called_once()
        msg = interaction.followup.send.call_args.args[0]
        assert "/clord-init" in msg


# ---------------------------------------------------------------------------
# !skill text/mention twin (E2E-testable via webhook)
# ---------------------------------------------------------------------------


def _make_ctx(channel: MagicMock | None = None, author_id: int = 1) -> MagicMock:
    """Return a mocked commands.Context for the !skill text twin."""
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.id = author_id
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

    @pytest.mark.asyncio
    async def test_unauthorized_user(self) -> None:
        cog = _make_cog(allowed_user_ids={42})
        ctx = _make_ctx(author_id=99)
        await cog.run_skill_text.callback(cog, ctx, name="test", args=None)
        ctx.send.assert_called_once()
        assert "permission" in ctx.send.call_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_invalid_skill_name(self) -> None:
        cog = _make_cog(skills=[])
        ctx = _make_ctx()
        await cog.run_skill_text.callback(cog, ctx, name="bad;name", args=None)
        assert "Invalid" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_skill_not_found(self) -> None:
        cog = _make_cog(skills=[{"name": "existing", "description": ""}])
        ctx = _make_ctx()
        await cog.run_skill_text.callback(cog, ctx, name="missing", args=None)
        assert "not found" in ctx.send.call_args.args[0]

    @pytest.mark.asyncio
    async def test_in_thread_runs_with_prompt_and_args(self) -> None:
        cog = _make_cog(skills=[{"name": "recall", "description": ""}])
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
        cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())
        cog._make_runner = MagicMock(return_value=MagicMock())
        thread = _make_thread(thread_id=5555, parent_id=999)
        ctx = _make_ctx(channel=thread)
        cog.repo.get = AsyncMock(return_value=None)

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config", new_callable=AsyncMock
        ) as mock_run:
            await cog.run_skill_text.callback(cog, ctx, name="recall", args="filter today")
            config = mock_run.call_args[0][0]
            assert config.prompt == "/recall filter today"
            assert config.thread is thread
            assert config.session_id is None
