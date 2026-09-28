"""Tests for 「このスレッドは別の c-lord の担当です」 — #811.

Two c-lord bots in one guild register the same slash-command names, so Discord
lists every command once per bot. Picking the *other* bot's entry in a thread
used to get an answer from a bot that only looked at its own ``sessions`` table:
「ワークスペースがありません」 / 「メッセージを送っても復元できません（c-lord の
記録が見つかりません）」 — about a thread the owning bot was working in at that
very moment. These tests pin that the non-owning bot names the owner instead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.database.repository import SessionRecord
from c_lord.session_resume import ThreadResume, stopped_hint
from c_lord.thread_owner import foreign_owner_id, foreign_owner_notice

ME = 1475105094071750818
OTHER_CLORD = 1517328788164182116
HUMAN = 900000000000000001


def _said(mock: AsyncMock) -> str:
    assert mock.await_args is not None, "nothing was sent"
    if mock.await_args.args:
        return str(mock.await_args.args[0])
    return str(mock.await_args.kwargs.get("content", ""))


def _record() -> SessionRecord:
    return SessionRecord(
        thread_id=42,
        session_id="sess-abc",
        working_dir="/tmp/x",
        model=None,
        origin="discord",
        summary=None,
        created_at="2026-09-25 10:00:00",
        last_used_at="2026-09-25 11:00:00",
        closed_at=None,
    )


def _bot(*, owner_is_bot: bool = True) -> MagicMock:
    bot = MagicMock()
    bot.channel_id = 999
    bot.user.id = ME
    bot.get_cog = MagicMock(return_value=None)
    owner = MagicMock()
    owner.bot = owner_is_bot
    bot.get_user = MagicMock(return_value=owner)
    bot.fetch_user = AsyncMock(return_value=owner)
    return bot


def _repo(record: SessionRecord | None = None) -> MagicMock:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=record)
    return repo


def _thread(owner_id: object = OTHER_CLORD) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = 42
    thread.parent_id = 999
    thread.owner_id = owner_id
    thread.name = "機能実装について"
    return thread


# ── the decision ─────────────────────────────────────────────────────────────


class TestForeignOwnerId:
    @pytest.mark.asyncio
    async def test_other_bots_thread_without_our_row_is_foreign(self) -> None:
        assert await foreign_owner_id(_bot(), _repo(), _thread()) == OTHER_CLORD

    @pytest.mark.asyncio
    async def test_our_own_thread_is_not_foreign(self) -> None:
        """AC3: our thread with no row keeps the #538 wording — not this one."""
        assert await foreign_owner_id(_bot(), _repo(), _thread(ME)) is None

    @pytest.mark.asyncio
    async def test_a_row_means_it_is_ours_whoever_made_the_thread(self) -> None:
        assert await foreign_owner_id(_bot(), _repo(_record()), _thread()) is None

    @pytest.mark.asyncio
    async def test_human_made_thread_is_not_another_clords(self) -> None:
        """A person's thread has no other c-lord to point at (#556's Grafana thread)."""
        bot = _bot(owner_is_bot=False)
        assert await foreign_owner_id(bot, _repo(), _thread(HUMAN)) is None

    @pytest.mark.asyncio
    async def test_uncached_owner_is_fetched(self) -> None:
        bot = _bot()
        bot.get_user = MagicMock(return_value=None)
        assert await foreign_owner_id(bot, _repo(), _thread()) == OTHER_CLORD
        bot.fetch_user.assert_awaited_once_with(OTHER_CLORD)

    @pytest.mark.asyncio
    async def test_unknowable_owner_falls_back_to_old_behaviour(self) -> None:
        bot = _bot()
        bot.get_user = MagicMock(return_value=None)
        bot.fetch_user = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
        assert await foreign_owner_id(bot, _repo(), _thread()) is None

    @pytest.mark.asyncio
    async def test_db_error_falls_back_to_old_behaviour(self) -> None:
        repo = MagicMock()
        repo.get = AsyncMock(side_effect=RuntimeError("db locked"))
        assert await foreign_owner_id(_bot(), repo, _thread()) is None

    @pytest.mark.asyncio
    async def test_not_a_thread(self) -> None:
        assert await foreign_owner_id(_bot(), _repo(), MagicMock(spec=discord.TextChannel)) is None

    @pytest.mark.asyncio
    async def test_missing_owner_id(self) -> None:
        assert await foreign_owner_id(_bot(), _repo(), _thread(None)) is None


class TestNotice:
    def test_names_the_owner_and_denies_nothing(self) -> None:
        text = foreign_owner_notice(OTHER_CLORD)
        assert f"<@{OTHER_CLORD}>" in text
        for false_claim in ("復元できません", "記録が見つかりません", "ワークスペースがありません"):
            assert false_claim not in text


# ── wired into the commands (AC1 / AC2 / AC3) ────────────────────────────────


def _manage_cog(*, bot: MagicMock | None = None, record: SessionRecord | None = None):
    from c_lord.cogs.session_manage import SessionManageCog

    return SessionManageCog(bot=bot or _bot(), repo=_repo(record))


def _assert_names_owner(text: str) -> None:
    assert f"<@{OTHER_CLORD}>" in text
    for false_claim in ("復元できません", "記録が見つかりません", "ワークスペースがありません"):
        assert false_claim not in text


class TestCommandsNameTheOwner:
    @pytest.mark.asyncio
    async def test_screenshot_in_other_clords_thread(self) -> None:
        """AC1."""
        cog = _manage_cog()
        tmux = MagicMock()
        tmux.window_name = MagicMock(return_value=None)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        respond, ack = AsyncMock(), AsyncMock()

        await cog._screenshot_impl(channel=_thread(), respond=respond, ack=ack)

        _assert_names_owner(_said(respond))

    @pytest.mark.asyncio
    async def test_screenshot_in_our_untracked_thread_is_unchanged(self) -> None:
        """AC3."""
        cog = _manage_cog()
        tmux = MagicMock()
        tmux.window_name = MagicMock(return_value=None)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
        respond, ack = AsyncMock(), AsyncMock()

        await cog._screenshot_impl(channel=_thread(ME), respond=respond, ack=ack)

        assert _said(respond) == stopped_hint(ThreadResume.UNTRACKED)

    @pytest.mark.asyncio
    async def test_resync_in_other_clords_thread(self) -> None:
        cog = _manage_cog()
        cog._find_thread_window = AsyncMock(return_value=(None, None))
        respond, ack = AsyncMock(), AsyncMock()

        await cog._resync_impl(channel=_thread(), respond=respond, ack=ack)

        _assert_names_owner(_said(respond))

    @pytest.mark.asyncio
    async def test_workspace_start_in_other_clords_thread(self) -> None:
        """AC2."""
        cog = _manage_cog()
        respond, ack = AsyncMock(), AsyncMock()

        await cog._reopen_workspace_impl(channel=_thread(), respond=respond, ack=ack)

        _assert_names_owner(_said(respond))

    @pytest.mark.asyncio
    async def test_workspace_start_in_our_untracked_thread_is_unchanged(self) -> None:
        cog = _manage_cog()
        respond, ack = AsyncMock(), AsyncMock()

        await cog._reopen_workspace_impl(channel=_thread(ME), respond=respond, ack=ack)

        assert "ワークスペースがありません" in _said(respond)

    @pytest.mark.asyncio
    async def test_workspace_stop_does_not_archive_other_clords_thread(self) -> None:
        """Stopping is ours to do only in our threads — it archives the thread."""
        cog = _manage_cog()
        cog._stop_transcript_mirror = AsyncMock()
        cog._resolve_tmux_manager = AsyncMock()
        thread = _thread()
        thread.edit = AsyncMock()
        respond, ack = AsyncMock(), AsyncMock()

        await cog._close_workspace_impl(channel=thread, respond=respond, ack=ack)

        _assert_names_owner(_said(respond))
        thread.edit.assert_not_awaited()
        cog._resolve_tmux_manager.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_workspace_delete_does_not_report_a_deletion(self) -> None:
        cog = _manage_cog()
        cog._stop_transcript_mirror = AsyncMock()
        cog._resolve_tmux_manager = AsyncMock()
        cog._resolve_session_dir_manager = AsyncMock()
        respond, ack = AsyncMock(), AsyncMock()

        await cog._workspace_delete_impl(channel=_thread(), respond=respond, ack=ack)

        _assert_names_owner(_said(respond))
        cog._resolve_tmux_manager.assert_not_awaited()
        cog._resolve_session_dir_manager.assert_not_awaited()


# ── the thread commands that reach into a workspace (claude_chat) ────────────


def _chat_cog():
    from c_lord.cogs.claude_chat import ClaudeChatCog
    from c_lord.discord_ui.authorization import Authorizer

    tmux = MagicMock()
    tmux.is_claude_running = MagicMock(return_value=True)
    tmux.kill_session = MagicMock()
    channel_cog = MagicMock()
    channel_cog.resolve_tmux_manager = AsyncMock(return_value=tmux)
    channel_cog.resolve_manager = AsyncMock(return_value=None)
    bot = _bot()
    bot.get_cog = MagicMock(return_value=channel_cog)
    repo = _repo()
    repo.reset = AsyncMock(return_value=False)
    cog = ClaudeChatCog(
        bot=bot, repo=repo, runner=MagicMock(), authorizer=Authorizer(allow_anyone=True)
    )
    return cog, tmux


class TestChatCommandsKeepOffOtherClordsThread:
    """Where two bots share a tmux server and a repo, these would reach the
    owner's window — so the non-owner must stop before touching tmux at all."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("impl", ["_stop_impl", "_restart_impl", "_compact_impl"])
    async def test_names_owner_and_touches_nothing(self, impl: str) -> None:
        cog, tmux = _chat_cog()
        respond = AsyncMock()

        await getattr(cog, impl)(_thread(), respond)

        _assert_names_owner(_said(respond))
        tmux.kill_session.assert_not_called()
        tmux.is_claude_running.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear(self) -> None:
        cog, tmux = _chat_cog()
        respond = AsyncMock()

        await cog._clear_impl(_thread(), respond, user=MagicMock())

        _assert_names_owner(_said(respond))
        tmux.kill_session.assert_not_called()
        cog.repo.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reattach(self) -> None:
        cog, _ = _chat_cog()
        cog._reattach_thread = AsyncMock()
        interaction = MagicMock(spec=discord.Interaction)
        interaction.channel = _thread()
        interaction.user = MagicMock()
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()

        await cog.clord_reattach.callback(cog, interaction)

        _assert_names_owner(_said(interaction.response.send_message))
        cog._reattach_thread.assert_not_awaited()
