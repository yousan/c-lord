"""Tests for Discord Role-based access control across all Cogs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord

from c_lord.cogs.channel_repo import ChannelRepoCog
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.cogs.skill_command import SkillCommandCog

# ---------------------------------------------------------------------------
# Helpers — mock Member / User with roles
# ---------------------------------------------------------------------------


def _make_member(user_id: int = 1, role_names: list[str] | None = None) -> MagicMock:
    """Return a MagicMock that behaves like a discord.Member with roles."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    roles: list[MagicMock] = []
    for name in role_names or []:
        role = MagicMock(spec=discord.Role)
        role.name = name
        roles.append(role)
    # @everyone role is always present at index 0
    everyone = MagicMock(spec=discord.Role)
    everyone.name = "@everyone"
    roles.insert(0, everyone)
    member.roles = roles
    return member


def _make_user(user_id: int = 1) -> MagicMock:
    """Return a MagicMock that behaves like a discord.User (no roles, e.g. DM)."""
    user = MagicMock(spec=discord.User)
    user.id = user_id
    return user


# ---------------------------------------------------------------------------
# ClaudeChatCog helpers
# ---------------------------------------------------------------------------


def _make_chat_cog(
    allowed_user_ids: set[int] | None = None,
    allowed_role_name: str | None = None,
) -> ClaudeChatCog:
    bot = MagicMock()
    bot.channel_id = 999
    _default_ctx = MagicMock()
    _default_ctx.valid = False
    bot.get_context = AsyncMock(return_value=_default_ctx)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    return ClaudeChatCog(
        bot=bot,
        repo=repo,
        runner=runner,
        allowed_user_ids=allowed_user_ids,
        allowed_role_name=allowed_role_name,
    )


# ---------------------------------------------------------------------------
# SkillCommandCog helpers
# ---------------------------------------------------------------------------


def _make_skill_cog(
    allowed_user_ids: set[int] | None = None,
    allowed_role_name: str | None = None,
) -> SkillCommandCog:
    bot = MagicMock()
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    runner = MagicMock()
    runner.clone = MagicMock(return_value=MagicMock())
    return SkillCommandCog(
        bot=bot,
        repo=repo,
        runner=runner,
        claude_channel_id=999,
        skills_dir="/nonexistent/skills",
        allowed_user_ids=allowed_user_ids,
        allowed_role_name=allowed_role_name,
    )


# ---------------------------------------------------------------------------
# ChannelRepoCog helpers
# ---------------------------------------------------------------------------


def _make_channel_cog(
    allowed_user_ids: set[int] | None = None,
    allowed_role_name: str | None = None,
) -> ChannelRepoCog:
    bot = MagicMock()
    repo = MagicMock()
    return ChannelRepoCog(
        bot,
        repo=repo,
        allowed_user_ids=allowed_user_ids,
        allowed_role_name=allowed_role_name,
    )


# ===========================================================================
# Tests — ClaudeChatCog._is_allowed
# ===========================================================================


class TestClaudeChatCogIsAllowed:
    def test_allowed_by_user_id(self) -> None:
        cog = _make_chat_cog(allowed_user_ids={42})
        member = _make_member(user_id=42)
        assert cog._is_allowed(member) is True

    def test_denied_by_user_id(self) -> None:
        cog = _make_chat_cog(allowed_user_ids={42})
        member = _make_member(user_id=99)
        assert cog._is_allowed(member) is False

    def test_allowed_by_role(self) -> None:
        cog = _make_chat_cog(allowed_role_name="claude-operator")
        member = _make_member(user_id=99, role_names=["claude-operator"])
        assert cog._is_allowed(member) is True

    def test_denied_without_role(self) -> None:
        cog = _make_chat_cog(allowed_role_name="claude-operator")
        member = _make_member(user_id=99, role_names=["some-other-role"])
        assert cog._is_allowed(member) is False

    def test_both_unset_allows_all(self) -> None:
        cog = _make_chat_cog()
        member = _make_member(user_id=99)
        assert cog._is_allowed(member) is True

    def test_user_id_or_role(self) -> None:
        """User in allowed_user_ids should pass even without role."""
        cog = _make_chat_cog(allowed_user_ids={42}, allowed_role_name="claude-operator")
        member_by_id = _make_member(user_id=42, role_names=[])
        assert cog._is_allowed(member_by_id) is True

        member_by_role = _make_member(user_id=99, role_names=["claude-operator"])
        assert cog._is_allowed(member_by_role) is True

        member_neither = _make_member(user_id=99, role_names=["other"])
        assert cog._is_allowed(member_neither) is False

    def test_dm_user_no_roles(self) -> None:
        """DM context — discord.User (not Member) has no roles."""
        cog = _make_chat_cog(allowed_role_name="claude-operator")
        user = _make_user(user_id=99)
        assert cog._is_allowed(user) is False

    def test_dm_user_allowed_by_id(self) -> None:
        """DM context — user ID match should still work."""
        cog = _make_chat_cog(allowed_user_ids={42}, allowed_role_name="claude-operator")
        user = _make_user(user_id=42)
        assert cog._is_allowed(user) is True


# ===========================================================================
# Tests — ClaudeChatCog._is_message_authorized
# Infra (webhooks + trusted bots) bypass the human allowlist so that setting an
# owner does not break CI/CD webhooks or trusted companion bots.
# ===========================================================================


class TestMessageAuthorization:
    @staticmethod
    def _msg(author: MagicMock, *, webhook_id: int | None = None) -> MagicMock:
        m = MagicMock(spec=discord.Message)
        m.webhook_id = webhook_id
        m.author = author
        return m

    @staticmethod
    def _human(user_id: int, role_names: list[str] | None = None) -> MagicMock:
        a = _make_member(user_id, role_names)
        a.bot = False
        return a

    @staticmethod
    def _bot_author(user_id: int) -> MagicMock:
        a = MagicMock()
        a.id = user_id
        a.bot = True
        return a

    def test_webhook_bypasses_allowlist(self) -> None:
        # Owner-restricted, but a webhook message (URL possession = auth) passes.
        cog = _make_chat_cog(allowed_user_ids={42})
        msg = self._msg(self._bot_author(999), webhook_id=12345)
        assert cog._is_message_authorized(msg) is True

    def test_trusted_bot_bypasses_allowlist(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_TRUSTED_BOT_IDS", "777")
        cog = _make_chat_cog(allowed_user_ids={42})
        msg = self._msg(self._bot_author(777))
        assert cog._is_message_authorized(msg) is True

    def test_untrusted_bot_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_TRUSTED_BOT_IDS", "777")
        cog = _make_chat_cog(allowed_user_ids={42})
        msg = self._msg(self._bot_author(888))
        assert cog._is_message_authorized(msg) is False

    def test_owner_human_allowed(self) -> None:
        cog = _make_chat_cog(allowed_user_ids={42})
        assert cog._is_message_authorized(self._msg(self._human(42))) is True

    def test_non_owner_human_denied(self) -> None:
        cog = _make_chat_cog(allowed_user_ids={42})
        assert cog._is_message_authorized(self._msg(self._human(7))) is False

    def test_unconfigured_allows_human(self) -> None:
        # Zero-config preserved: with no allowlist, humans are still allowed.
        cog = _make_chat_cog()
        assert cog._is_message_authorized(self._msg(self._human(7))) is True


# ===========================================================================
# Tests — text commands (!clord / !attach) must use the *actor* rule (#507)
#
# `ClaudeDiscordBot.process_commands` deliberately lets webhook messages reach
# text commands (E2E / CI-CD triggers), but the commands themselves used the
# human-only allowlist (`_is_allowed`).  With `DISCORD_OWNER_ID` set, the
# webhook's pseudo-author never matches the owner id, so every webhook-driven
# `!clord` was answered with "You are not authorized to use this command."
#
# Auth passing is asserted via the *next* check the command reaches (unbound
# repo / tmux not configured) — the same message the owner-less staging bot
# returned for the very webhook that prod rejected.
# ===========================================================================

UNAUTHORIZED = "You are not authorized to use this command."


def _sent_text(ctx: MagicMock) -> str:
    """Concatenate everything the command sent back through ``ctx.send``."""
    return " ".join(str(c.args[0]) for c in ctx.send.await_args_list if c.args)


def _make_text_ctx(
    author: MagicMock,
    *,
    webhook_id: int | None = None,
    channel_spec: type = discord.TextChannel,
) -> MagicMock:
    """A ``commands.Context`` double for a text command invocation."""
    ctx = MagicMock()
    ctx.author = author
    ctx.channel = MagicMock(spec=channel_spec)
    ctx.channel.id = 555
    ctx.channel.parent_id = None
    ctx.message = MagicMock(spec=discord.Message)
    ctx.message.webhook_id = webhook_id
    ctx.message.author = author
    ctx.send = AsyncMock()
    return ctx


def _unbound(cog: ClaudeChatCog) -> ClaudeChatCog:
    """Make both resolvers return None so an authorized command stops early."""
    cog._resolve_session_dir_manager = AsyncMock(return_value=None)  # type: ignore[method-assign]
    cog._resolve_tmux_manager = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return cog


class TestClordTextAuthorization:
    """`!clord` — webhooks and trusted bots must not be locked out by an owner."""

    async def test_webhook_not_rejected(self) -> None:
        cog = _unbound(_make_chat_cog(allowed_user_ids={42}))
        ctx = _make_text_ctx(TestMessageAuthorization._bot_author(999), webhook_id=12345)

        await cog.clord_text.callback(cog, ctx, prompt="hello")

        sent = _sent_text(ctx)
        assert UNAUTHORIZED not in sent
        # Reached the next gate — exactly what the owner-less staging bot replied.
        assert "リポジトリが紐づけられていません" in sent

    async def test_trusted_bot_not_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("CLORD_TRUSTED_BOT_IDS", "777")
        cog = _unbound(_make_chat_cog(allowed_user_ids={42}))
        ctx = _make_text_ctx(TestMessageAuthorization._bot_author(777))

        await cog.clord_text.callback(cog, ctx, prompt="hello")

        assert UNAUTHORIZED not in _sent_text(ctx)

    async def test_untrusted_bot_still_rejected(self, monkeypatch) -> None:
        # The bypass must not widen to *any* bot.
        monkeypatch.setenv("CLORD_TRUSTED_BOT_IDS", "777")
        cog = _unbound(_make_chat_cog(allowed_user_ids={42}))
        ctx = _make_text_ctx(TestMessageAuthorization._bot_author(888))

        await cog.clord_text.callback(cog, ctx, prompt="hello")

        assert UNAUTHORIZED in _sent_text(ctx)

    async def test_non_owner_human_still_rejected(self) -> None:
        # The human allowlist itself is unchanged.
        cog = _unbound(_make_chat_cog(allowed_user_ids={42}))
        ctx = _make_text_ctx(TestMessageAuthorization._human(7))

        await cog.clord_text.callback(cog, ctx, prompt="hello")

        assert UNAUTHORIZED in _sent_text(ctx)

    async def test_owner_human_allowed(self) -> None:
        cog = _unbound(_make_chat_cog(allowed_user_ids={42}))
        ctx = _make_text_ctx(TestMessageAuthorization._human(42))

        await cog.clord_text.callback(cog, ctx, prompt="hello")

        assert UNAUTHORIZED not in _sent_text(ctx)


class TestAttachTextAuthorization:
    """`!attach` — same defect, same call site shape (#507 AC2)."""

    async def test_webhook_not_rejected(self) -> None:
        cog = _unbound(_make_chat_cog(allowed_user_ids={42}))
        ctx = _make_text_ctx(
            TestMessageAuthorization._bot_author(999),
            webhook_id=12345,
            channel_spec=discord.Thread,
        )

        await cog.attach_text.callback(cog, ctx, window="w1")

        sent = _sent_text(ctx)
        assert UNAUTHORIZED not in sent
        assert "tmux is not configured" in sent

    async def test_non_owner_human_still_rejected(self) -> None:
        cog = _unbound(_make_chat_cog(allowed_user_ids={42}))
        ctx = _make_text_ctx(TestMessageAuthorization._human(7), channel_spec=discord.Thread)

        await cog.attach_text.callback(cog, ctx, window="w1")

        assert UNAUTHORIZED in _sent_text(ctx)


class TestSlashClordAuthorizationUnchanged:
    """`/clord` has no message behind it — the human allowlist still governs (#507 AC6)."""

    @staticmethod
    def _interaction(user: MagicMock) -> MagicMock:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = user
        interaction.channel = MagicMock(spec=discord.TextChannel)
        interaction.channel.id = 555
        interaction.channel_id = 555
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_non_owner_human_rejected(self) -> None:
        cog = _unbound(_make_chat_cog(allowed_user_ids={42}))
        interaction = self._interaction(TestMessageAuthorization._human(7))

        await cog.start_session.callback(cog, interaction, "hello")

        interaction.response.send_message.assert_awaited_once()
        assert UNAUTHORIZED in interaction.response.send_message.await_args.args[0]

    async def test_owner_human_allowed(self) -> None:
        cog = _unbound(_make_chat_cog(allowed_user_ids={42}))
        interaction = self._interaction(TestMessageAuthorization._human(42))

        await cog.start_session.callback(cog, interaction, "hello")

        sent = interaction.response.send_message.await_args.args[0]
        assert UNAUTHORIZED not in sent


# ===========================================================================
# Tests — SkillCommandCog._is_authorized
# ===========================================================================


class TestSkillCommandCogIsAuthorized:
    def test_allowed_by_user_id(self) -> None:
        cog = _make_skill_cog(allowed_user_ids={42})
        member = _make_member(user_id=42)
        assert cog._is_authorized(member) is True

    def test_denied_by_user_id(self) -> None:
        cog = _make_skill_cog(allowed_user_ids={42})
        member = _make_member(user_id=99)
        assert cog._is_authorized(member) is False

    def test_allowed_by_role(self) -> None:
        cog = _make_skill_cog(allowed_role_name="claude-operator")
        member = _make_member(user_id=99, role_names=["claude-operator"])
        assert cog._is_authorized(member) is True

    def test_denied_without_role(self) -> None:
        cog = _make_skill_cog(allowed_role_name="claude-operator")
        member = _make_member(user_id=99, role_names=["other"])
        assert cog._is_authorized(member) is False

    def test_both_unset_allows_all(self) -> None:
        cog = _make_skill_cog()
        member = _make_member(user_id=99)
        assert cog._is_authorized(member) is True

    def test_user_id_or_role(self) -> None:
        cog = _make_skill_cog(allowed_user_ids={42}, allowed_role_name="claude-operator")
        member_by_id = _make_member(user_id=42, role_names=[])
        assert cog._is_authorized(member_by_id) is True

        member_by_role = _make_member(user_id=99, role_names=["claude-operator"])
        assert cog._is_authorized(member_by_role) is True

    def test_dm_user_no_roles(self) -> None:
        cog = _make_skill_cog(allowed_role_name="claude-operator")
        user = _make_user(user_id=99)
        assert cog._is_authorized(user) is False


# ===========================================================================
# Tests — ChannelRepoCog._is_allowed
# ===========================================================================


class TestChannelRepoCogIsAllowed:
    def test_allowed_by_user_id(self) -> None:
        cog = _make_channel_cog(allowed_user_ids={42})
        member = _make_member(user_id=42)
        assert cog._is_allowed(member) is True

    def test_denied_by_user_id(self) -> None:
        cog = _make_channel_cog(allowed_user_ids={42})
        member = _make_member(user_id=99)
        assert cog._is_allowed(member) is False

    def test_allowed_by_role(self) -> None:
        cog = _make_channel_cog(allowed_role_name="claude-operator")
        member = _make_member(user_id=99, role_names=["claude-operator"])
        assert cog._is_allowed(member) is True

    def test_denied_without_role(self) -> None:
        cog = _make_channel_cog(allowed_role_name="claude-operator")
        member = _make_member(user_id=99, role_names=["other"])
        assert cog._is_allowed(member) is False

    def test_both_unset_allows_all(self) -> None:
        cog = _make_channel_cog()
        member = _make_member(user_id=99)
        assert cog._is_allowed(member) is True

    def test_user_id_or_role(self) -> None:
        cog = _make_channel_cog(allowed_user_ids={42}, allowed_role_name="claude-operator")
        member_by_id = _make_member(user_id=42, role_names=[])
        assert cog._is_allowed(member_by_id) is True

        member_by_role = _make_member(user_id=99, role_names=["claude-operator"])
        assert cog._is_allowed(member_by_role) is True

    def test_dm_user_no_roles(self) -> None:
        cog = _make_channel_cog(allowed_role_name="claude-operator")
        user = _make_user(user_id=99)
        assert cog._is_allowed(user) is False
