"""Tests for button-level authorization (#466).

Discord ``discord.ui.View`` buttons run their callback for *anyone* who can
click them unless ``interaction_check`` is overridden.  c-lord restricts
messages / slash commands to an allowlist (``allowed_user_ids`` /
``allowed_role_name``) but historically left every interactive View wide open,
so a non-allowlisted user in a public thread could Approve / Allow / Stop.

These tests pin down:

* ``Authorizer.is_allowed`` — the extracted allowlist predicate (same
  semantics as ``ClaudeChatCog._is_allowed``).
* ``AuthorizedViewMixin.interaction_check`` — allow when no authorizer
  (zero-config), allow allowlisted users, reject others with an ephemeral
  notice.
* Every real interactive View enforces the authorizer it is given.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.cogs.auto_upgrade import UpgradeApprovalView
from c_lord.discord_ui.ask_view import AskView
from c_lord.discord_ui.authorization import AuthorizedViewMixin, Authorizer
from c_lord.discord_ui.elicitation_view import ElicitationFormView, ElicitationUrlView
from c_lord.discord_ui.permission_view import PermissionView
from c_lord.discord_ui.plan_view import PlanApprovalView
from c_lord.discord_ui.views import ReopenSessionView, StopView

# ---------------------------------------------------------------------------
# Mock helpers (mirror tests/test_role_access.py)
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


# ---------------------------------------------------------------------------
# Authorizer.is_allowed — same semantics as ClaudeChatCog._is_allowed
# ---------------------------------------------------------------------------


class TestAuthorizer:
    def test_no_allowlist_allows_everyone(self) -> None:
        auth = Authorizer(allowed_user_ids=None, allowed_role_name=None)
        assert auth.is_allowed(_make_member(user_id=123)) is True

    def test_allowed_by_user_id(self) -> None:
        auth = Authorizer(allowed_user_ids={42})
        assert auth.is_allowed(_make_member(user_id=42)) is True

    def test_denied_by_user_id(self) -> None:
        auth = Authorizer(allowed_user_ids={42})
        assert auth.is_allowed(_make_member(user_id=99)) is False

    def test_allowed_by_role(self) -> None:
        auth = Authorizer(allowed_role_name="claude-operator")
        member = _make_member(user_id=99, role_names=["claude-operator"])
        assert auth.is_allowed(member) is True

    def test_denied_without_role(self) -> None:
        auth = Authorizer(allowed_role_name="claude-operator")
        member = _make_member(user_id=99, role_names=["other"])
        assert auth.is_allowed(member) is False

    def test_role_check_rejects_plain_user(self) -> None:
        # A discord.User (DM, no roles) can never satisfy a role requirement.
        auth = Authorizer(allowed_role_name="claude-operator")
        assert auth.is_allowed(_make_user(user_id=99)) is False

    def test_user_id_or_role_is_or_logic(self) -> None:
        auth = Authorizer(allowed_user_ids={42}, allowed_role_name="ops")
        # matches by id, lacks role
        assert auth.is_allowed(_make_member(user_id=42, role_names=["x"])) is True
        # matches by role, wrong id
        assert auth.is_allowed(_make_member(user_id=7, role_names=["ops"])) is True


# ---------------------------------------------------------------------------
# AuthorizedViewMixin.interaction_check
# ---------------------------------------------------------------------------


class _DummyView(AuthorizedViewMixin, discord.ui.View):
    def __init__(self, authorizer: Authorizer | None) -> None:
        super().__init__(timeout=None)
        self._authorizer = authorizer


class TestInteractionCheck:
    async def test_none_authorizer_allows_everyone(self) -> None:
        view = _DummyView(authorizer=None)
        interaction = _make_interaction(_make_member(user_id=123))
        assert await view.interaction_check(interaction) is True
        interaction.response.send_message.assert_not_called()

    async def test_allowlisted_user_passes(self) -> None:
        view = _DummyView(authorizer=Authorizer(allowed_user_ids={42}))
        interaction = _make_interaction(_make_member(user_id=42))
        assert await view.interaction_check(interaction) is True
        interaction.response.send_message.assert_not_called()

    async def test_outsider_rejected_with_ephemeral(self) -> None:
        view = _DummyView(authorizer=Authorizer(allowed_user_ids={42}))
        interaction = _make_interaction(_make_member(user_id=99))
        assert await view.interaction_check(interaction) is False
        interaction.response.send_message.assert_called_once()
        # The rejection notice must be ephemeral (only the clicker sees it).
        _, kwargs = interaction.response.send_message.call_args
        assert kwargs.get("ephemeral") is True


# ---------------------------------------------------------------------------
# Every interactive View enforces its authorizer (#466 — no View left open)
# ---------------------------------------------------------------------------


def _build_view(cls_name: str, authorizer: Authorizer | None):
    runner = MagicMock()
    if cls_name == "PermissionView":
        return PermissionView(runner, MagicMock(), authorizer=authorizer)
    if cls_name == "PlanApprovalView":
        return PlanApprovalView(runner, "req-1", authorizer=authorizer)
    if cls_name == "ElicitationUrlView":
        req = MagicMock()
        req.url = None  # skip the link-button branch
        return ElicitationUrlView(runner, req, authorizer=authorizer)
    if cls_name == "ElicitationFormView":
        return ElicitationFormView(runner, MagicMock(), authorizer=authorizer)
    if cls_name == "StopView":
        return StopView(runner, authorizer=authorizer)
    if cls_name == "ReopenSessionView":
        # #512: reopening a closed session resumes work, so it must be gated by
        # the same allowlist as sending a message — not clickable by any member.
        return ReopenSessionView(AsyncMock(), authorizer=authorizer)
    if cls_name == "AskView":
        question = AskQuestion(
            question="pick one",
            options=[AskOption("A"), AskOption("B")],
        )
        return AskView(question, thread_id=1, q_idx=0, authorizer=authorizer)
    if cls_name == "UpgradeApprovalView":
        return UpgradeApprovalView(
            approved_event=asyncio.Event(), bot_id=None, authorizer=authorizer
        )
    raise AssertionError(f"unknown view {cls_name}")


ALL_VIEWS = [
    "PermissionView",
    "PlanApprovalView",
    "ElicitationUrlView",
    "ElicitationFormView",
    "StopView",
    "ReopenSessionView",
    "AskView",
    "UpgradeApprovalView",
]


@pytest.mark.parametrize("cls_name", ALL_VIEWS)
class TestEveryViewEnforcesAuthorizer:
    async def test_outsider_rejected(self, cls_name: str) -> None:
        view = _build_view(cls_name, Authorizer(allowed_user_ids={42}))
        interaction = _make_interaction(_make_member(user_id=99))
        assert await view.interaction_check(interaction) is False
        interaction.response.send_message.assert_called_once()

    async def test_allowlisted_user_passes(self, cls_name: str) -> None:
        view = _build_view(cls_name, Authorizer(allowed_user_ids={42}))
        interaction = _make_interaction(_make_member(user_id=42))
        assert await view.interaction_check(interaction) is True

    async def test_no_authorizer_allows_everyone(self, cls_name: str) -> None:
        view = _build_view(cls_name, None)
        interaction = _make_interaction(_make_member(user_id=99))
        assert await view.interaction_check(interaction) is True
