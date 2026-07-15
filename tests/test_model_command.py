"""Tests for /model command group in SessionManageCog.

TDD: Write tests first, then implement.

Commands:
- /model show  — display current global model (+ per-thread model if in thread)
- /model set   — update global default model in settings_repo
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from c_lord.cogs.session_manage import (
    SETTING_CLAUDE_MODEL,
    SessionManageCog,
)
from c_lord.database.repository import SessionRecord


def _make_record(
    thread_id: int = 100,
    session_id: str = "abc-123",
    model: str | None = "sonnet",
) -> SessionRecord:
    return SessionRecord(
        thread_id=thread_id,
        session_id=session_id,
        working_dir="/home/user",
        model=model,
        origin="discord",
        summary=None,
        created_at="2026-02-22 10:00:00",
        last_used_at="2026-02-22 11:00:00",
    )


def _make_thread_interaction(thread_id: int = 12345) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    interaction.channel = thread
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_channel_interaction() -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_cog(
    default_model: str = "sonnet",
    settings_model: str | None = None,
) -> SessionManageCog:
    from c_lord.cogs.session_manage import SessionManageCog

    bot = MagicMock()
    bot.channel_id = 999

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.list_all = AsyncMock(return_value=[])

    settings_repo = MagicMock()
    settings_repo.get = AsyncMock(return_value=settings_model)
    settings_repo.set = AsyncMock()

    runner = MagicMock()
    runner.model = default_model

    return SessionManageCog(
        bot=bot,
        repo=repo,
        settings_repo=settings_repo,
        runner=runner,
    )


class TestModelShow:
    async def test_show_global_model_in_channel(self):
        """In a channel (not thread), show the global model."""
        cog = _make_cog(default_model="sonnet", settings_model=None)
        interaction = _make_channel_interaction()
        await cog.model_show.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        embed = call_args.kwargs.get("embed")
        assert embed is not None
        # Global model should appear in the embed
        assert "sonnet" in embed.description.lower() or any(
            "sonnet" in str(f.value).lower() for f in embed.fields
        )

    async def test_show_settings_model_overrides_runner(self):
        """settings_repo model takes precedence over runner.model."""
        cog = _make_cog(default_model="sonnet", settings_model="opus")
        interaction = _make_channel_interaction()
        await cog.model_show.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        embed = call_args.kwargs.get("embed")
        assert embed is not None
        text = embed.description + " ".join(str(f.value) for f in embed.fields)
        assert "opus" in text.lower()

    async def test_show_thread_model_from_session(self):
        """In a thread with a session, also show the per-thread model."""
        cog = _make_cog(default_model="sonnet", settings_model=None)
        record = _make_record(thread_id=12345, model="haiku")
        cog.repo.get = AsyncMock(return_value=record)

        interaction = _make_thread_interaction(thread_id=12345)
        await cog.model_show.callback(cog, interaction)
        call_args = interaction.response.send_message.call_args
        embed = call_args.kwargs.get("embed")
        assert embed is not None
        text = embed.description + " ".join(str(f.value) for f in embed.fields)
        assert "haiku" in text.lower()

    async def test_show_no_session_in_thread(self):
        """In a thread with no session, only show global model."""
        cog = _make_cog(default_model="sonnet", settings_model=None)
        cog.repo.get = AsyncMock(return_value=None)

        interaction = _make_thread_interaction(thread_id=12345)
        await cog.model_show.callback(cog, interaction)
        # Should succeed without error
        assert interaction.response.send_message.called

    async def test_show_no_settings_repo(self):
        """Graceful fallback when settings_repo is None."""
        from c_lord.cogs.session_manage import SessionManageCog

        bot = MagicMock()
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        runner = MagicMock()
        runner.model = "sonnet"

        cog = SessionManageCog(bot=bot, repo=repo, runner=runner)
        interaction = _make_channel_interaction()
        await cog.model_show.callback(cog, interaction)
        assert interaction.response.send_message.called


class TestModelSet:
    async def test_set_valid_model(self):
        """Setting a valid model stores it in settings_repo."""
        cog = _make_cog()
        interaction = _make_channel_interaction()
        await cog.model_set.callback(cog, interaction, model="opus")
        cog.settings_repo.set.assert_awaited_once_with(SETTING_CLAUDE_MODEL, "opus")

    async def test_set_model_sends_confirmation(self):
        """Setting a model sends a confirmation embed."""
        cog = _make_cog()
        interaction = _make_channel_interaction()
        await cog.model_set.callback(cog, interaction, model="haiku")
        call_args = interaction.response.send_message.call_args
        embed = call_args.kwargs.get("embed")
        assert embed is not None
        assert "haiku" in embed.description.lower() or any(
            "haiku" in str(f.value).lower() for f in embed.fields
        )

    async def test_set_malformed_model_rejected(self):
        """A malformed model string (spaces/shell metachars) is rejected, not saved (#478)."""
        cog = _make_cog()
        interaction = _make_channel_interaction()
        await cog.model_set.callback(cog, interaction, model="bad model")
        # settings_repo.set should NOT be called for malformed models
        cog.settings_repo.set.assert_not_awaited()
        call_args = interaction.response.send_message.call_args
        assert call_args.kwargs.get("ephemeral") is True

    async def test_set_freeform_model_id_accepted(self):
        """A well-formed tier-external model ID (e.g. claude-fable-5) is accepted
        and saved without needing a c-lord release (#478)."""
        cog = _make_cog()
        interaction = _make_channel_interaction()
        await cog.model_set.callback(cog, interaction, model="claude-fable-5")
        cog.settings_repo.set.assert_awaited_once_with(SETTING_CLAUDE_MODEL, "claude-fable-5")

    @pytest.mark.parametrize("bad", ["bad model", "-flag", "a;b", "a$(x)", "", "a/b", "x" * 65])
    async def test_set_rejects_unsafe_model_strings(self, bad: str):
        """Model strings reach a tmux/shell context via ``--model {model}``, so
        unsafe ones must be rejected locally before use (security-audit, #478)."""
        cog = _make_cog()
        interaction = _make_channel_interaction()
        await cog.model_set.callback(cog, interaction, model=bad)
        cog.settings_repo.set.assert_not_awaited()

    async def test_set_model_no_settings_repo(self):
        """When settings_repo is None, set sends ephemeral error."""
        from c_lord.cogs.session_manage import SessionManageCog

        bot = MagicMock()
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        runner = MagicMock()
        runner.model = "sonnet"

        cog = SessionManageCog(bot=bot, repo=repo, runner=runner)
        interaction = _make_channel_interaction()
        await cog.model_set.callback(cog, interaction, model="opus")
        call_args = interaction.response.send_message.call_args
        assert call_args.kwargs.get("ephemeral") is True

    @pytest.mark.parametrize("model", ["haiku", "sonnet", "opus"])
    async def test_all_valid_models_accepted(self, model: str):
        """All documented model names should be accepted."""
        cog = _make_cog()
        interaction = _make_channel_interaction()
        await cog.model_set.callback(cog, interaction, model=model)
        cog.settings_repo.set.assert_awaited_once_with(SETTING_CLAUDE_MODEL, model)


class TestModelCommandGrouping:
    """The model commands must be a `/model` group with `show`/`set` subcommands (#164)."""

    def _model_group(self) -> app_commands.Group:
        group = SessionManageCog.__cog_app_commands__  # type: ignore[attr-defined]
        for cmd in group:
            if isinstance(cmd, app_commands.Group) and cmd.name == "model":
                return cmd
        raise AssertionError("no `/model` app_commands.Group registered on the cog")

    def test_model_group_exists(self):
        group = self._model_group()
        assert isinstance(group, app_commands.Group)
        assert group.name == "model"

    def test_model_group_has_show_and_set_subcommands(self):
        group = self._model_group()
        names = {c.name for c in group.commands}
        assert names == {"show", "set"}

    def test_no_flat_model_commands_registered(self):
        """Flat `/model-show` and `/model-set` must no longer be top-level commands."""
        top_level = {
            c.name
            for c in SessionManageCog.__cog_app_commands__  # type: ignore[attr-defined]
        }
        assert "model-show" not in top_level
        assert "model-set" not in top_level

    def test_choice_labels_are_version_agnostic(self):
        """Dropdown labels must NOT hardcode a version number (#478): each alias
        resolves to the *latest* model of its tier, so '4.6'/'4.7'/'5' would lie."""
        import re as _re

        from c_lord.cogs.session_manage import _MODEL_CHOICES

        for c in _MODEL_CHOICES:
            assert not _re.search(r"\d", c.name), f"version number in label: {c.name!r}"

    def test_dropdown_is_three_tier_aliases(self):
        """The shipped dropdown stays the three tier aliases; tier-external models
        (e.g. Fable) are reached via free-form input, not the list (#478)."""
        from c_lord.cogs.session_manage import _MODEL_CHOICES

        assert {c.value for c in _MODEL_CHOICES} == {"sonnet", "opus", "haiku"}


class TestModelAutocomplete:
    """/model set uses autocomplete (not fixed choices) so any model ID can be
    typed, while still suggesting the tier aliases (#478)."""

    async def test_suggests_tier_aliases_when_empty(self):
        cog = _make_cog()
        interaction = _make_channel_interaction()
        choices = await cog._model_autocomplete(interaction, "")
        assert {"sonnet", "opus", "haiku"} <= {c.value for c in choices}

    async def test_offers_custom_entry_for_wellformed_id(self):
        cog = _make_cog()
        interaction = _make_channel_interaction()
        choices = await cog._model_autocomplete(interaction, "claude-fable-5")
        assert any(c.value == "claude-fable-5" for c in choices)

    async def test_no_custom_entry_for_malformed(self):
        cog = _make_cog()
        interaction = _make_channel_interaction()
        choices = await cog._model_autocomplete(interaction, "bad model")
        assert all(c.value != "bad model" for c in choices)
