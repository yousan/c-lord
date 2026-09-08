"""Make the question menus of a previous process answerable again (#671).

A ``discord.ui.View``'s callbacks live in the process that built it. When the
bot restarts, every menu already on screen keeps its buttons — and nothing
answers Discord's 3-second ACK when one is pressed, so the user gets a red
**"アプリケーションは時間内に応答しませんでした"** and the bot log shows nothing
at all. Production, 2026-09-01: a menu bridged at 16:48:53, a redeploy at
17:01:22, a press at 17:29 that reached no code. The person then had to send an
ordinary message, which Esc'd the menu away — so the question was not merely
unanswerable, it was destroyed.

The recovery has two halves, and the second is the one that matters:

**Re-arm.** ``AskView`` is persistent (``timeout=None``) with stable
``custom_id``s, so ``bot.add_view()`` on a rebuilt view restores routing for
buttons that are already on screen. That alone stops the red timeout — the
click reaches code and can say something true.

**Deliver.** But the interesting fact about a restart is that *the TUI menu is
still open in the tmux pane, with Claude still blocked on it*. Nothing about the
question died; only the Python object holding the callback did. So a re-armed
button does not have to answer "sorry, the session is gone" — it can type the
answer straight into the pane, which is exactly what the live bridge does.
:class:`PaneMenuAnswerer` is that path, and it re-reads the pane first: the
answer goes out only when the menu still showing there is *the same question*
(same fingerprint the #633 watchdog dedups on). A menu the user has since
answered in the terminal, or moved past, is refused rather than answered blind.

Nothing here ever posts a menu. Re-posting is what #633 removed after the
watchdog stacked 188 re-bridges and put one ❓ into a thread six times over three
days, and the production bot restarts up to six times a day — so recovery works
only on the messages already on screen. A menu whose pane no longer shows it is
retired in place (buttons stripped, ledger row dropped), the same treatment #634
gave leftover ⏹ Stop buttons.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from .claude.types import AskQuestion, ask_question_from_dict
from .discord_ui.ask_handler import (
    _finalize_menu_message,
    _transcript_dir,
    _verify_answer_reached_claude,
    send_answer_keystrokes,
)
from .discord_ui.ask_view import AskView
from .transcript.ask_result import ASK_ANSWERED, latest_ask_tool_use
from .utils.logger import log_ctx

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .claude.tmux_runner import TmuxClaudeRunner
    from .database.ask_repo import PendingAskRepository

logger = logging.getLogger(__name__)

# Shown in place of a menu the pane has closed since the bot went down. It
# replaces the buttons, so there is nothing left to press — the #634 rule that a
# control which looks live and is not is worse than no control at all.
_RETIRED_NOTE = (
    "-# 🔁 この質問は bot の再起動より前に終了しました（このボタンは無効です）。"
    "続きが必要なら、あらためてメッセージを送ってください。"
)

# Refusal reasons, in the user's words. Each says what is true of the pane right
# now, because "the session was lost" was said to people whose menu had simply
# been answered in the terminal (#536) and reading a false explanation is worse
# than reading none.
REASON_NO_WINDOW = "この質問の tmux ウィンドウが見つかりませんでした"
REASON_MENU_GONE = "この質問はすでに閉じられています（端末側で回答・取り消し済み）"
REASON_MENU_CHANGED = "ペインの質問が別のものに変わっているため、この回答は送りませんでした"
REASON_NOT_DELIVERED = "キーを送れませんでした（tmux ウィンドウが見つかりません）"
REASON_UNCONFIRMED = "回答は送りましたが、Claude に届いたかを確認できませんでした"


def _for_log(question: AskQuestion, selected: list[str]) -> list[str]:
    """*selected*, with free text reduced to its length.

    A chosen option label is the bot's own text and safe to log. Anything else
    came out of the ✏️ Other modal — which is exactly where a secret gets
    pasted — so only its size is recorded, matching the convention
    ``AskView._other_callback`` and the prompt logging in ``_run_helper`` follow.
    """
    known = {o.label for o in question.options}
    return [s if s in known else f"<free text {len(s)} chars>" for s in selected]


def _fingerprint(question: AskQuestion) -> str:
    """The #633 menu identity — imported lazily to keep the import graph flat."""
    from .thread_state_sync import menu_fingerprint

    return menu_fingerprint(question)


async def _default_runner_factory(bot: Bot, thread_id: int) -> TmuxClaudeRunner | None:
    """Resolve the tmux runner for *thread_id*, the way the watchdog does (#420).

    Always passes ``thread_id`` to ``resolve_tmux_manager``: a thread bound to
    its own repo lives in that repo's session, and omitting it silently answers
    into the parent channel's session instead (#427).
    """
    from .claude.tmux_runner import TmuxClaudeRunner
    from .cogs.channel_repo import ChannelRepoCog

    channel = bot.get_channel(thread_id)
    parent_id = getattr(channel, "parent_id", None) or thread_id
    tmux_manager = None
    channel_cog = bot.get_cog("ChannelRepoCog")
    if isinstance(channel_cog, ChannelRepoCog):
        with contextlib.suppress(Exception):
            tmux_manager = await channel_cog.resolve_tmux_manager(parent_id, thread_id=thread_id)
    if tmux_manager is None:
        tmux_manager = getattr(bot, "tmux_manager", None)
    if tmux_manager is None:
        return None
    return TmuxClaudeRunner(tmux_manager=tmux_manager, thread_id=thread_id)


class PaneMenuAnswerer:
    """Answers a restored menu by typing into the TUI menu still open in the pane.

    Used as the ``recovery`` hook of a re-armed :class:`AskView`: the ask bus has
    no waiter after a restart (the coroutine that would have received the answer
    died with the old process), and this is what the view falls back to instead
    of declaring the answer undeliverable.

    The pane is re-read on every call rather than trusted from startup. Between
    the bot coming up and the click there is unbounded human time — the menu may
    have been answered in the terminal, cancelled, or replaced by the next
    question — and typing ``Down × index`` into whichever menu happens to be open
    would answer a *different* question with this one's choice.
    """

    def __init__(
        self,
        *,
        thread_id: int,
        question: AskQuestion,
        runner_factory: Any,
    ) -> None:
        self._thread_id = thread_id
        self._question = question
        self._runner_factory = runner_factory

    async def __call__(self, selected: list[str], message: Any = None) -> tuple[bool, str]:
        """Deliver *selected*; return ``(delivered, reason)``.

        *message* is the Discord message the menu is drawn on. When given, the
        verified outcome is written back onto it (#651) — the click has already
        been ACKed with the interim ⏳ by then, so this may take the full
        transcript-confirmation wait without tripping Discord's 3-second limit.
        """
        ctx = log_ctx(thread_id=self._thread_id)
        loggable = _for_log(self._question, selected)
        runner = await self._runner_factory(self._thread_id)
        if runner is None:
            logger.warning("%s restart recovery: no tmux runner for this menu (#671)", ctx)
            return False, REASON_NO_WINDOW

        pane_question = await runner.peek_pending_ask()
        if pane_question is None:
            logger.info(
                "%s restart recovery: the pane shows no menu — refusing to type %r (#671)",
                ctx,
                loggable,
            )
            return False, REASON_MENU_GONE
        if _fingerprint(pane_question) != _fingerprint(self._question):
            logger.warning(
                "%s restart recovery: the pane moved on to %r — refusing to type "
                "the answer to %r into it (#671)",
                ctx,
                pane_question.header,
                self._question.header,
            )
            return False, REASON_MENU_CHANGED

        # #651: note which tool_use this menu is BEFORE answering, so the outcome
        # can be read back from Claude's own transcript afterwards.
        project_dir = await _transcript_dir(runner)
        ask_ref = (
            await asyncio.to_thread(latest_ask_tool_use, project_dir)
            if project_dir is not None
            else None
        )

        logger.info(
            "%s restart recovery: typing %r into the still-open menu %r (#671)",
            ctx,
            loggable,
            pane_question.header,
        )
        delivered = await send_answer_keystrokes(runner, pane_question, selected)
        if delivered is False:
            logger.warning("%s restart recovery: keystrokes reached no window (#600)", ctx)
            return False, REASON_NOT_DELIVERED

        if message is None:
            # No message to rewrite ⇒ nothing waits on the confirmation, and the
            # 12s poll would be pure latency. The keys went out; say so.
            return True, ""

        outcome = await _verify_answer_reached_claude(runner, project_dir, ask_ref)
        logger.info("%s restart recovery: outcome=%s for %r (#671)", ctx, outcome, loggable)
        await _finalize_menu_message(message, self._question, selected, outcome)
        return True, "" if outcome == ASK_ANSWERED else REASON_UNCONFIRMED


async def _retire(channel: Any, message_id: int | None) -> bool:
    """Strip a dead menu's buttons in place. True when the message was edited."""
    if message_id is None:
        return False
    try:
        message = await channel.fetch_message(message_id)
    except Exception:
        logger.debug("restart recovery: menu message %s unreachable", message_id, exc_info=True)
        return False
    with contextlib.suppress(Exception):
        await message.edit(content=_RETIRED_NOTE, embed=None, view=None)
        return True
    return False


async def recover_ask_menus(
    bot: Bot,
    ask_repo: PendingAskRepository | None,
    *,
    runner_factory: Any = None,
) -> int:
    """Give every menu a previous process left on screen a live handler again.

    Returns how many menus were re-armed. Never raises and never posts: one
    unreachable thread must not strand the menus in every other thread, and a
    second copy of a menu is the #633 bug this must not become.
    """
    if ask_repo is None:
        return 0
    # A row outlives its menu only when a process died without closing it and
    # the thread is gone too. The live bridge gives up at 24h, so anything twice
    # that age cannot still be answerable — drop it rather than probe a pane for
    # it on every boot. (Until #671 nothing ever called this.)
    with contextlib.suppress(Exception):
        dropped = await ask_repo.cleanup_old()
        if dropped:
            logger.info("restart recovery: dropped %d menu row(s) older than 48h", dropped)
    try:
        records = await ask_repo.list_all()
    except Exception:
        logger.warning("restart recovery: could not read pending menus", exc_info=True)
        return 0
    if not records:
        logger.debug("restart recovery: no ask menus were open when the bot went down")
        return 0

    resolve = runner_factory
    if resolve is None:

        async def resolve(thread_id: int):  # noqa: ANN202 — the built-in default
            return await _default_runner_factory(bot, thread_id)

    rearmed = 0
    for record in records:
        try:
            rearmed += await _recover_one(bot, ask_repo, record, resolve)
        except Exception:
            # One unreachable thread must not strand the menus in every other
            # thread — but it must not be silent either, which is the half of
            # #671 that hid it for a month.
            logger.warning(
                "%s restart recovery: could not re-arm this menu",
                log_ctx(thread_id=getattr(record, "thread_id", 0)),
                exc_info=True,
            )
    logger.info(
        "restart recovery: re-armed %d ask menu(s) left open by the previous process (#671)",
        rearmed,
    )
    return rearmed


async def _recover_one(
    bot: Bot,
    ask_repo: PendingAskRepository,
    record: Any,
    runner_factory: Any,
) -> int:
    """Re-arm one row; retire it instead when its pane no longer shows the menu."""
    thread_id = record.thread_id
    ctx = log_ctx(thread_id=thread_id)
    try:
        raw = record.questions()[record.question_idx]
    except Exception:
        logger.warning("%s restart recovery: unreadable menu row — dropping it", ctx)
        await ask_repo.delete(thread_id)
        return 0
    question = ask_question_from_dict(raw)

    channel = bot.get_channel(thread_id)
    if channel is None:
        with contextlib.suppress(Exception):
            channel = await bot.fetch_channel(thread_id)

    # Re-arm FIRST, unconditionally: whatever the pane says, a press must reach
    # code. Deciding first and registering second would leave a window in which
    # the button is still the silent red-timeout of #671.
    answerer = PaneMenuAnswerer(
        thread_id=thread_id, question=question, runner_factory=runner_factory
    )
    view = AskView(
        question,
        thread_id=thread_id,
        q_idx=record.question_idx,
        ask_repo=ask_repo,
        authorizer=getattr(bot, "authorizer", None),
        recovery=answerer,
    )
    # Bind to the exact message when we know it: a later menu in the same thread
    # reuses the same custom_id (``ask_{thread}_0_*``), and an unbound view would
    # be the fallback handler for that one too.
    if record.message_id is not None:
        bot.add_view(view, message_id=record.message_id)
    else:
        bot.add_view(view)
    logger.info(
        "%s restart recovery: re-armed the menu %r left open by the previous process (#671)",
        ctx,
        question.header,
    )

    runner = await runner_factory(thread_id)
    pane_question = None
    if runner is not None:
        with contextlib.suppress(Exception):
            pane_question = await runner.peek_pending_ask()
    if runner is None or pane_question is None:
        # The episode is over (answered in the terminal, cancelled, or the window
        # is gone). Leaving the buttons up would invite a press that can only be
        # refused — #634's "looks live and is not", applied to ask menus.
        # Duck-typed rather than isinstance-gated: a thread we could only reach
        # through ``fetch_channel`` still edits fine, and _retire already treats
        # every failure as "could not retire this one".
        if channel is not None and await _retire(channel, record.message_id):
            logger.info("%s restart recovery: retired a menu the pane has closed (#671)", ctx)
        await ask_repo.delete(thread_id)
        return 0
    return 1
