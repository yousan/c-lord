"""How a turn's end reaches Discord through EventProcessor — the cases not pinned yet.

``docs/specs/turn-end-notification.md`` is the "should" for every test here:

* #915 / #967 AC2: the turn-end line is skipped only when the ❌ embed already
  pinged *the same person*. "embed が呼んでいない / 別の人を呼んだ場合は従来どおり
  1 行が出る".
* spec §「原因の分かっているターンの呼び出しは 1 回」: "原因の分からない
  「応答がありませんでした」は対象外（embed と 1 行の両方が出ます）".
* #562 / #630 / #684 / #701: a turn that produced nothing (no response, trust
  dialog stuck / not accepted, fleet tmux replaced) is never announced as
  "Claude has finished".
* #856: ``on_session_saved`` fires once per turn, and a failing hook cannot
  take the turn down.
* #166 / #723: ``pane_ask`` is bridged only by a runner that can answer the
  pane's menu; ``unknown_tui_prompt`` warns the thread — a warning that, like
  every decoration, must not raise when Discord is unreachable (#930).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

from c_lord.claude.tmux_runner import (
    FLEET_TMUX_RESTART_ERROR_PREFIX,
    LOGIN_REQUIRED_ERROR_PREFIX,
    NO_RESPONSE_ERROR_PREFIX,
    TRUST_START_FAILED_ERROR_PREFIX,
    TRUST_STUCK_ERROR_PREFIX,
)
from c_lord.claude.types import AskOption, AskQuestion, MessageType, StreamEvent
from c_lord.cogs.event_processor import EventProcessor
from c_lord.cogs.run_config import RunConfig
from c_lord.discord_ui.thread_dashboard import ThreadState, ThreadStatusDashboard

REQUESTER = 7
DISPATCHER = 8


def _thread(thread_id: int = 9150) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return thread


def _mentions(thread: MagicMock, uid: int) -> int:
    count = 0
    for call in thread.send.await_args_list:
        content = call.kwargs.get("content")
        if content is None and call.args:
            content = call.args[0]
        if content and f"<@{uid}>" in content:
            count += 1
    return count


def _result(error: str | None) -> StreamEvent:
    return StreamEvent(message_type=MessageType.RESULT, is_complete=True, error=error)


async def _turn(
    event: StreamEvent, *, failure_notify_id: int | None, status: MagicMock | None = None
) -> tuple[MagicMock, RunConfig]:
    """One turn through the ❌ embed path, then the turn-end line (as claude_chat does)."""
    thread = _thread()
    config = RunConfig(
        thread=thread,
        runner=MagicMock(),
        prompt="hi",
        notify_user_id=REQUESTER,
        failure_notify_id=failure_notify_id,
        status=status,
    )
    await EventProcessor(config).process(event)

    board = ThreadStatusDashboard(MagicMock(), owner_id=None)
    board._refresh_dashboard = AsyncMock()  # type: ignore[method-assign]
    await board.set_state(
        thread.id, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=REQUESTER
    )
    await board.set_state(
        thread.id,
        ThreadState.WAITING_INPUT,
        "x",
        thread=thread,
        notify_user_id=REQUESTER,
        no_response=config.outcome.no_response,
        usage_limit=config.outcome.usage_limit,
        login_required=config.outcome.login_required,
        model_unavailable=config.outcome.model_unavailable,
        api_error=config.outcome.api_error,
        already_pinged=config.outcome.pinged_user_id,
    )
    return thread, config


def _status() -> MagicMock:
    status = MagicMock()
    status.set_done = AsyncMock()
    status.set_error = AsyncMock()
    return status


# ── #915 / #967: one ping — but only when it is the same person ─────────────


@pytest.mark.asyncio
async def test_embed_pinging_someone_else_leaves_the_requesters_line() -> None:
    """#967 AC2: the ❌ embed summoned the dispatcher, not the requester.

    ``failure_notify_id`` and ``notify_user_id`` answer different policy rungs
    (#681), so they can differ. Skipping the line here would leave the person
    who asked with no summons at all.
    """
    thread, config = await _turn(
        _result(f"{LOGIN_REQUIRED_ERROR_PREFIX} Claude Code needs /login."),
        failure_notify_id=DISPATCHER,
    )
    assert config.outcome.pinged_user_id == DISPATCHER
    assert _mentions(thread, DISPATCHER) == 1
    assert _mentions(thread, REQUESTER) == 1
    assert "🔑" in thread.send.await_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_without_a_failure_target_the_embed_pings_the_requester_and_the_line_is_skipped() -> (
    None
):
    """#681 fallback: no ``failure_notify_id`` → the embed pings ``notify_user_id``.

    That is the same person the line would ping, so #915 holds: exactly one.
    """
    thread, config = await _turn(
        _result(f"{LOGIN_REQUIRED_ERROR_PREFIX} Claude Code needs /login."),
        failure_notify_id=None,
    )
    assert config.outcome.pinged_user_id == REQUESTER
    assert _mentions(thread, REQUESTER) == 1
    assert thread.send.await_count == 1  # the embed only


@pytest.mark.asyncio
async def test_no_response_is_outside_the_one_ping_rule() -> None:
    """Spec: "原因の分からない「応答がありませんでした」は対象外（embed と 1 行の両方が出ます）"."""
    thread, config = await _turn(
        _result(f"{NO_RESPONSE_ERROR_PREFIX} Claude did not start this turn."),
        failure_notify_id=REQUESTER,
    )
    assert config.outcome.no_response is True
    assert config.outcome.pinged_user_id == REQUESTER
    assert _mentions(thread, REQUESTER) == 2
    assert thread.send.await_args_list[-1].args[0].startswith("⚠️ 応答がありませんでした")


# ── turns that produced nothing are never "finished" ────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prefix", "issue"),
    [
        (NO_RESPONSE_ERROR_PREFIX, "#562"),
        (TRUST_STUCK_ERROR_PREFIX, "#630"),
        (TRUST_START_FAILED_ERROR_PREFIX, "#684"),
        (FLEET_TMUX_RESTART_ERROR_PREFIX, "#701"),
    ],
)
async def test_a_turn_that_produced_nothing_is_not_announced_as_finished(
    prefix: str, issue: str
) -> None:
    """Each of these joined ``no_response`` in ``_on_complete`` for its own issue."""
    status = _status()
    thread, config = await _turn(
        _result(f"{prefix} details"), failure_notify_id=REQUESTER, status=status
    )
    assert config.outcome.no_response is True, issue
    status.set_error.assert_awaited_once()
    status.set_done.assert_not_awaited()
    line = thread.send.await_args_list[-1].args[0]
    assert "Claude has finished" not in line, issue
    assert line.startswith("⚠️ 応答がありませんでした"), issue


# ── #856: on_session_saved ───────────────────────────────────────────────────


def _system(session_id: str) -> StreamEvent:
    return StreamEvent(message_type=MessageType.SYSTEM, session_id=session_id)


@pytest.mark.asyncio
async def test_on_session_saved_fires_once_per_turn() -> None:
    """#856: Claude can emit several SYSTEM events; the naming stash is written once."""
    thread = _thread()
    repo = MagicMock()
    repo.save = AsyncMock()
    hook = AsyncMock()
    config = RunConfig(
        thread=thread, runner=MagicMock(), prompt="hi", repo=repo, on_session_saved=hook
    )
    p = EventProcessor(config)
    await p.process(_system("abc-123"))
    await p.process(_system("abc-123"))
    hook.assert_awaited_once_with(thread.id)
    assert repo.save.await_count == 2


@pytest.mark.asyncio
async def test_a_failing_on_session_saved_hook_does_not_stop_the_turn() -> None:
    """#856: "A failure here is the hook's to log" — the turn goes on."""
    thread = _thread()
    repo = MagicMock()
    repo.save = AsyncMock()
    hook = AsyncMock(side_effect=RuntimeError("db locked"))
    config = RunConfig(
        thread=thread, runner=MagicMock(), prompt="hi", repo=repo, on_session_saved=hook
    )
    p = EventProcessor(config)
    await p.process(_system("abc-123"))  # must not raise
    assert p.session_id == "abc-123"
    thread.send.assert_awaited_once()  # the session-start embed still went out


@pytest.mark.asyncio
async def test_no_hook_without_a_repo() -> None:
    """The hook waits for the row to exist; with no repo there is no row."""
    hook = AsyncMock()
    config = RunConfig(thread=_thread(), runner=MagicMock(), prompt="hi", on_session_saved=hook)
    await EventProcessor(config).process(_system("abc-123"))
    hook.assert_not_awaited()


# ── SYSTEM side-channels that end in Discord ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        aiohttp.ClientConnectionError("Temporary failure in name resolution"),
        TimeoutError(),
        discord.HTTPException(MagicMock(status=500), "x"),
    ],
)
async def test_an_unknown_tui_prompt_warning_that_cannot_be_sent_does_not_raise(
    exc: BaseException,
) -> None:
    """The warning is decoration: a blip must not end the turn it warns about."""
    thread = _thread()
    thread.send = AsyncMock(side_effect=exc)
    repo = MagicMock()
    repo.save = AsyncMock()
    config = RunConfig(thread=thread, runner=MagicMock(), prompt="hi", repo=repo)
    event = StreamEvent(
        message_type=MessageType.SYSTEM,
        unknown_tui_prompt="Do you want to continue? (y/n)",
        session_id="abc-123",
    )
    p = EventProcessor(config)
    await p.process(event)
    thread.send.assert_awaited_once()
    # The prompt event is handled on its own — it does not double as a session save.
    repo.save.assert_not_awaited()
    assert p.session_id is None


@pytest.mark.asyncio
async def test_pane_ask_is_skipped_by_a_runner_that_cannot_answer_menus() -> None:
    """#166: the buttons are only useful if the answer can be typed into a pane."""
    thread = _thread()
    runner = MagicMock(spec=["interrupt"])  # no answer_menu — not a tmux runner
    config = RunConfig(thread=thread, runner=runner, prompt="hi")
    event = StreamEvent(
        message_type=MessageType.SYSTEM,
        pane_ask=AskQuestion(question="どうしますか？", header="h", options=[AskOption("A", "")]),
    )
    await EventProcessor(config).process(event)
    thread.send.assert_not_awaited()
