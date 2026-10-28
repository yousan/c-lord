"""#910: the thread name follows the branch Claude moved to *during* the turn.

The name was only (re)built when a message arrived — when Claude is still on
the previous job's branch. So each new request renamed the thread to the
*previous* job's number (``→#21`` while working on #95) and nothing put it
right afterwards. The naming pass now also runs when the turn ends, where the
branch Claude switched to is visible; it renames only when the name changed.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from tests.test_claude_chat import _make_cog
from tests.test_preempted_turn_ping import _make_cog as _make_turn_cog
from tests.test_preempted_turn_ping import _message


def _record(issue_ref: str) -> MagicMock:
    record = MagicMock()
    record.topic = "交易"
    record.auto_topic_locked = 0
    record.state = "alive"
    record.tmux_window_id = "@1"
    record.issue_ref = issue_ref
    record.origin_issue_ref = "4"
    record.closed_at = None
    return record


async def _run_turn(cog, *, preempt: bool = False) -> list[str]:
    """Drive ``_run_claude``; return the order of naming passes vs the run."""
    order: list[str] = []
    sdm = MagicMock()
    sdm.create_session_dir = MagicMock(return_value="/tmp/work")
    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w1")
    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
    cog._get_dashboard = MagicMock(return_value=None)
    cog._get_coordination = MagicMock(return_value=None)
    cog._get_current_model = AsyncMock(return_value=None)
    cog._apply_thread_naming = AsyncMock(side_effect=lambda **_k: order.append("naming"))

    thread = MagicMock(spec=discord.Thread)
    thread.id = 501
    thread.parent_id = 500
    thread.send = AsyncMock(return_value=MagicMock())

    async def _fake_run(config) -> None:
        order.append("run")
        if preempt:
            await config.runner.interrupt(silent=True)

    with (
        patch("c_lord.cogs.claude_chat.run_claude_with_config", AsyncMock(side_effect=_fake_run)),
        contextlib.suppress(BaseException),
    ):
        await cog._run_claude(_message(), thread, "続けて #12 をお願いします", None)
    return order


async def test_naming_runs_again_when_the_turn_ends() -> None:
    order = await _run_turn(_make_turn_cog())
    assert order == ["naming", "run", "naming"], order


async def test_a_preempted_turn_leaves_naming_to_the_turn_that_replaced_it() -> None:
    order = await _run_turn(_make_turn_cog(), preempt=True)
    assert order == ["naming", "run"], order


async def _naming_pass(cog, thread, branch: str) -> None:
    tmux = MagicMock()
    tmux.get_window_info = MagicMock(return_value=("@1", 4))
    with patch.object(cog, "_git_current_branch", return_value=branch):
        await cog._apply_thread_naming(
            thread=thread, tmux_manager=tmux, first_message="続けて", working_dir="/w"
        )


def _naming_cog(record: MagicMock):
    cog = _make_cog()
    cog._thread_lamp = False
    cog.repo.get = AsyncMock(return_value=record)
    cog.repo.set_topic = AsyncMock()
    cog.repo.set_tmux_window_id = AsyncMock()

    async def _set_issue_ref(_tid: int, ref: str) -> None:
        record.issue_ref = ref

    cog.repo.set_issue_ref = AsyncMock(side_effect=_set_issue_ref)
    return cog


async def test_branch_switched_during_the_turn_is_in_the_name_at_its_end() -> None:
    """AC1: arrival on ``feature/11-a`` → Claude moves to ``feature/12-b`` → ``→#12``."""
    record = _record("11")
    cog = _naming_cog(record)
    thread = MagicMock(spec=discord.Thread)
    thread.id = 1557941264924541028
    thread.name = "W4 │ #4 交易 →#11"
    thread.edit = AsyncMock()

    await _naming_pass(cog, thread, "feature/11-a")  # arrival: still the old branch
    thread.edit.assert_not_awaited()  # nothing to change yet
    await _naming_pass(cog, thread, "feature/12-b")  # turn end: Claude switched
    assert thread.edit.await_args.kwargs["name"] == "W4 │ #4 交易 →#12"


async def test_turn_end_with_no_branch_change_does_not_rename() -> None:
    """AC2: an unchanged name is not sent (rename rate limit, #241)."""
    record = _record("12")
    cog = _naming_cog(record)
    thread = MagicMock(spec=discord.Thread)
    thread.id = 1557941264924541028
    thread.name = "W4 │ #4 交易 →#12"
    thread.edit = AsyncMock()

    await _naming_pass(cog, thread, "feature/12-b")
    thread.edit.assert_not_awaited()
