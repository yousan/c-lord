"""Tests for button-level authorization (#466).

Discord ``discord.ui.View`` buttons run their callback for *anyone* who can
click them unless ``interaction_check`` is overridden.  c-lord restricts
messages / slash commands to an allowlist (``allowed_user_ids`` /
``allowed_role_name``) but historically left every interactive View wide open,
so a non-allowlisted user in a public thread could Approve / Allow / Stop.

These tests pin down:

* ``Authorizer.is_allowed`` — the extracted allowlist predicate (same
  semantics as ``ClaudeChatCog._is_allowed``).
* ``AuthorizedViewMixin.interaction_check`` — allow allowlisted users,
  reject others with an ephemeral notice, and (since #713) treat a View with no
  authorizer as "no allowlist configured", i.e. owner-only rather than open.
* Every real interactive View enforces the authorizer it is given.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.cogs.auto_upgrade import UpgradeApprovalView
from c_lord.discord_ui.ask_view import AskView
from c_lord.discord_ui.authorization import (
    AuthorizedViewMixin,
    Authorizer,
    set_fallback_owner_ids,
    set_process_authorizer,
)
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


def _ask_pane() -> str:
    """A real ``capture-pane`` of an open AskUserQuestion menu."""
    path = Path(__file__).parent / "fixtures" / "panes" / "ask_context_prose_above_menu.txt"
    return path.read_text()


def _make_interaction(user: MagicMock) -> MagicMock:
    interaction = MagicMock()
    interaction.user = user
    interaction.response.send_message = AsyncMock()
    return interaction


# ---------------------------------------------------------------------------
# Authorizer.is_allowed — same semantics as ClaudeChatCog._is_allowed
# ---------------------------------------------------------------------------


class TestAuthorizer:
    def test_no_allowlist_denies_everyone_but_the_app_owner(self) -> None:
        """#713: no allowlist means *the owner*, never *everyone*.

        Talking to c-lord runs a shell on its host, so "not configured" can
        not be the setting that hands that to the whole server.
        """
        auth = Authorizer(allowed_user_ids=None, allowed_role_name=None)
        assert auth.is_allowed(_make_member(user_id=123)) is False
        set_fallback_owner_ids({123})
        assert auth.is_allowed(_make_member(user_id=123)) is True
        assert auth.is_allowed(_make_member(user_id=124)) is False

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
    async def test_none_authorizer_falls_back_to_the_owner_default(self) -> None:
        """#713: an un-wired View is "nothing configured" — owner-only."""
        view = _DummyView(authorizer=None)
        assert await view.interaction_check(_make_interaction(_make_member(123))) is False
        set_fallback_owner_ids({123})
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

    async def test_no_authorizer_falls_back_to_the_owner_default(self, cls_name: str) -> None:
        """#713: not wired up ⇒ the unconfigured rule, which is owner-only.

        Every one of these buttons decides something on the session owner's
        behalf, so "we forgot to pass the authorizer" must not be the setting
        that lets any member of a public thread press it.
        """
        view = _build_view(cls_name, None)
        assert await view.interaction_check(_make_interaction(_make_member(99))) is False
        set_fallback_owner_ids({99})
        assert await view.interaction_check(_make_interaction(_make_member(99))) is True


# ---------------------------------------------------------------------------
# #739 — a View nobody handed the authorizer must still obey the real allowlist
# ---------------------------------------------------------------------------


class TestUnwiredViewFollowsTheConfiguredAllowlist:
    """#739: the owner pressing their own button got 「権限がありません」.

    Production, 2026-09-14 (``/tmp/clord-bot-c-lord-20260914-135153.log``)::

        13:51:57 Authorization: using the configured allowlist
                 (user_ids=[499163459418587176] role=None)
        13:57:02 menu watchdog: bridging unwatched TUI menu (thread=1548897737465004032 ...)
        13:57:36 Rejected unauthorized button interaction from user 499163459418587176 on AskView

    The rejected ID *is* the configured allowlist.  The menu came from the #359
    watchdog, which called ``bridge_pane_ask`` without an authorizer, so the
    ``AskView`` fell through to ``interaction_check``'s ``or Authorizer()`` —
    a fresh, argument-less predicate that knows nothing about the configured
    allowlist and, since #713, denies rather than opens up.

    The fix is not to re-open the fallback (that is the fail-open #713 closed):
    the fallback must reach **the process's own authorizer**, the one holding
    the configured allowlist.
    """

    def _view(self):
        question = AskQuestion(question="pick one", options=[AskOption("A"), AskOption("B")])
        return AskView(question, thread_id=1, q_idx=0)  # exactly what the watchdog builds

    async def test_configured_allowlist_reaches_a_view_that_was_not_handed_it(self) -> None:
        set_process_authorizer(Authorizer(allowed_user_ids={499163459418587176}))
        view = self._view()
        interaction = _make_interaction(_make_member(user_id=499163459418587176))
        assert await view.interaction_check(interaction) is True
        interaction.response.send_message.assert_not_called()

    async def test_outsider_is_still_rejected(self) -> None:
        set_process_authorizer(Authorizer(allowed_user_ids={499163459418587176}))
        view = self._view()
        interaction = _make_interaction(_make_member(user_id=99))
        assert await view.interaction_check(interaction) is False
        interaction.response.send_message.assert_called_once()

    async def test_a_wired_authorizer_still_wins(self) -> None:
        """The process fallback is a backstop, never an override."""
        set_process_authorizer(Authorizer(allowed_user_ids={1}))
        question = AskQuestion(question="pick one", options=[AskOption("A")])
        view = AskView(question, thread_id=1, q_idx=0, authorizer=Authorizer(allowed_user_ids={2}))
        assert await view.interaction_check(_make_interaction(_make_member(2))) is True
        assert await view.interaction_check(_make_interaction(_make_member(1))) is False

    async def test_no_process_authorizer_still_means_owner_only(self) -> None:
        """#713 must not regress: nothing configured anywhere ⇒ owner only."""
        view = self._view()
        assert await view.interaction_check(_make_interaction(_make_member(99))) is False
        set_fallback_owner_ids({99})
        assert await view.interaction_check(_make_interaction(_make_member(99))) is True


class TestDenialSaysWhy:
    """AC5 — the rejection log must name the branch that refused.

    Both failures below wrote the *same* line in production, which is why
    telling "the owner is not on the list" apart from "this View never got the
    list" took an incident's worth of reading.
    """

    async def test_not_on_the_configured_allowlist(self, caplog) -> None:
        view = _build_view("AskView", Authorizer(allowed_user_ids={42}))
        with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.authorization"):
            await view.interaction_check(_make_interaction(_make_member(99)))
        assert "not on the configured allowlist" in caplog.text

    async def test_owner_fallback_unresolved(self, caplog) -> None:
        view = _build_view("AskView", None)
        with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.authorization"):
            await view.interaction_check(_make_interaction(_make_member(99)))
        assert "no allowlist is configured" in caplog.text
        assert "owner" in caplog.text


# ---------------------------------------------------------------------------
# #739 AC3/AC4 — every path that builds an AskView hands it the authorizer
# ---------------------------------------------------------------------------


class TestEveryAskViewPathIsWired:
    """No construction site may leave the allowlist behind.

    ``AskView`` is built from four places, and the two the watchdogs use passed
    nothing.  These drive the real call paths rather than reading the source,
    so a new path that forgets is caught by the same assertion.
    """

    async def test_menu_watchdog_passes_the_authorizer(self) -> None:
        """#359 sweep — the path that produced the 2026-09-14 incident."""
        from c_lord import thread_state_sync
        from c_lord.discord_ui import ask_handler

        authorizer = Authorizer(allowed_user_ids={42})
        bot = MagicMock()
        bot.get_cog.return_value = None
        bot.authorizer = authorizer
        bot.ask_repo = None
        bot.tmux_manager = MagicMock()
        bot.tmux_manager.capture_pane_tall = MagicMock(return_value="")
        bot.get_channel.return_value = MagicMock(spec=discord.Thread)
        loop = thread_state_sync.MenuWatchdogLoop(bot, interval_seconds=60)

        pane = _ask_pane()
        bridge = AsyncMock()
        with (
            patch.object(ask_handler, "bridge_pane_ask", bridge),
            patch.object(thread_state_sync, "_capture_pane_text", return_value=pane),
            patch.object(thread_state_sync, "_pane_foreground_command", return_value="claude"),
        ):
            await loop._maybe_bridge_open_menu(1, "sess", "w1", pane)
            await asyncio.sleep(0)
            task = loop._ask_bridges.get(1)
            if task is not None:
                await task

        bridge.assert_awaited_once()
        assert bridge.await_args.kwargs.get("authorizer") is authorizer

    async def test_transcript_mirror_ask_bridge_passes_the_authorizer(self) -> None:
        """#232 mirror bridge — same omission, same consequence."""
        from c_lord.cogs import transcript_mirror as tm
        from c_lord.discord_ui import ask_handler

        authorizer = Authorizer(allowed_user_ids={42})
        bot = MagicMock()
        bot.get_cog.return_value = None
        bot.authorizer = authorizer
        bot.ask_repo = None
        bot.tmux_manager = MagicMock()

        cog = tm.TranscriptMirrorCog.__new__(tm.TranscriptMirrorCog)
        cog.bot = bot
        thread = MagicMock(spec=discord.Thread)
        thread.parent_id = 7

        bridge = AsyncMock()
        with (
            patch.object(ask_handler, "bridge_pane_ask", bridge),
            patch.object(
                tm.TranscriptMirrorCog, "_resolve_channel", AsyncMock(return_value=thread)
            ),
        ):
            await cog._make_ask_bridge(1)(MagicMock())

        bridge.assert_awaited_once()
        assert bridge.await_args.kwargs.get("authorizer") is authorizer

    async def test_restart_recovery_passes_the_authorizer(self) -> None:
        """#671 re-arm — already wired; pinned so it stays that way."""
        from c_lord import ask_menu_recovery

        authorizer = Authorizer(allowed_user_ids={42})
        bot = MagicMock()
        bot.authorizer = authorizer
        bot.get_channel.return_value = MagicMock(spec=discord.Thread)
        record = MagicMock()
        record.thread_id = 1
        record.question_idx = 0
        record.message_id = None
        record.questions.return_value = [
            {"question": "pick one", "options": [{"label": "A"}], "header": "h"}
        ]
        repo = MagicMock()
        repo.delete = AsyncMock()

        captured: list = []
        real_ask_view = ask_menu_recovery.AskView

        def _spy(*args, **kwargs):
            captured.append(kwargs.get("authorizer"))
            return real_ask_view(*args, **kwargs)

        async def _no_runner(_tid):
            return None

        with patch.object(ask_menu_recovery, "AskView", _spy):
            await ask_menu_recovery._recover_one(bot, repo, record, _no_runner)

        assert captured == [authorizer]
