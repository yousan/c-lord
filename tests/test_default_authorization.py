"""The default when nothing is configured is *fail-closed* (#713).

c-lord used to treat "no ``DISCORD_OWNER_ID``, no ``CLORD_ALLOWED_ROLE``" as
"everyone is allowed" — and talking to c-lord means running a shell on the host
that runs it.  Someone following the README and starting the bot handed their
host to every member of the server without being told.

The default is now: **only the application's own owner** — the account that
created the bot in the Discord developer portal, which Discord already knows,
so nothing has to be configured for it to work (Zero-Config).  Opening it back
up to everyone is still possible, but only on purpose (``CLORD_ALLOW_ANYONE=1``)
and never in silence.

These tests pin all four gates that answer "may this user drive c-lord?":
``Authorizer`` (messages / slash), ``AuthorizedViewMixin`` (buttons),
``SkillCommandCog`` (``/skill``) and ``ChannelRepoCog`` (``/clord-init``).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.discord_ui.authorization import (
    AuthorizedViewMixin,
    Authorizer,
    get_fallback_owner_ids,
    resolve_fallback_owner_ids,
    set_fallback_owner_ids,
)

OWNER = 4242
TEAM_MATE = 4343
OUTSIDER = 99


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_member(user_id: int = 1, role_names: list[str] | None = None) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    roles: list[MagicMock] = []
    for name in role_names or []:
        role = MagicMock(spec=discord.Role)
        role.name = name
        roles.append(role)
    everyone = MagicMock(spec=discord.Role)
    everyone.name = "@everyone"
    roles.insert(0, everyone)
    member.roles = roles
    return member


def _make_user(user_id: int = 1) -> MagicMock:
    user = MagicMock(spec=discord.User)
    user.id = user_id
    return user


def _make_interaction(user: MagicMock) -> MagicMock:
    interaction = MagicMock()
    interaction.user = user
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_bot(owner_id: int | None = OWNER, team_ids: list[int] | None = None) -> MagicMock:
    """A bot whose ``application_info()`` answers like Discord's does."""
    app = MagicMock()
    if team_ids is None:
        app.team = None
        app.owner = MagicMock()
        app.owner.id = owner_id
        app.owner.__str__ = lambda self: "owner#0001"  # type: ignore[method-assign]
    else:
        app.team = MagicMock()
        members = []
        for tid in team_ids:
            m = MagicMock()
            m.id = tid
            members.append(m)
        app.team.members = members
        app.team.name = "a-team"
    bot = MagicMock()
    bot.application = None
    bot.application_info = AsyncMock(return_value=app)
    return bot


# ---------------------------------------------------------------------------
# Authorizer — the unconfigured default
# ---------------------------------------------------------------------------


class TestUnconfiguredAuthorizer:
    def test_nobody_allowed_before_owner_is_resolved(self) -> None:
        """Unresolved owner ⇒ deny.  Never "everyone" while we don't know."""
        auth = Authorizer()
        assert auth.is_allowed(_make_member(user_id=OUTSIDER)) is False

    def test_owner_allowed_after_resolution(self) -> None:
        set_fallback_owner_ids({OWNER})
        auth = Authorizer()
        assert auth.is_allowed(_make_member(user_id=OWNER)) is True

    def test_non_owner_rejected_after_resolution(self) -> None:
        set_fallback_owner_ids({OWNER})
        auth = Authorizer()
        assert auth.is_allowed(_make_member(user_id=OUTSIDER)) is False

    def test_team_member_allowed(self) -> None:
        set_fallback_owner_ids({OWNER, TEAM_MATE})
        auth = Authorizer()
        assert auth.is_allowed(_make_member(user_id=TEAM_MATE)) is True

    def test_owner_allowed_in_dm_too(self) -> None:
        """A DM has no roles, but the owner is matched by id (AC6)."""
        set_fallback_owner_ids({OWNER})
        auth = Authorizer()
        assert auth.is_allowed(_make_user(user_id=OWNER)) is True
        assert auth.is_allowed(_make_user(user_id=OUTSIDER)) is False

    def test_bare_user_id_follows_the_same_rule(self) -> None:
        set_fallback_owner_ids({OWNER})
        auth = Authorizer()
        assert auth.is_allowed_user_id(OWNER) is True
        assert auth.is_allowed_user_id(OUTSIDER) is False


class TestAllowAnyoneEscapeHatch:
    """AC4 — opening it up is allowed, but only explicitly."""

    def test_env_opens_it_to_everyone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_ALLOW_ANYONE", "1")
        auth = Authorizer()
        assert auth.is_allowed(_make_member(user_id=OUTSIDER)) is True
        assert auth.is_allowed_user_id(OUTSIDER) is True

    def test_explicit_kwarg_opens_it_to_everyone(self) -> None:
        auth = Authorizer(allow_anyone=True)
        assert auth.is_allowed(_make_member(user_id=OUTSIDER)) is True

    def test_off_values_do_not_open_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ("0", "false", "no", "", "off"):
            monkeypatch.setenv("CLORD_ALLOW_ANYONE", value)
            assert Authorizer().is_allowed(_make_member(user_id=OUTSIDER)) is False

    async def test_warns_at_startup(self, caplog: pytest.LogCaptureFixture) -> None:
        """fail-open is a choice, never an accident — it must be announced."""
        bot = _make_bot()
        with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.authorization"):
            await resolve_fallback_owner_ids(bot, Authorizer(allow_anyone=True))
        assert any(
            record.levelno >= logging.WARNING and "CLORD_ALLOW_ANYONE" in record.getMessage()
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# AC7 — a configured allowlist behaves exactly as it did before
# ---------------------------------------------------------------------------


class TestConfiguredAllowlistUnchanged:
    def test_user_id_allowlist(self) -> None:
        set_fallback_owner_ids({OWNER})
        auth = Authorizer(allowed_user_ids={42})
        assert auth.is_allowed(_make_member(user_id=42)) is True
        assert auth.is_allowed(_make_member(user_id=OUTSIDER)) is False
        # The app owner is NOT silently added to a configured allowlist.
        assert auth.is_allowed(_make_member(user_id=OWNER)) is False

    def test_role_allowlist(self) -> None:
        set_fallback_owner_ids({OWNER})
        auth = Authorizer(allowed_role_name="claude-operator")
        assert auth.is_allowed(_make_member(user_id=7, role_names=["claude-operator"])) is True
        assert auth.is_allowed(_make_member(user_id=7, role_names=["other"])) is False
        assert auth.is_allowed(_make_member(user_id=OWNER)) is False
        assert auth.is_allowed(_make_user(user_id=7)) is False  # DM has no roles

    def test_or_logic(self) -> None:
        auth = Authorizer(allowed_user_ids={42}, allowed_role_name="ops")
        assert auth.is_allowed(_make_member(user_id=42, role_names=["x"])) is True
        assert auth.is_allowed(_make_member(user_id=7, role_names=["ops"])) is True
        assert auth.is_allowed(_make_member(user_id=7, role_names=["x"])) is False

    def test_bare_user_id_with_allowlist(self) -> None:
        auth = Authorizer(allowed_user_ids={42})
        assert auth.is_allowed_user_id(42) is True
        assert auth.is_allowed_user_id(OUTSIDER) is False

    async def test_resolution_is_skipped_when_configured(self) -> None:
        """No ``application_info()`` call at all — nothing to fall back to."""
        bot = _make_bot()
        await resolve_fallback_owner_ids(bot, Authorizer(allowed_user_ids={42}))
        bot.application_info.assert_not_called()
        assert get_fallback_owner_ids() is None


# ---------------------------------------------------------------------------
# resolve_fallback_owner_ids — where the owner comes from (AC2, AC3)
# ---------------------------------------------------------------------------


class TestOwnerResolution:
    async def test_resolves_application_owner(self) -> None:
        bot = _make_bot(owner_id=OWNER)
        await resolve_fallback_owner_ids(bot, Authorizer())
        assert get_fallback_owner_ids() == {OWNER}

    async def test_resolves_team_members(self) -> None:
        bot = _make_bot(team_ids=[OWNER, TEAM_MATE])
        await resolve_fallback_owner_ids(bot, Authorizer())
        assert get_fallback_owner_ids() == {OWNER, TEAM_MATE}

    async def test_logs_one_line_naming_who_may_use_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC3 — narrowing silently is the failure mode c-lord hates (#585)."""
        bot = _make_bot(owner_id=OWNER)
        with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.authorization"):
            await resolve_fallback_owner_ids(bot, Authorizer())
        messages = [r.getMessage() for r in caplog.records]
        assert any(str(OWNER) in m for m in messages), messages
        # ...and how to change it.
        assert any("DISCORD_OWNER_ID" in m or "CLORD_ALLOW_ANYONE" in m for m in messages)

    async def test_api_failure_fails_closed(self, caplog: pytest.LogCaptureFixture) -> None:
        bot = _make_bot()
        bot.application_info = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
        with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.authorization"):
            await resolve_fallback_owner_ids(bot, Authorizer())
        assert Authorizer().is_allowed(_make_member(user_id=OUTSIDER)) is False
        assert Authorizer().is_allowed(_make_member(user_id=OWNER)) is False
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    async def test_api_failure_is_retried_on_reconnect(self) -> None:
        """Fail-closed must not mean permanently locked out by one network blip.

        ``on_ready`` fires again on reconnect; the failed attempt releases the
        once-only flag so the next one can succeed.
        """
        bot = _make_bot(owner_id=OWNER)
        bot.application_info = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
        await resolve_fallback_owner_ids(bot, Authorizer())
        assert get_fallback_owner_ids() == set()  # nobody, for now

        bot.application_info = _make_bot(owner_id=OWNER).application_info  # network back
        await resolve_fallback_owner_ids(bot, Authorizer())
        assert get_fallback_owner_ids() == {OWNER}
        assert Authorizer().is_allowed(_make_member(user_id=OWNER)) is True

    async def test_uses_cached_application_when_available(self) -> None:
        """``bot.application`` is discord.py's cache — don't re-hit the API."""
        bot = _make_bot(owner_id=OWNER)
        bot.application = await bot.application_info()
        bot.application_info = AsyncMock(side_effect=AssertionError("should not be called"))
        await resolve_fallback_owner_ids(bot, Authorizer())
        assert get_fallback_owner_ids() == {OWNER}


# ---------------------------------------------------------------------------
# AC5 — buttons follow the same rule
# ---------------------------------------------------------------------------


class _DummyView(AuthorizedViewMixin, discord.ui.View):
    def __init__(self, authorizer: Authorizer | None) -> None:
        super().__init__(timeout=None)
        self._authorizer = authorizer


class TestUnwiredViewFollowsTheSameDefault:
    async def test_outsider_cannot_click(self) -> None:
        set_fallback_owner_ids({OWNER})
        view = _DummyView(authorizer=None)
        interaction = _make_interaction(_make_member(user_id=OUTSIDER))
        assert await view.interaction_check(interaction) is False
        interaction.response.send_message.assert_called_once()

    async def test_owner_can_click(self) -> None:
        set_fallback_owner_ids({OWNER})
        view = _DummyView(authorizer=None)
        interaction = _make_interaction(_make_member(user_id=OWNER))
        assert await view.interaction_check(interaction) is True

    async def test_allow_anyone_still_lets_everyone_click(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLORD_ALLOW_ANYONE", "1")
        view = _DummyView(authorizer=None)
        interaction = _make_interaction(_make_member(user_id=OUTSIDER))
        assert await view.interaction_check(interaction) is True


# ---------------------------------------------------------------------------
# AC1 — the other two gates (/skill, /clord-init) share the rule
# ---------------------------------------------------------------------------


def _make_skill_cog(**kwargs):
    from c_lord.cogs.skill_command import SkillCommandCog

    return SkillCommandCog(
        bot=MagicMock(),
        repo=MagicMock(),
        runner=MagicMock(),
        claude_channel_id=1,
        **kwargs,
    )


def _make_channel_cog(**kwargs):
    from c_lord.cogs.channel_repo import ChannelRepoCog

    return ChannelRepoCog(bot=MagicMock(), repo=MagicMock(), **kwargs)


def _make_chat_cog(**kwargs):
    from c_lord.cogs.claude_chat import ClaudeChatCog

    bot = MagicMock()
    bot.authorizer = None
    bot.session_registry = MagicMock()
    return ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), **kwargs)


class TestEveryGateSharesTheDefault:
    def test_skill_command_denies_outsider(self) -> None:
        set_fallback_owner_ids({OWNER})
        cog = _make_skill_cog()
        assert cog._is_authorized(_make_member(user_id=OUTSIDER)) is False
        assert cog._is_authorized(OUTSIDER) is False

    def test_skill_command_allows_owner(self) -> None:
        set_fallback_owner_ids({OWNER})
        cog = _make_skill_cog()
        assert cog._is_authorized(_make_member(user_id=OWNER)) is True
        assert cog._is_authorized(OWNER) is True

    def test_channel_repo_denies_outsider(self) -> None:
        set_fallback_owner_ids({OWNER})
        cog = _make_channel_cog()
        assert cog._is_allowed(_make_member(user_id=OUTSIDER)) is False
        assert cog._is_allowed(OUTSIDER) is False

    def test_channel_repo_allows_owner(self) -> None:
        set_fallback_owner_ids({OWNER})
        cog = _make_channel_cog()
        assert cog._is_allowed(_make_member(user_id=OWNER)) is True
        assert cog._is_allowed(OWNER) is True

    def test_chat_denies_outsider(self) -> None:
        set_fallback_owner_ids({OWNER})
        cog = _make_chat_cog()
        assert cog._is_allowed(_make_member(user_id=OUTSIDER)) is False
        assert cog._is_allowed(_make_member(user_id=OWNER)) is True

    def test_all_four_gates_share_one_authorizer_via_setup(self) -> None:
        """A cog handed an ``Authorizer`` uses *that* one, so resolving the
        owner once at startup reaches every gate (no per-cog copies)."""
        shared = Authorizer()
        skill = _make_skill_cog(authorizer=shared)
        channel = _make_channel_cog(authorizer=shared)
        chat = _make_chat_cog(authorizer=shared)
        assert skill._authorizer is shared
        assert channel._authorizer is shared
        assert chat._authorizer is shared


class TestAnnouncedOncePerProcess:
    """``on_ready`` fires on every reconnect — the startup line does not."""

    async def test_second_call_is_silent_and_does_not_refetch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bot = _make_bot(owner_id=OWNER)
        await resolve_fallback_owner_ids(bot, Authorizer())
        bot.application_info.reset_mock()
        with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.authorization"):
            await resolve_fallback_owner_ids(bot, Authorizer())
        bot.application_info.assert_not_called()
        assert caplog.records == []
        assert get_fallback_owner_ids() == {OWNER}
