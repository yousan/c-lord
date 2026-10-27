"""``/model`` and its text twins on SessionManageCog (#164, #478, #918).

``/model`` is one command: no arguments displays, arguments change one scope
(session / channel / global).  The old ``/model show`` and ``/model set``
subcommands are gone (a Discord command cannot be both a group and a command);
``!model-show`` and ``!model-set`` stay as the text-twin spellings.
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
from c_lord.database.models import init_db
from c_lord.database.settings_repo import SettingsRepository
from c_lord.discord_ui.authorization import Authorizer

OWNER = 499163459418587176


def _member() -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = OWNER
    member.bot = False
    member.roles = []
    return member


def _make_thread_interaction(thread_id: int = 12345, parent_id: int = 999) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    interaction.channel = thread
    interaction.user = _member()
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    return interaction


def _make_channel_interaction(channel_id: int = 999) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    interaction.channel = channel
    interaction.user = _member()
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    return interaction


@pytest.fixture
async def cog(tmp_path) -> SessionManageCog:
    db = str(tmp_path / "s.db")
    await init_db(db)
    bot = MagicMock()
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    runner = MagicMock()
    runner.model = "sonnet"
    runner.effort = None
    runner.model_source = "組み込み既定"
    runner.effort_source = "未設定"
    return SessionManageCog(
        bot=bot,
        repo=repo,
        settings_repo=SettingsRepository(db),
        runner=runner,
        authorizer=Authorizer(allowed_user_ids={OWNER}),
    )


def _text(interaction: MagicMock) -> str:
    args, kwargs = interaction.response.send_message.call_args
    return args[0] if args else kwargs.get("content", "")


class TestModelShow:
    async def test_bare_model_displays_the_layers(self, cog):
        interaction = _make_channel_interaction()
        await cog.model_command.callback(cog, interaction)
        text = _text(interaction)
        assert "sonnet" in text and "グローバル" in text and "新規に適用" in text
        assert "組み込み既定" not in text or "出所" in text  # source is named, not faked

    async def test_global_override_is_shown(self, cog):
        await cog.settings_repo.set(SETTING_CLAUDE_MODEL, "opus")
        interaction = _make_channel_interaction()
        await cog.model_command.callback(cog, interaction)
        assert "新規に適用 → opus" in _text(interaction)

    async def test_display_changes_nothing(self, cog):
        interaction = _make_thread_interaction()
        await cog.model_command.callback(cog, interaction)
        assert await cog.settings_repo.get_all() == {}

    async def test_show_without_settings_repo_degrades(self):
        bot = MagicMock()
        repo = MagicMock()
        runner = MagicMock()
        runner.model = "sonnet"
        cog = SessionManageCog(bot=bot, repo=repo, runner=runner)
        interaction = _make_channel_interaction()
        await cog.model_command.callback(cog, interaction)
        assert interaction.response.send_message.called


class TestModelSet:
    async def test_global_scope_stores_in_the_legacy_key(self, cog):
        interaction = _make_channel_interaction()
        await cog.model_command.callback(cog, interaction, model="opus", scope="global")
        assert await cog.settings_repo.get(SETTING_CLAUDE_MODEL) == "opus"
        assert "opus" in _text(interaction)

    async def test_freeform_model_id_accepted(self, cog):
        interaction = _make_channel_interaction()
        await cog.model_command.callback(cog, interaction, model="claude-fable-5", scope="global")
        assert await cog.settings_repo.get(SETTING_CLAUDE_MODEL) == "claude-fable-5"

    @pytest.mark.parametrize("bad", ["bad model", "-flag", "a;b", "a$(x)", "a/b", "x" * 65])
    async def test_unsafe_model_strings_are_rejected(self, cog, bad):
        """Model strings reach a tmux/shell context via ``--model {model}`` (#478)."""
        interaction = _make_channel_interaction()
        await cog.model_command.callback(cog, interaction, model=bad, scope="global")
        assert await cog.settings_repo.get(SETTING_CLAUDE_MODEL) is None
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    async def test_without_settings_repo_replies_with_an_error(self):
        bot = MagicMock()
        runner = MagicMock()
        runner.model = "sonnet"
        cog = SessionManageCog(
            bot=bot,
            repo=MagicMock(),
            runner=runner,
            authorizer=Authorizer(allowed_user_ids={OWNER}),
        )
        interaction = _make_channel_interaction()
        await cog.model_command.callback(cog, interaction, model="opus")
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.parametrize("model", ["haiku", "sonnet", "opus"])
    async def test_all_aliases_accepted(self, cog, model):
        interaction = _make_channel_interaction()
        await cog.model_command.callback(cog, interaction, model=model, scope="global")
        assert await cog.settings_repo.get(SETTING_CLAUDE_MODEL) == model


class TestScopesFromTheCommand:
    async def test_in_a_thread_the_default_scope_is_the_session(self, cog):
        interaction = _make_thread_interaction()
        await cog.model_command.callback(cog, interaction, model="haiku")
        assert await cog.settings_repo.get(SETTING_CLAUDE_MODEL) is None
        shown = _make_thread_interaction()
        await cog.model_command.callback(cog, shown)
        assert "このセッション → haiku" in _text(shown)

    async def test_in_a_channel_the_default_scope_is_the_channel(self, cog):
        await cog.model_command.callback(cog, _make_channel_interaction(), model="opus")
        assert await cog.settings_repo.get(SETTING_CLAUDE_MODEL) is None
        shown = _make_channel_interaction()
        await cog.model_command.callback(cog, shown)
        assert "新規に適用 → opus" in _text(shown)
        other = _make_channel_interaction(channel_id=555)
        await cog.model_command.callback(cog, other)
        assert "新規に適用 → sonnet" in _text(other)

    async def test_scope_channel_from_a_thread_targets_the_parent(self, cog):
        await cog.model_command.callback(
            cog, _make_thread_interaction(parent_id=777), model="opus", scope="channel"
        )
        shown = _make_channel_interaction(channel_id=777)
        await cog.model_command.callback(cog, shown)
        assert "新規に適用 → opus" in _text(shown)

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(model="opus", scope="session"),  # session scope outside a thread
            dict(model="opus", scope="planet"),
            dict(model="opus", effort="ultra"),
            dict(model="haiku", effort="high"),
            dict(include_existing=True),  # nothing to include
        ],
    )
    async def test_meaningless_requests_change_nothing(self, cog, kwargs):
        interaction = _make_channel_interaction()
        await cog.model_command.callback(cog, interaction, **kwargs)
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
        assert await cog.settings_repo.get_all() == {}
        shown = _make_channel_interaction()
        await cog.model_command.callback(cog, shown)
        assert "新規に適用 → sonnet" in _text(shown)

    async def test_include_existing_with_session_scope_is_an_error(self, cog):
        interaction = _make_thread_interaction()
        await cog.model_command.callback(
            cog, interaction, model="opus", scope="session", include_existing=True
        )
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


class TestPermissionBoundary:
    """Reading is open; every change goes through the allowlist (#781)."""

    async def _stranger(self, cog) -> MagicMock:
        interaction = _make_channel_interaction()
        stranger = MagicMock(spec=discord.Member)
        stranger.id = 12345
        stranger.bot = False
        stranger.roles = []
        interaction.user = stranger
        return interaction

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(model="opus", scope="global"),
            dict(model="opus"),
            dict(effort="low", scope="channel"),
            dict(model="opus", scope="global", include_existing=True),
        ],
    )
    async def test_stranger_cannot_change_anything(self, cog, kwargs):
        interaction = await self._stranger(cog)
        await cog.model_command.callback(cog, interaction, **kwargs)
        assert "not authorized" in _text(interaction)
        assert await cog.settings_repo.get_all() == {}

    async def test_stranger_can_still_read(self, cog):
        interaction = await self._stranger(cog)
        await cog.model_command.callback(cog, interaction)
        assert "新規に適用" in _text(interaction)

    async def test_text_twin_is_gated_too(self, cog):
        ctx = MagicMock()
        ctx.send = AsyncMock()
        ctx.channel = _make_channel_interaction().channel
        msg = MagicMock(spec=discord.Message)
        msg.webhook_id = None
        msg.author = MagicMock(spec=discord.Member)
        msg.author.id = 12345
        msg.author.bot = False
        msg.author.roles = []
        ctx.author = msg.author
        ctx.message = msg
        await cog.model_text.callback(cog, ctx, "opus", "scope:global")
        assert "not authorized" in ctx.send.call_args.args[0]
        assert await cog.settings_repo.get_all() == {}


class TestTextTwin:
    async def test_text_arguments_are_parsed(self, cog):
        ctx = MagicMock()
        ctx.send = AsyncMock()
        ctx.channel = _make_channel_interaction().channel
        ctx.author = _member()
        ctx.message = MagicMock(spec=discord.Message)
        ctx.message.webhook_id = None
        ctx.message.author = ctx.author
        await cog.model_text.callback(cog, ctx, "sonnet", "effort:medium", "scope:global")
        assert await cog.settings_repo.get(SETTING_CLAUDE_MODEL) == "sonnet"
        assert await cog.settings_repo.get("claude_effort") == "medium"

    async def test_garbage_argument_changes_nothing(self, cog):
        ctx = MagicMock()
        ctx.send = AsyncMock()
        ctx.channel = _make_channel_interaction().channel
        ctx.author = _member()
        ctx.message = MagicMock(spec=discord.Message)
        ctx.message.webhook_id = None
        ctx.message.author = ctx.author
        await cog.model_text.callback(cog, ctx, "sonnet", "whatever:1")
        assert await cog.settings_repo.get_all() == {}


class TestModelCommandShape:
    """``/model`` is a single command now, not a group (#918)."""

    def test_model_is_a_plain_command_with_the_four_options(self):
        top = {c.name: c for c in SessionManageCog.__cog_app_commands__}  # type: ignore[attr-defined]
        cmd = top["model"]
        assert isinstance(cmd, app_commands.Command)
        names = {p.display_name for p in cmd.parameters}
        assert names == {"model", "effort", "scope", "include-existing"}

    def test_no_group_or_flat_commands_remain(self):
        top = {
            c.name
            for c in SessionManageCog.__cog_app_commands__  # type: ignore[attr-defined]
            if isinstance(c, app_commands.Group)
        }
        assert "model" not in top

    def test_choice_labels_are_version_agnostic(self):
        """Dropdown labels must NOT hardcode a version number (#478)."""
        import re as _re

        from c_lord.cogs.session_manage import _MODEL_CHOICES

        for c in _MODEL_CHOICES:
            assert not _re.search(r"\d", c.name), f"version number in label: {c.name!r}"

    def test_dropdown_is_three_tier_aliases(self):
        from c_lord.cogs.session_manage import _MODEL_CHOICES

        assert {c.value for c in _MODEL_CHOICES} == {"sonnet", "opus", "haiku"}


class TestModelAutocomplete:
    """Autocomplete (not fixed choices) so any model ID can be typed (#478)."""

    async def test_suggests_tier_aliases_when_empty(self, cog):
        choices = await cog._model_value_autocomplete(_make_channel_interaction(), "")
        assert {"sonnet", "opus", "haiku", "inherit"} <= {c.value for c in choices}

    async def test_offers_custom_entry_for_wellformed_id(self, cog):
        choices = await cog._model_value_autocomplete(_make_channel_interaction(), "claude-fable-5")
        assert any(c.value == "claude-fable-5" for c in choices)

    async def test_no_custom_entry_for_malformed(self, cog):
        choices = await cog._model_value_autocomplete(_make_channel_interaction(), "bad model")
        assert all(c.value != "bad model" for c in choices)
