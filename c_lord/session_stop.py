"""The one stop path behind ⏹ Stop, ``/stop`` and ``!stop`` (#878).

Before #878 the button called the runner's ``interrupt()`` itself while the two
commands went through ``ClaudeChatCog._stop_impl``, and both only sent C-c to a
turn c-lord had started. On staging that left three ways for Claude to keep
going after the user pressed Stop:

1. Commands Claude had put in the background kept running (C-c does not reach
   them), and each one that finished woke Claude for a new turn.
2. A ``<task-notification>`` already queued when C-c landed is not dropped by
   the CLI — it starts a new turn right after the interrupt.
3. Such a self-started turn has no runner in c-lord, so ``!stop`` answered
   "No active session" and there was no button to press.

:func:`stop_thread_work` therefore interrupts the turn (through its runner, or
straight in the pane when c-lord did not start it), ends the Bash tool shells,
and :func:`suppress_wakeups` interrupts a turn that wakes up in the seconds
after. Spec: ``docs/specs/stop-button.md``.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from .discord_ui.embeds import stopped_embed
from .utils.logger import log_ctx

if TYPE_CHECKING:
    from .tmux import TmuxSessionManager

logger = logging.getLogger(__name__)

#: How long after a stop a self-started turn is still treated as fallout of the
#: stop. A killed shell's notification arrives within a few seconds (staging:
#: ~3s); the margin covers a slow box.
WAKEUP_GUARD_SECONDS = 20.0
#: Pause after sending C-c before looking again. Two C-c's at an idle prompt
#: within about a second exit Claude Code, so the guard never sends them closer.
_SETTLE_SECONDS = 3.0
_POLL_SECONDS = 1.0

NOTHING_RUNNING_MESSAGE = "No active session is running in this thread."
FAILED_MESSAGE = (
    "⚠️ 止められませんでした — このスレッドの Claude に割り込みを送れませんでした"
    "（tmux のウィンドウが見つからない等）。Claude はまだ動いている可能性があります。"
)


class StopOutcome(enum.Enum):
    STOPPED = "stopped"
    NOTHING_RUNNING = "nothing_running"
    FAILED = "failed"


@dataclass(frozen=True)
class StopResult:
    outcome: StopOutcome
    shells_stopped: int = 0


async def _pane_is_working(tmux: TmuxSessionManager, thread_id: int) -> bool:
    """True when the pane shows Claude mid-turn (spinner on screen, claude alive)."""
    from .claude.tmux_runner import TmuxClaudeRunner, _normalize_capture

    pane = await asyncio.to_thread(tmux.capture_pane, thread_id)
    if not TmuxClaudeRunner._is_generating(_normalize_capture(pane or "")):
        return False
    return bool(await asyncio.to_thread(tmux.is_claude_running, thread_id))


async def stop_thread_work(
    thread_id: int,
    *,
    runner: Any | None,
    tmux: TmuxSessionManager | None,
) -> StopResult:
    """Stop everything Claude is doing in *thread_id*'s pane.

    *runner* is the turn c-lord is driving, if any. Without one, C-c is sent
    only when the pane visibly shows a turn — a C-c at an idle prompt arms
    "press Ctrl-C again to exit", and a second one would quit Claude Code.
    """
    interrupted = False
    failed = False
    if runner is not None:
        sent = await runner.interrupt()
        # ``Interruptable.interrupt`` used to return None; only an explicit
        # False means the C-c did not go out.
        interrupted = sent is not False
        failed = not interrupted
    elif tmux is not None and await _pane_is_working(tmux, thread_id):
        interrupted = bool(await asyncio.to_thread(tmux.send_interrupt, thread_id))
        failed = not interrupted

    shells = 0
    if tmux is not None:
        try:
            shells = int(await asyncio.to_thread(tmux.stop_tool_shells, thread_id))
        except Exception:
            logger.warning(
                "%s could not stop the Bash tool shells (#878)",
                log_ctx(thread_id=thread_id),
                exc_info=True,
            )

    if failed:
        outcome = StopOutcome.FAILED
    elif interrupted or shells:
        outcome = StopOutcome.STOPPED
    else:
        outcome = StopOutcome.NOTHING_RUNNING
    logger.info(
        "%s stop: outcome=%s runner=%s shells_stopped=%d (#878)",
        log_ctx(thread_id=thread_id),
        outcome.value,
        runner is not None,
        shells,
    )
    return StopResult(outcome, shells_stopped=shells)


async def suppress_wakeups(
    thread_id: int,
    *,
    tmux: TmuxSessionManager,
    still_ours: Callable[[], bool],
    window: float = WAKEUP_GUARD_SECONDS,
    interval: float = _POLL_SECONDS,
    settle: float = _SETTLE_SECONDS,
    max_interrupts: int = 3,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> int:
    """Interrupt turns that start on their own right after a stop; return how many.

    The CLI delivers a stopped command's ``<task-notification>`` (and any that
    were queued) as a new turn. *still_ours* returns False once the user sends
    a new message (c-lord registers a runner for it) — from then on a busy pane
    is the user's turn and the guard steps away.
    """
    sent = 0
    await sleep(settle)
    elapsed = settle
    while elapsed < window and sent < max_interrupts:
        if not still_ours():
            break
        if await _pane_is_working(tmux, thread_id):
            await asyncio.to_thread(tmux.send_interrupt, thread_id)
            sent += 1
            logger.info(
                "%s stop: interrupted a turn that woke up after the stop (#878)",
                log_ctx(thread_id=thread_id),
            )
            await sleep(settle)
            elapsed += settle
        else:
            await sleep(interval)
            elapsed += interval
    return sent


def stop_reply(result: StopResult) -> tuple[str | None, discord.Embed | None, bool]:
    """What to tell the user: ``(content, embed, ephemeral)``. Same for every entry point."""
    if result.outcome is StopOutcome.STOPPED:
        return None, stopped_embed(shells_stopped=result.shells_stopped), False
    if result.outcome is StopOutcome.FAILED:
        return FAILED_MESSAGE, None, False
    return NOTHING_RUNNING_MESSAGE, None, True
