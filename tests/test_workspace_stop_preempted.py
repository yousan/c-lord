"""#1101 — a workspace stop must not turn a pre-empted (replaced) turn into a user stop.

While ``_preempt_prior_turn`` drains the displaced turn, its runner is still
``ClaudeChatCog._active_runners[thread]``. ``end_turn_for_workspace_stop``
withdrew it with ``silent=False`` regardless, so the replaced turn ended as
``outcome=stopped`` (with a turn-end event of its own) instead of
``outcome=cancelled`` — contradicting ``docs/specs/session-close.md``. The
ledger side (``withdraw_in_flight``) already skipped pre-empted runs (#1019).
# <<REDACTED 5 lines>>
import logging
from unittest.mock import MagicMock

import pytest

from c_lord.cogs import _run_helper
from c_lord.cogs.run_config import RunConfig

THREAD = 1_101_001


class _Runner:
    """Mirrors TmuxClaudeRunner's stopped / preempted / withdraw semantics."""

    def __init__(self) -> None:
        self._stopped = False
        self._silent = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def preempted(self) -> bool:
        return self._stopped and self._silent

    def withdraw(self, *, silent: bool = False) -> None:
        self._stopped = True
        self._silent = silent


def _chat():  # type: ignore[no-untyped-def]
    from c_lord.cogs.claude_chat import ClaudeChatCog

    bot = MagicMock()
    bot.channel_id = 999
    return ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())


@pytest.fixture(autouse=True)
def _ledger():  # type: ignore[no-untyped-def]
    yield
    _run_helper._IN_FLIGHT.pop(THREAD, None)


async def _parked(_config, _ctx) -> None:  # type: ignore[no-untyped-def]
    await asyncio.sleep(3600)


async def _run_and_stop(monkeypatch, runner: _Runner, caplog) -> str:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(_run_helper, "_run_turn", _parked)
    thread = MagicMock()
    thread.id = THREAD
    task = asyncio.create_task(
        _run_helper.run_claude_with_config(
            RunConfig(thread=thread, runner=runner, repo=None, prompt="a")
        )
    )
    await asyncio.sleep(0.02)
    chat = _chat()
    chat._active_runners[THREAD] = runner
    chat._active_tasks[THREAD] = task
    with caplog.at_level(logging.INFO):
        chat.end_turn_for_workspace_stop(THREAD)
        await asyncio.wait_for(chat.wait_for_turn_end(THREAD, timeout=0.2), 3.0)
    return "\n".join(r.getMessage() for r in caplog.records if "run_claude: exit" in r.getMessage())


async def test_chat_table_preempted_runner_stays_preempted(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    old = _Runner()
    old.withdraw(silent=True)  # the next message's silent interrupt (_preempt_prior_turn)
    exits = await _run_and_stop(monkeypatch, old, caplog)
    assert old.preempted is True, "workspace stop overwrote the pre-emption (silent=False)"
    assert "outcome=cancelled" in exits, exits
    assert "outcome=stopped" not in exits, exits


async def test_a_running_turn_is_still_ended_as_a_user_stop(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """AC2: a turn nobody interrupted still ends as ``outcome=stopped``."""
    live = _Runner()
    exits = await _run_and_stop(monkeypatch, live, caplog)
    assert live.stopped is True and live.preempted is False
    assert "outcome=stopped" in exits, exits
