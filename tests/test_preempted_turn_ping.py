"""#583 (2026-09-08 追記): a turn the user interrupted must not ping them.

Production shape, thread ``1530372563858096321``::

    06:21:55  C-lord    「はい、参加していないスレッドも読めます。…」  ← the answer
                        ← the turn never closed
    06:22:51  yousan    「OK。だいぶん整理できたね。…」               ← the next message
    06:22:52  C-lord    -# ⚡ Interrupted. Starting with new instruction...
    06:22:55  C-lord    🟡 Claude has finished — your reply is needed here. @yousan
    06:23:12  C-lord    「2つとも確認します。」                        ← the new turn

The 🟡 is the *previous* turn's completion ping, finally firing because the
user's own message is what closed that turn.  From the user's side it reads as
a reply to what they just said: they are @-mentioned and told their reply is
needed, four seconds after replying, and one second before the new turn starts.

So: a turn that ended because a new instruction displaced it has no completion
ceremony.  The person is already here — there is nothing to summon them to.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.discord_ui.thread_dashboard import ThreadState, ThreadStatusDashboard


def _runner(tmux: MagicMock | None = None) -> TmuxClaudeRunner:
    tmux = tmux or MagicMock()
    tmux.send_interrupt.return_value = True
    return TmuxClaudeRunner(tmux_manager=tmux, thread_id=42, model="sonnet")


class TestRunnerKnowsItWasPreempted:
    """The runner is where "a new instruction displaced this turn" is known."""

    def test_a_fresh_runner_is_not_preempted(self) -> None:
        assert _runner().preempted is False

    @pytest.mark.asyncio
    async def test_a_silent_interrupt_is_a_preemption(self) -> None:
        """``interrupt(silent=True)`` is only ever the new-message path."""
        runner = _runner()
        await runner.interrupt(silent=True)
        assert runner.preempted is True

    @pytest.mark.asyncio
    async def test_the_stop_button_is_not_a_preemption(self) -> None:
        """⏹ Stop is the user ending a turn deliberately, not replacing it —
        that turn keeps its ordinary ending."""
        runner = _runner()
        await runner.interrupt()
        assert runner.preempted is False


class TestDashboardSkipsThePingForAPreemptedTurn:
    @staticmethod
    def _dash() -> ThreadStatusDashboard:
        d = ThreadStatusDashboard(MagicMock(), owner_id=999)
        d._refresh_dashboard = AsyncMock()  # type: ignore[method-assign]
        return d

    @pytest.mark.asyncio
    async def test_no_completion_ping_when_preempted(self) -> None:
        """AC-N1: no 🟡 for a turn the user's own message closed."""
        thread = MagicMock()
        thread.send = AsyncMock()
        d = self._dash()

        await d.set_state(1, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=7)
        await d.set_state(
            1,
            ThreadState.WAITING_INPUT,
            "x",
            thread=thread,
            notify_user_id=7,
            preempted=True,
        )

        thread.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_next_real_ending_still_pings(self) -> None:
        """Suppression is per turn — the replacement turn must still summon."""
        thread = MagicMock()
        thread.send = AsyncMock()
        d = self._dash()

        await d.set_state(1, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=7)
        await d.set_state(
            1, ThreadState.WAITING_INPUT, "x", thread=thread, notify_user_id=7, preempted=True
        )
        await d.set_state(1, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=7)
        await d.set_state(1, ThreadState.WAITING_INPUT, "x", thread=thread, notify_user_id=7)

        thread.send.assert_awaited_once()
        assert "Claude has finished" in thread.send.await_args.args[0]


# ---------------------------------------------------------------------------
# The wiring: the ⚡ path must actually reach the dashboard as "preempted"
# ---------------------------------------------------------------------------


def _make_cog() -> ClaudeChatCog:
    bot = MagicMock()
    bot.channel_id = 999
    bot.owner_id = 7777
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


def _message() -> MagicMock:
    author = MagicMock()
    author.id = 4242
    author.display_name = "yousan"
    author.bot = False
    m = MagicMock(spec=discord.Message)
    m.id = 77
    m.author = author
    m.add_reaction = AsyncMock()
    m.remove_reaction = AsyncMock()
    m.clear_reaction = AsyncMock()
    return m


async def _run_turn(cog: ClaudeChatCog, *, preempt: bool) -> MagicMock:
    """Drive a whole ``_run_claude``; optionally pre-empt it mid-run.

    ``preempt=True`` reproduces what ``_preempt_prior_turn`` does to the
    in-flight turn: a silent interrupt on its runner while it is running.
    """
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

    async def _fake_run(config) -> None:
        if preempt:
            await config.runner.interrupt(silent=True)

    with (
        patch("c_lord.cogs.claude_chat.run_claude_with_config", AsyncMock(side_effect=_fake_run)),
        contextlib.suppress(BaseException),
    ):
        await cog._run_claude(_message(), thread, "hi", None)

    return dashboard


class TestRunClaudeReportsPreemption:
    @pytest.mark.asyncio
    async def test_a_preempted_turn_is_reported_as_preempted(self) -> None:
        """AC-N1/AC-N2: the ⚡ path must reach the ping decision."""
        dashboard = await _run_turn(_make_cog(), preempt=True)

        waiting = dashboard.set_state.await_args_list[-1]
        assert waiting.args[1] == ThreadState.WAITING_INPUT
        assert waiting.kwargs.get("preempted") is True, (
            "the turn was displaced by a new instruction, but the dashboard was "
            "told to ping the user anyway — 🟡 lands seconds after they typed"
        )

    @pytest.mark.asyncio
    async def test_an_ordinary_turn_is_not_reported_as_preempted(self) -> None:
        dashboard = await _run_turn(_make_cog(), preempt=False)

        waiting = dashboard.set_state.await_args_list[-1]
        assert waiting.args[1] == ThreadState.WAITING_INPUT
        assert not waiting.kwargs.get("preempted")


class TestInterruptedPathMarksTheRunner:
    """End-to-end of the ⚡ notice: the runner it interrupts reports preempted."""

    @pytest.mark.asyncio
    async def test_handle_thread_reply_preempts_the_live_runner(self) -> None:
        cog = _make_cog()
        thread = MagicMock(spec=discord.Thread)
        thread.id = 42
        thread.parent_id = 999
        thread.send = AsyncMock()
        message = MagicMock(spec=discord.Message)
        message.id = 1
        message.channel = thread
        message.content = "new instruction"
        message.attachments = []
        message.reference = None
        message.author = MagicMock()
        message.author.bot = False

        runner = _runner()
        cog._active_runners[thread.id] = runner
        done = asyncio.Event()

        async def active_turn() -> None:
            await done.wait()

        task = asyncio.ensure_future(active_turn())
        cog._active_tasks[thread.id] = task
        # The real runner has no poll loop here, so release the drain by hand
        # once the interrupt has been delivered.
        asyncio.get_running_loop().call_later(0.05, done.set)
        cog._run_claude = AsyncMock()  # type: ignore[method-assign]

        await cog._handle_thread_reply(message)
        with contextlib.suppress(BaseException):
            await task

        assert any("⚡" in str(c.args[0]) for c in thread.send.call_args_list), (
            "the interrupt notice is the user-visible half of this path"
        )
        assert runner.preempted is True
