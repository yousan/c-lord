"""#754 — 📊 Session Status froze for two days.

Two holes, both closed here:

* **Turns nobody posted for never reached the board.** Only ``ClaudeChatCog``
  called ``ThreadStatusDashboard.set_state``; the scheduler, webhook triggers
  and ``/skill`` ran Claude without ever touching it. A scheduled run was live
  while the board said nothing was.
* **Stale rows were only pruned on a state change.** ``_prune_stale`` ran inside
  ``_refresh_dashboard``, which only a state change called — so a quiet day
  left 47-hour-old rows on the board reading "0s ago".
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.scheduler import SchedulerCog
from c_lord.cogs.skill_command import SkillCommandCog
from c_lord.cogs.webhook_trigger import WebhookTrigger, WebhookTriggerCog
from c_lord.database.task_repo import TaskRepository
from c_lord.discord_ui import thread_dashboard as board_mod
from c_lord.discord_ui.authorization import Authorizer
from c_lord.discord_ui.thread_dashboard import (
    _STALE_HOURS,
    ThreadState,
    ThreadStatusDashboard,
    _ThreadInfo,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_dashboard() -> MagicMock:
    """A dashboard double that records the order of state transitions."""
    dashboard = MagicMock(spec=ThreadStatusDashboard)
    dashboard.set_state = AsyncMock()
    return dashboard


def _states(dashboard: MagicMock) -> list[tuple[int, ThreadState]]:
    return [(c.args[0], c.args[1]) for c in dashboard.set_state.call_args_list]


def _run_recording(dashboard: MagicMock, seen: list[list[tuple[int, ThreadState]]]):
    """A ``run_claude_with_config`` stand-in that snapshots the board mid-run."""

    async def _run(_config: object) -> str:
        seen.append(_states(dashboard))
        return "session-1"

    return _run


def _runner_config() -> MagicMock:
    cfg = MagicMock()
    cfg.model = "sonnet"
    cfg.working_dir = None
    cfg.timeout_seconds = 300
    cfg.effort = None
    return cfg


# ---------------------------------------------------------------------------
# AC1 — a scheduled run shows up on the board
# ---------------------------------------------------------------------------


@pytest.fixture
async def task_repo(tmp_path) -> TaskRepository:
    r = TaskRepository(str(tmp_path / "tasks.db"))
    await r.init_db()
    return r


async def _scheduler_with_task(
    task_repo: TaskRepository, dashboard: MagicMock | None
) -> tuple[SchedulerCog, dict]:
    bot = MagicMock()
    bot.settings_repo = None
    bot.thread_dashboard = dashboard
    cog = SchedulerCog(bot, _runner_config(), repo=task_repo)

    thread = AsyncMock(spec=discord.Thread)
    thread.id = 777
    starter = AsyncMock()
    starter.create_thread = AsyncMock(return_value=thread)
    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 99
    channel.send = AsyncMock(return_value=starter)
    bot.get_channel = MagicMock(return_value=channel)

    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w7")
    tmux.session_exists = MagicMock(return_value=True)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)

    task_id = await task_repo.create(
        name="weekly-audit",
        prompt="週次精査をして\n結果を Issue にする",
        interval_seconds=604800,
        channel_id=99,
    )
    task = await task_repo.get(task_id)
    assert task is not None
    return cog, task


class TestScheduledRunIsOnTheBoard:
    async def test_run_is_processing_while_claude_runs_and_waiting_after(
        self, task_repo: TaskRepository
    ) -> None:
        dashboard = _mock_dashboard()
        cog, task = await _scheduler_with_task(task_repo, dashboard)
        seen: list[list[tuple[int, ThreadState]]] = []

        with patch(
            "c_lord.cogs.scheduler.run_claude_with_config",
            side_effect=_run_recording(dashboard, seen),
        ):
            await cog._run_task(task)

        assert seen == [[(777, ThreadState.PROCESSING)]]
        assert _states(dashboard) == [
            (777, ThreadState.PROCESSING),
            (777, ThreadState.WAITING_INPUT),
        ]

    async def test_row_is_labelled_by_the_task_name_not_its_prompt(
        self, task_repo: TaskRepository
    ) -> None:
        """The prompt is server-side and never reached Discord; the board is public."""
        dashboard = _mock_dashboard()
        cog, task = await _scheduler_with_task(task_repo, dashboard)

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock):
            await cog._run_task(task)

        description = dashboard.set_state.call_args_list[0].args[2]
        assert description == "[Scheduled] weekly-audit"
        assert "週次精査" not in description

    async def test_run_that_raises_still_leaves_processing(self, task_repo: TaskRepository) -> None:
        dashboard = _mock_dashboard()
        cog, task = await _scheduler_with_task(task_repo, dashboard)

        with patch(
            "c_lord.cogs.scheduler.run_claude_with_config",
            side_effect=RuntimeError("boom"),
        ):
            await cog._run_task(task)

        assert _states(dashboard)[-1] == (777, ThreadState.WAITING_INPUT)

    async def test_turn_end_summons_nobody(self, task_repo: TaskRepository) -> None:
        """Only the board changes — no new "Claude has finished" ping (#525 stays as-is)."""
        dashboard = _mock_dashboard()
        cog, task = await _scheduler_with_task(task_repo, dashboard)

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock):
            await cog._run_task(task)

        for call in dashboard.set_state.call_args_list:
            assert call.kwargs.get("thread") is None

    async def test_failing_board_never_stops_the_run(self, task_repo: TaskRepository) -> None:
        """#632 parity: the board is decoration."""
        dashboard = _mock_dashboard()
        dashboard.set_state = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "down"))
        cog, task = await _scheduler_with_task(task_repo, dashboard)

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock) as run:
            await cog._run_task(task)

        run.assert_awaited_once()

    async def test_no_board_is_fine(self, task_repo: TaskRepository) -> None:
        cog, task = await _scheduler_with_task(task_repo, None)

        with patch("c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock) as run:
            await cog._run_task(task)

        run.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC2 — webhook triggers and /skill show up too
# ---------------------------------------------------------------------------


class TestWebhookRunIsOnTheBoard:
    async def test_webhook_run_is_processing_then_waiting(self) -> None:
        dashboard = _mock_dashboard()
        bot = MagicMock()
        bot.settings_repo = None
        bot.thread_dashboard = dashboard
        cog = WebhookTriggerCog(
            bot=bot,
            runner=_runner_config(),
            triggers={"🔄 docs-sync": WebhookTrigger(prompt="Sync docs")},
        )
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())

        thread = MagicMock(spec=discord.Thread)
        thread.id = 4321
        thread.send = AsyncMock()
        msg = MagicMock(spec=discord.Message)
        msg.content = "🔄 docs-sync"
        msg.webhook_id = 12345
        msg.channel = MagicMock()
        msg.channel.id = 999
        msg.reply = AsyncMock()
        msg.add_reaction = AsyncMock()
        msg.create_thread = AsyncMock(return_value=thread)
        seen: list[list[tuple[int, ThreadState]]] = []

        with (
            patch("c_lord.claude.tmux_runner.TmuxClaudeRunner"),
            patch(
                "c_lord.cogs.webhook_trigger.run_claude_with_config",
                side_effect=_run_recording(dashboard, seen),
            ),
        ):
            await cog.on_message(msg)

        assert seen == [[(4321, ThreadState.PROCESSING)]]
        assert _states(dashboard) == [
            (4321, ThreadState.PROCESSING),
            (4321, ThreadState.WAITING_INPUT),
        ]
        # The public prefix, not the server-side prompt "Sync docs".
        assert dashboard.set_state.call_args_list[0].args[2] == "🔄 docs-sync"


def _skill_cog(dashboard: MagicMock) -> SkillCommandCog:
    bot = MagicMock()
    bot.settings_repo = None
    bot.thread_dashboard = dashboard
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    cog = SkillCommandCog(
        bot=bot,
        repo=repo,
        runner=_runner_config(),
        claude_channel_id=999,
        skills_dir="/nonexistent/skills",
        authorizer=Authorizer(allow_anyone=True),
    )
    cog._skills = [{"name": "recall", "description": ""}]
    cog._maybe_reload_skills = MagicMock()  # type: ignore[method-assign]
    cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    cog._resolve_session_dir_manager = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    return cog


def _thread(thread_id: int, parent_id: int = 999) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.mention = f"<#{thread_id}>"
    thread.send = AsyncMock()
    return thread


def _interaction(channel: MagicMock) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 1
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.channel = channel
    return interaction


class TestSkillRunIsOnTheBoard:
    async def test_skill_in_a_new_thread(self) -> None:
        dashboard = _mock_dashboard()
        cog = _skill_cog(dashboard)
        new_thread = _thread(6060)
        claude_channel = MagicMock(spec=discord.TextChannel)
        claude_channel.id = 999
        claude_channel.create_thread = AsyncMock(return_value=new_thread)
        cog.bot.get_channel = MagicMock(return_value=claude_channel)
        seen: list[list[tuple[int, ThreadState]]] = []

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config",
            side_effect=_run_recording(dashboard, seen),
        ):
            await cog.run_skill.callback(
                cog, _interaction(MagicMock(spec=discord.TextChannel)), name="recall", args=None
            )

        assert seen == [[(6060, ThreadState.PROCESSING)]]
        assert _states(dashboard) == [
            (6060, ThreadState.PROCESSING),
            (6060, ThreadState.WAITING_INPUT),
        ]
        assert dashboard.set_state.call_args_list[0].args[2] == "/recall"

    async def test_skill_inside_an_existing_thread(self) -> None:
        dashboard = _mock_dashboard()
        cog = _skill_cog(dashboard)
        seen: list[list[tuple[int, ThreadState]]] = []

        with patch(
            "c_lord.cogs.skill_command.run_claude_with_config",
            side_effect=_run_recording(dashboard, seen),
        ):
            await cog.run_skill.callback(
                cog, _interaction(_thread(5555)), name="recall", args="today"
            )

        assert seen == [[(5555, ThreadState.PROCESSING)]]
        assert _states(dashboard) == [
            (5555, ThreadState.PROCESSING),
            (5555, ThreadState.WAITING_INPUT),
        ]


# ---------------------------------------------------------------------------
# AC3 / AC4 — stale rows go away with nobody posting; empty board says so
# ---------------------------------------------------------------------------


def _board() -> tuple[ThreadStatusDashboard, MagicMock]:
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message)
    message.id = 1
    message.pinned = True
    message.edit = AsyncMock()
    channel.send = AsyncMock(return_value=message)

    async def _empty(**_kw: object):
        for _ in ():
            yield _

    channel.history = MagicMock(side_effect=lambda **_kw: _empty())
    channel.pins = MagicMock(side_effect=lambda **_kw: _empty())
    return ThreadStatusDashboard(channel=channel, bot_user_id=4242), message


def _age(dashboard: ThreadStatusDashboard, thread_id: int, hours: float) -> None:
    info = _ThreadInfo(thread_id=thread_id, description="old", state=ThreadState.WAITING_INPUT)
    info.state_changed_at = time.monotonic() - hours * 3600
    dashboard._threads[thread_id] = info


class TestStaleRowsLeaveWithoutAStateChange:
    async def test_prune_drops_the_last_stale_row_and_shows_no_active_sessions(self) -> None:
        dashboard, message = _board()
        await dashboard.initialize()
        _age(dashboard, 55, _STALE_HOURS + 1)
        message.edit.reset_mock()

        await dashboard.prune_stale_rows()

        assert 55 not in dashboard._threads
        message.edit.assert_awaited_once()
        embed = message.edit.call_args.kwargs["embed"]
        assert embed.description == "No active sessions."
        await dashboard.aclose()

    async def test_prune_with_nothing_stale_does_not_edit(self) -> None:
        dashboard, message = _board()
        await dashboard.initialize()
        _age(dashboard, 56, 0.5)
        message.edit.reset_mock()

        await dashboard.prune_stale_rows()

        assert 56 in dashboard._threads
        message.edit.assert_not_awaited()
        await dashboard.aclose()

    async def test_the_board_prunes_itself_on_a_timer(self) -> None:
        """AC3: nobody posts, no state changes — the stale row still goes."""
        dashboard, message = _board()
        with patch.object(board_mod, "_PRUNE_INTERVAL_SECONDS", 0.01):
            await dashboard.initialize()
            _age(dashboard, 57, _STALE_HOURS + 1)
            for _ in range(100):
                if 57 not in dashboard._threads:
                    break
                await asyncio.sleep(0.01)

        assert 57 not in dashboard._threads
        assert message.edit.call_args.kwargs["embed"].description == "No active sessions."
        await dashboard.aclose()

    async def test_reinitialize_does_not_start_a_second_timer(self) -> None:
        """on_ready fires on every reconnect (#720) — one timer, not one per reconnect."""
        dashboard, _ = _board()
        await dashboard.initialize()
        first = dashboard._prune_task
        await dashboard.initialize()

        assert first is not None
        assert dashboard._prune_task is first
        await dashboard.aclose()

    async def test_timer_survives_a_failing_tick(self) -> None:
        dashboard, message = _board()
        with patch.object(board_mod, "_PRUNE_INTERVAL_SECONDS", 0.01):
            await dashboard.initialize()
            message.edit = AsyncMock(side_effect=[RuntimeError("session closed"), None])
            _age(dashboard, 58, _STALE_HOURS + 1)
            await asyncio.sleep(0.05)
            _age(dashboard, 59, _STALE_HOURS + 1)
            for _ in range(100):
                if 59 not in dashboard._threads:
                    break
                await asyncio.sleep(0.01)

        assert 59 not in dashboard._threads
        assert dashboard._prune_task is not None
        assert not dashboard._prune_task.done()
        await dashboard.aclose()
