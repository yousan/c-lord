"""#915: a turn whose cause is known pings its requester once, not twice.

A logged-out / rate-limited / model-refused turn posted the ❌ embed with a
mention (#681) and then the turn-end line with a mention (#812 / #631 / #484).
Same person, same content, two pings — and a summons that repeats itself stops
being read. The embed carries everything the line says (and more), so when it
has already pinged the person the line would ping, the line is not posted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.claude.tmux_runner import LOGIN_REQUIRED_ERROR_PREFIX, MODEL_UNAVAILABLE_ERROR_PREFIX
from c_lord.claude.types import MessageType, StreamEvent, UsageLimit
from c_lord.cogs.event_processor import EventProcessor
from c_lord.cogs.run_config import RunConfig
from c_lord.discord_ui.thread_dashboard import ThreadState, ThreadStatusDashboard

USER = 7


def _dash() -> ThreadStatusDashboard:
    d = ThreadStatusDashboard(MagicMock(), owner_id=999)
    d._refresh_dashboard = AsyncMock()  # type: ignore[method-assign]
    return d


def _mentions(thread: MagicMock) -> int:
    count = 0
    for call in thread.send.await_args_list:
        content = call.kwargs.get("content")
        if content is None and call.args:
            content = call.args[0]
        if content and f"<@{USER}>" in content:
            count += 1
    return count


async def _turn(event: StreamEvent) -> MagicMock:
    """Drive one failed turn through the embed path and the turn-end path."""
    thread = MagicMock()
    thread.id = 915
    thread.send = AsyncMock(return_value=MagicMock())
    config = RunConfig(thread=thread, runner=MagicMock(), prompt="hi", notify_user_id=USER)
    await EventProcessor(config).process(event)

    d = _dash()
    await d.set_state(915, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=USER)
    await d.set_state(
        915,
        ThreadState.WAITING_INPUT,
        "x",
        thread=thread,
        notify_user_id=USER,
        no_response=config.outcome.no_response,
        usage_limit=config.outcome.usage_limit,
        login_required=config.outcome.login_required,
        model_unavailable=config.outcome.model_unavailable,
        already_pinged=config.outcome.pinged_user_id,
    )
    return thread


def _result(error: str, usage_limit: UsageLimit | None = None) -> StreamEvent:
    return StreamEvent(
        message_type=MessageType.RESULT, is_complete=True, error=error, usage_limit=usage_limit
    )


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            _result(f"{LOGIN_REQUIRED_ERROR_PREFIX} Claude Code needs /login."), id="login"
        ),
        pytest.param(
            _result(
                "usage limit",
                UsageLimit("weekly limit", "Aug 29, 4pm (Asia/Tokyo)", ""),
            ),
            id="usage-limit",
        ),
        pytest.param(
            _result(f'{MODEL_UNAVAILABLE_ERROR_PREFIX} Claude Code cannot use the model "bogus".'),
            id="model-unavailable",
        ),
    ],
)
async def test_known_cause_turn_pings_exactly_once(event: StreamEvent) -> None:
    thread = await _turn(event)
    assert _mentions(thread) == 1, thread.send.await_args_list
    # The one that remains is the embed — it says everything the line said.
    assert thread.send.await_args_list[0].kwargs.get("embed") is not None


async def test_line_still_pings_when_the_embed_pinged_nobody() -> None:
    """No mention on the embed (no failure target) — the line must still summon."""
    thread = MagicMock()
    thread.send = AsyncMock()
    d = _dash()
    await d.set_state(1, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=USER)
    await d.set_state(
        1,
        ThreadState.WAITING_INPUT,
        "x",
        thread=thread,
        notify_user_id=USER,
        no_response=True,
        login_required=True,
        already_pinged=None,
    )
    assert _mentions(thread) == 1


async def test_ordinary_turn_end_is_unchanged() -> None:
    thread = MagicMock()
    thread.send = AsyncMock()
    d = _dash()
    await d.set_state(1, ThreadState.PROCESSING, "x", thread=thread, notify_user_id=USER)
    await d.set_state(
        1, ThreadState.WAITING_INPUT, "x", thread=thread, notify_user_id=USER, already_pinged=USER
    )
    assert "Claude has finished" in thread.send.await_args.args[0]
