"""Tests for Discord Role-based access control across all Cogs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

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
