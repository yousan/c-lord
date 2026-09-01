"""#525: how loudly c-lord falls back to the owner when nobody human asked.

A webhook/CI/scheduled turn has no person behind it, so the mention falls back
to ``DISCORD_OWNER_ID``. Whether that is welcome depends entirely on the
deployment: a server running many automated threads drowns its owner in
"Claude has finished" pings, while a quiet server wants to know when a turn is
stuck. ``CLORD_OWNER_FALLBACK`` picks the policy.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.notify_policy import owner_fallback_allowed, owner_fallback_mode, owner_notify_id

OWNER_ID = 7777


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLORD_OWNER_FALLBACK", raising=False)


class TestMode:
    def test_defaults_to_blocked(self) -> None:
        assert owner_fallback_mode() == "blocked"

    @pytest.mark.parametrize("raw", ["all", "ALL", " all ", "off", "blocked"])
    def test_accepts_the_documented_values(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", raw)
        assert owner_fallback_mode() == raw.strip().lower()

    @pytest.mark.parametrize("raw", ["", "yes", "loud", "1"])
    def test_unknown_values_fall_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", raw)
        assert owner_fallback_mode() == "blocked"


class TestAllowedMatrix:
    """AC2 / AC3 / AC4 — the whole table, one assert per cell."""

    def test_all_pings_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "all")
        assert owner_fallback_allowed("completion") is True
        assert owner_fallback_allowed("blocked") is True

    def test_blocked_pings_only_a_stuck_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "blocked")
        assert owner_fallback_allowed("completion") is False
        assert owner_fallback_allowed("blocked") is True

    def test_off_pings_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "off")
        assert owner_fallback_allowed("completion") is False
        assert owner_fallback_allowed("blocked") is False


class TestOwnerNotifyId:
    def test_returns_the_owner_when_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "all")
        bot = MagicMock()
        bot.owner_id = OWNER_ID
        assert owner_notify_id(bot, kind="completion") == OWNER_ID

    def test_returns_none_when_the_policy_forbids_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "off")
        bot = MagicMock()
        bot.owner_id = OWNER_ID
        assert owner_notify_id(bot, kind="blocked") is None

    def test_returns_none_without_a_configured_owner(self) -> None:
        bot = MagicMock()
        bot.owner_id = None
        assert owner_notify_id(bot, kind="blocked") is None


# ---------------------------------------------------------------------------
# End-to-end through _run_claude: what actually reaches Discord
# ---------------------------------------------------------------------------


class _Stop(BaseException):
    """Sentinel raised to halt _run_claude right after the call under test."""


def _human(user_id: int = 4242) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.display_name = "yousan"
    u.name = "yousan"
    u.bot = False
    return u


def _webhook_user(user_id: int = 9999) -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.display_name = "かたぼ (PM)"
    u.name = "かたぼ (PM)"
    u.bot = True
    return u


def _message(author: MagicMock) -> MagicMock:
    m = MagicMock(spec=discord.Message)
    m.id = 77
    m.author = author
    m.add_reaction = AsyncMock()
    m.remove_reaction = AsyncMock()
    m.clear_reaction = AsyncMock()
    return m


def _make_cog() -> ClaudeChatCog:
    bot = MagicMock()
    bot.channel_id = 999
    bot.owner_id = OWNER_ID
    bot.settings_repo = None
    bot.transcript_mirror_cog = None
    bot.user = MagicMock(id=1111)
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    repo.update_trigger_message = AsyncMock()
    runner = MagicMock()
    runner.working_dir = "/tmp/work"
    runner.model = None
    runner.timeout_seconds = 60
    runner.effort = None
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner)


async def _run_turn(cog: ClaudeChatCog, message: MagicMock) -> dict:
    """Drive ``_run_claude`` far enough to capture both mention decisions."""
    sdm = MagicMock()
    sdm.create_session_dir = MagicMock(return_value="/tmp/work")
    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w1")
    dashboard = MagicMock()
    dashboard.set_state = AsyncMock()

    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    cog._get_dashboard = MagicMock(return_value=dashboard)  # type: ignore[method-assign]
    cog._get_coordination = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_current_model = AsyncMock(return_value=None)  # type: ignore[method-assign]
    cog._apply_thread_naming = AsyncMock()  # type: ignore[method-assign]

    thread = MagicMock(spec=discord.Thread)
    thread.id = 501
    thread.parent_id = 500
    thread.send = AsyncMock(return_value=MagicMock())

    run_config = AsyncMock(side_effect=_Stop)
    with (
        patch("c_lord.cogs.claude_chat.run_claude_with_config", run_config),
        contextlib.suppress(BaseException),
    ):
        await cog._run_claude(message, thread, "hi", None)

    call = run_config.await_args
    assert call is not None, "Claude must be run"
    waiting = dashboard.set_state.await_args_list[-1]
    return {
        "blocked_mention": call.args[0].notify_user_id,
        "completion_mention": waiting.kwargs["notify_user_id"],
    }


class TestPolicyReachesTheTurn:
    async def test_off_silences_a_webhook_turn_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2 — the mode this server wants."""
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "off")
        seen = await _run_turn(_make_cog(), _message(_webhook_user()))
        assert seen["blocked_mention"] is None
        assert seen["completion_mention"] is None

    async def test_blocked_pings_only_when_the_turn_is_stuck(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 — the default."""
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "blocked")
        seen = await _run_turn(_make_cog(), _message(_webhook_user()))
        assert seen["blocked_mention"] == OWNER_ID
        assert seen["completion_mention"] is None

    async def test_all_keeps_the_previous_behaviour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC4."""
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", "all")
        seen = await _run_turn(_make_cog(), _message(_webhook_user()))
        assert seen["blocked_mention"] == OWNER_ID
        assert seen["completion_mention"] == OWNER_ID

    @pytest.mark.parametrize("mode", ["all", "blocked", "off"])
    async def test_a_human_turn_always_reaches_that_human(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        """AC5 — the policy only governs the *fallback*, never a real poster."""
        monkeypatch.setenv("CLORD_OWNER_FALLBACK", mode)
        human = _human()
        seen = await _run_turn(_make_cog(), _message(human))
        assert seen["blocked_mention"] == human.id
        assert seen["completion_mention"] == human.id
