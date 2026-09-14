"""A View must judge clicks against the allowlist the bot actually runs (#739).

#713 made "no allowlist configured" mean "the app owner only" instead of
"everyone".  That was right, but ``AuthorizedViewMixin`` reached it the wrong
way: a View built without an authorizer got a **blank** ``Authorizer()``, which
has no allowlist and therefore consulted the app-owner fallback — and
``resolve_fallback_owner_ids`` deliberately leaves that unresolved whenever an
allowlist *is* configured.  So on every deployment with ``DISCORD_OWNER_ID``
set, an un-wired View rejected **everyone**, the owner included:

    13:51:57 Authorization: using the configured allowlist (user_ids=[499163459418587176])
    13:57:36 Rejected unauthorized button interaction from user 499163459418587176 on AskView

Two things are pinned here, because either one alone would leave the hole:

* the **wiring** — every ``AskView`` construction path is handed the authorizer
  (the two ask bridges had simply never passed it);
* the **floor** — a View that misses the wiring anyway consults the process's
  own authorizer, so it enforces the configured allowlist instead of an empty
  one.  Still deny-by-default, just not deny-by-accident.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.discord_ui.authorization import (
    AuthorizedViewMixin,
    Authorizer,
    get_default_authorizer,
    set_default_authorizer,
    set_fallback_owner_ids,
)

OWNER = 499163459418587176  # the real id from the #739 report
OUTSIDER = 12345

_SRC = Path(__file__).resolve().parents[1] / "c_lord"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _member(user_id: int, role_names: list[str] | None = None) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = user_id
    roles: list[MagicMock] = []
    for name in role_names or []:
        r = MagicMock(spec=discord.Role)
        r.name = name
        roles.append(r)
    everyone = MagicMock(spec=discord.Role)
    everyone.name = "@everyone"
    roles.insert(0, everyone)
    m.roles = roles
    return m


def _interaction(user: MagicMock) -> MagicMock:
    i = MagicMock()
    i.user = user
    i.response.send_message = AsyncMock()
    return i


class _UnwiredView(AuthorizedViewMixin, discord.ui.View):
    """A View whose construction site forgot ``authorizer=``."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self._authorizer = None


# ---------------------------------------------------------------------------
# AC1 / AC2 — the regression itself
# ---------------------------------------------------------------------------


class TestAllowlistedUserCanClick:
    async def test_owner_on_the_configured_allowlist_can_click_an_unwired_view(self) -> None:
        """The #739 reproduction: prod's exact configuration."""
        set_default_authorizer(Authorizer(allowed_user_ids={OWNER}))
        view = _UnwiredView()
        interaction = _interaction(_member(OWNER))
        assert await view.interaction_check(interaction) is True
        interaction.response.send_message.assert_not_called()

    async def test_outsider_is_still_rejected(self) -> None:
        """The floor must not become a hole: only the allowlist passes."""
        set_default_authorizer(Authorizer(allowed_user_ids={OWNER}))
        view = _UnwiredView()
        interaction = _interaction(_member(OUTSIDER))
        assert await view.interaction_check(interaction) is False
        interaction.response.send_message.assert_called_once()

    async def test_role_allowlist_also_reaches_an_unwired_view(self) -> None:
        set_default_authorizer(Authorizer(allowed_role_name="claude-operator"))
        view = _UnwiredView()
        assert await view.interaction_check(_interaction(_member(1, ["claude-operator"]))) is True
        assert await view.interaction_check(_interaction(_member(2, ["other"]))) is False

    async def test_unconfigured_deployment_still_means_app_owner_only(self) -> None:
        """#713 is not undone: with nothing configured it is the owner, not all."""
        set_default_authorizer(Authorizer())
        set_fallback_owner_ids({OWNER})
        view = _UnwiredView()
        assert await view.interaction_check(_interaction(_member(OWNER))) is True
        assert await view.interaction_check(_interaction(_member(OUTSIDER))) is False

    async def test_no_authorizer_anywhere_denies(self) -> None:
        """Nothing to check against ⇒ deny (never 'allow because we don't know')."""
        set_default_authorizer(None)
        view = _UnwiredView()
        assert await view.interaction_check(_interaction(_member(OWNER))) is False

    async def test_the_views_own_authorizer_still_wins(self) -> None:
        set_default_authorizer(Authorizer(allowed_user_ids={OWNER}))

        class _Wired(AuthorizedViewMixin, discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=None)
                self._authorizer = Authorizer(allowed_user_ids={OUTSIDER})

        view = _Wired()
        assert await view.interaction_check(_interaction(_member(OUTSIDER))) is True
        assert await view.interaction_check(_interaction(_member(OWNER))) is False


# ---------------------------------------------------------------------------
# AC4 — ClaudeChatCog publishes the process authorizer
# ---------------------------------------------------------------------------


class TestCogPublishesTheAuthorizer:
    def test_constructing_the_chat_cog_publishes_it(self) -> None:
        from c_lord.cogs.claude_chat import ClaudeChatCog

        set_default_authorizer(None)
        bot = MagicMock()
        bot.authorizer = None
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock(), allowed_user_ids={OWNER})
        assert get_default_authorizer() is cog._authorizer
        assert bot.authorizer is cog._authorizer

    def test_an_existing_bot_authorizer_is_the_one_published(self) -> None:
        from c_lord.cogs.claude_chat import ClaudeChatCog

        set_default_authorizer(None)
        existing = Authorizer(allowed_user_ids={OWNER})
        bot = MagicMock()
        bot.authorizer = existing
        ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())
        assert get_default_authorizer() is existing


# ---------------------------------------------------------------------------
# AC3 — every AskView construction path passes an authorizer
# ---------------------------------------------------------------------------


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


ASK_VIEW_SOURCES = [
    "discord_ui/ask_handler.py",
    "ask_menu_recovery.py",
]
ASK_BRIDGE_CALLERS = [
    "cogs/transcript_mirror.py",
    "thread_state_sync.py",
]


@pytest.mark.parametrize("rel", ASK_VIEW_SOURCES)
def test_every_askview_construction_passes_an_authorizer(rel: str) -> None:
    """No ``AskView(...)`` anywhere may be built without ``authorizer=``.

    The three construction sites in #739 were not all wired, and nothing in the
    tree said they had to be.
    """
    tree = ast.parse((_SRC / rel).read_text())
    calls = _calls_named(tree, "AskView")
    assert calls, f"no AskView construction found in {rel} — did it move?"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "authorizer" in kwargs, (
            f"{rel}:{call.lineno} builds an AskView without authorizer= — "
            "a View that cannot see the allowlist rejects everyone (#739)"
        )


@pytest.mark.parametrize("rel", ASK_BRIDGE_CALLERS)
def test_every_ask_bridge_caller_passes_an_authorizer(rel: str) -> None:
    """``bridge_pane_ask`` forwards its ``authorizer`` into the AskView, so a
    caller that omits it produces exactly the un-wired View of #739."""
    tree = ast.parse((_SRC / rel).read_text())
    calls = _calls_named(tree, "bridge_pane_ask")
    assert calls, f"no bridge_pane_ask call found in {rel} — did it move?"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "authorizer" in kwargs, (
            f"{rel}:{call.lineno} calls bridge_pane_ask without authorizer= (#739)"
        )


# ---------------------------------------------------------------------------
# AC5 — a refusal says which kind of refusal it is
# ---------------------------------------------------------------------------


class TestDenialLogSaysWhy:
    async def test_not_on_the_configured_allowlist(self, caplog: pytest.LogCaptureFixture) -> None:
        set_default_authorizer(Authorizer(allowed_user_ids={OWNER}))
        view = _UnwiredView()
        with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.authorization"):
            await view.interaction_check(_interaction(_member(OUTSIDER)))
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "not on the configured allowlist" in text
        assert str(OWNER) in text  # who *is* allowed

    async def test_owner_not_resolved_yet(self, caplog: pytest.LogCaptureFixture) -> None:
        set_default_authorizer(Authorizer())
        set_fallback_owner_ids(None)
        view = _UnwiredView()
        with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.authorization"):
            await view.interaction_check(_interaction(_member(OUTSIDER)))
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "not resolved yet" in text

    async def test_no_authorizer_at_all_is_its_own_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        set_default_authorizer(None)
        view = _UnwiredView()
        with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.authorization"):
            await view.interaction_check(_interaction(_member(OWNER)))
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "none published for this process" in text

    async def test_says_when_the_view_was_not_wired(self, caplog: pytest.LogCaptureFixture) -> None:
        """Which of the two authorizers answered is the fact that was missing."""
        set_default_authorizer(Authorizer(allowed_user_ids={OWNER}))
        view = _UnwiredView()
        with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.authorization"):
            await view.interaction_check(_interaction(_member(OUTSIDER)))
        assert "view was not wired" in " ".join(r.getMessage() for r in caplog.records)
