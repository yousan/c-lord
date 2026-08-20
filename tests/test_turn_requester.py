"""#520: who c-lord pings and credits for a turn (= the turn's *requester*).

``/clord`` and ``POST /api/spawn`` seed the thread with a message **c-lord
itself** posts, so the author of the trigger message is not the person behind
the turn.  Reading the requester off that author made the completion mention
(#481), the interactive-prompt mention (#480) and the ``Co-authored-by``
trailer (#519) all name the bot.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from c_lord.cogs.claude_chat import ClaudeChatCog, _notify_target, _requester_of_turn

OWNER_ID = 7777


def _human(user_id: int = 4242, name: str = "yousan") -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.display_name = name
    u.name = name
    u.bot = False
    return u


def _bot_user(user_id: int = 1111, name: str = "C-lord") -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.display_name = name
    u.name = name
    u.bot = True
    return u


def _bot_host(self_id: int = 1111) -> MagicMock:
    """A bot object whose own user id is *self_id* (c-lord itself)."""
    bot = MagicMock()
    bot.user = MagicMock(id=self_id)
    bot.owner_id = OWNER_ID
    return bot


def _message(author: MagicMock, message_id: int = 77) -> MagicMock:
    m = MagicMock(spec=discord.Message)
    m.id = message_id
    m.author = author
    m.add_reaction = AsyncMock()
    m.remove_reaction = AsyncMock()
    m.clear_reaction = AsyncMock()
    return m


class TestRequesterOfTurn:
    """Who asked for the turn — used for the ``Co-authored-by`` credit."""

    def test_trigger_author_is_the_requester_for_a_normal_message(self) -> None:
        author = _human()
        assert _requester_of_turn(_message(author), None, _bot_host()) is author

    def test_explicit_requester_wins_over_our_own_seed_message(self) -> None:
        """/clord: the seed message is ours, the invoker is the requester."""
        invoker = _human()
        assert _requester_of_turn(_message(_bot_user()), invoker, _bot_host()) is invoker

    def test_our_own_seed_without_an_invoker_has_no_requester(self) -> None:
        """/api/spawn: nobody in Discord asked for this turn."""
        assert _requester_of_turn(_message(_bot_user()), None, _bot_host()) is None

    def test_a_foreign_webhook_or_bot_is_still_the_requester(self) -> None:
        """#519 records CI/companion-bot provenance — only *our* seed is nobody."""
        webhook = _bot_user(user_id=9999, name="CI")
        assert _requester_of_turn(_message(webhook), None, _bot_host()) is webhook

    def test_our_own_user_id_is_recognised_without_a_bot_flag(self) -> None:
        """Identity is by user id, so a missing ``.bot`` attribute is harmless."""
        me = MagicMock()
        me.id = 1111
        del me.bot
        assert _requester_of_turn(_message(me), None, _bot_host()) is None


class TestNotifyTarget:
    """Who gets @-mentioned — only a human reads a ping."""

    def test_a_human_requester_is_mentioned(self) -> None:
        human = _human()
        assert _notify_target(human, _bot_host()) == human.id

    def test_a_webhook_or_bot_requester_falls_back_to_the_owner(self) -> None:
        assert _notify_target(_bot_user(user_id=9999), _bot_host()) == OWNER_ID

    def test_no_requester_falls_back_to_the_owner(self) -> None:
        assert _notify_target(None, _bot_host()) == OWNER_ID


class _Stop(BaseException):
    """Sentinel raised to halt _run_claude right after the call under test."""


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


async def _run_turn(cog: ClaudeChatCog, message: MagicMock, **kwargs: object) -> dict:
    """Drive ``_run_claude`` up to (and including) the RunConfig it builds."""
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
        await cog._run_claude(message, thread, "hi", None, **kwargs)  # type: ignore[arg-type]

    create_call = sdm.create_session_dir.call_args
    run_call = run_config.await_args
    assert create_call is not None, "the session dir must be created"
    assert run_call is not None, "Claude must be run"
    return {
        "coauthor": create_call.args[1],
        "config": run_call.args[0],
        "dashboard": dashboard,
    }


class TestRunClaudeUsesTheRequester:
    async def test_clord_pings_and_credits_the_invoker_not_the_bot(self) -> None:
        """AC1 / AC2 / AC3: the /clord invoker, even though we posted the seed."""
        cog = _make_cog()
        invoker = _human()

        seen = await _run_turn(cog, _message(_bot_user()), requester=invoker)

        assert seen["config"].notify_user_id == invoker.id
        waiting = seen["dashboard"].set_state.await_args_list[-1]
        assert waiting.kwargs["notify_user_id"] == invoker.id
        assert seen["coauthor"] is invoker

    async def test_our_own_seed_without_an_invoker_falls_back_to_the_owner(self) -> None:
        """AC4: /api/spawn — ping the owner, credit no Discord user."""
        cog = _make_cog()

        seen = await _run_turn(cog, _message(_bot_user()))

        assert seen["config"].notify_user_id == OWNER_ID
        waiting = seen["dashboard"].set_state.await_args_list[-1]
        assert waiting.kwargs["notify_user_id"] == OWNER_ID
        assert seen["coauthor"] is None

    async def test_a_webhook_turn_pings_the_owner_but_still_credits_the_webhook(self) -> None:
        """#519 keeps CI provenance in the commit; the ping goes to a human."""
        cog = _make_cog()
        webhook = _bot_user(user_id=9999, name="CI")

        seen = await _run_turn(cog, _message(webhook))

        assert seen["config"].notify_user_id == OWNER_ID
        assert seen["coauthor"] is webhook

    async def test_plain_thread_message_still_names_its_author(self) -> None:
        """AC5: the ordinary path is untouched."""
        cog = _make_cog()
        author = _human()

        seen = await _run_turn(cog, _message(author))

        assert seen["coauthor"] is author
        assert seen["config"].notify_user_id == author.id
        waiting = seen["dashboard"].set_state.await_args_list[-1]
        assert waiting.kwargs["notify_user_id"] == author.id


class TestClordPassesTheInvoker:
    async def test_existing_thread_forwards_the_invoker(self) -> None:
        cog = _make_cog()
        cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]
        cog._is_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
        thread = MagicMock(spec=discord.Thread)
        thread.id = 601
        thread.parent_id = 500
        thread.send = AsyncMock(return_value=MagicMock())
        invoker = _human()

        await cog._clord_impl(
            channel=thread,
            channel_id_fallback=None,
            user=invoker,
            prompt="やること",
            respond=AsyncMock(),
            ack=AsyncMock(),
        )

        cog._run_claude.assert_awaited_once()
        call = cog._run_claude.await_args
        assert call is not None
        assert call.kwargs["requester"] is invoker

    async def test_new_thread_forwards_the_invoker(self) -> None:
        cog = _make_cog()
        cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        cog.spawn_session = AsyncMock()  # type: ignore[method-assign]
        cog._is_allowed = MagicMock(return_value=True)  # type: ignore[method-assign]
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 500
        invoker = _human()

        await cog._clord_impl(
            channel=channel,
            channel_id_fallback=channel.id,
            user=invoker,
            prompt="やること",
            respond=AsyncMock(),
            ack=AsyncMock(),
        )

        cog.spawn_session.assert_awaited_once()
        call = cog.spawn_session.await_args
        assert call is not None
        assert call.kwargs["requester"] is invoker

    async def test_spawn_session_forwards_the_requester_to_the_turn(self) -> None:
        cog = _make_cog()
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]
        thread = MagicMock(spec=discord.Thread)
        thread.id = 701
        thread.parent_id = 500
        thread.send = AsyncMock(return_value=MagicMock())
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 500
        channel.create_thread = AsyncMock(return_value=thread)
        invoker = _human()

        await cog.spawn_session(channel, "やること", requester=invoker)

        import asyncio

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending)

        cog._run_claude.assert_awaited_once()
        call = cog._run_claude.await_args
        assert call is not None
        assert call.kwargs["requester"] is invoker
