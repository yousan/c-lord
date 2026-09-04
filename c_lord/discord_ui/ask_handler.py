"""AskUserQuestion interaction handler.

Handles the full lifecycle of AskUserQuestion tool calls from Claude Code:
- Saving question state to DB for restart recovery
- Showing Discord buttons via AskView
- Waiting for user answers (up to 24 hours)
- Returning the formatted answer prompt for Claude to resume

It also bridges the *in-pane* AskUserQuestion menu in jsonl/tmux mode
(``bridge_pane_ask``): there Claude is blocked on a TUI menu, so the answer is
delivered by sending menu keystrokes back to the pane rather than resuming with
a new prompt (#166).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from ..claude.types import AskQuestion
from ..database.ask_repo import PendingAskRepository
from ..transcript.ask_result import (
    ASK_ANSWERED,
    ASK_NOT_ANSWERED,
    ASK_UNKNOWN,
    AskOutcome,
    classify_ask_result,
    latest_ask_tool_use,
    read_ask_result,
)
from .ask_bus import (
    CLOSE_ANSWERED,
    CLOSE_INTERRUPTED,
    CLOSE_TERMINAL,
    CLOSE_TIMEOUT,
)
from .ask_bus import ask_bus as _ask_bus
from .ask_menus import ask_menus as _ask_menus
from .ask_view import AskView
from .authorization import Authorizer
from .bridged_context import bridged_context as _bridged_context
from .embeds import (
    ask_answered_embed,
    ask_embed,
    ask_unconfirmed_embed,
    ask_undelivered_embed,
)

if TYPE_CHECKING:
    from ..claude.tmux_runner import TmuxClaudeRunner

logger = logging.getLogger(__name__)

# How long to wait for the user to answer (seconds).  24 hours lets users
# step away for the day and come back without "Interaction Failed" errors.
ASK_ANSWER_TIMEOUT = 86_400  # 24 h

# #359: while waiting for a Discord click, also watch the tmux pane.  The human
# can answer/cancel the menu directly in the pane (tmux attach); without this
# watch the bridge would wait for a click for ``ASK_ANSWER_TIMEOUT`` while the
# suspended ``run_claude`` holds the thread lock — freezing the whole thread.
# Poll ``peek_pending_ask`` every ``_PANE_RESOLVE_POLL`` seconds and treat the
# menu as resolved-elsewhere after ``_PANE_RESOLVE_MISSES`` consecutive misses
# (a small dwell guards against a transient half-drawn capture).
_PANE_RESOLVE_POLL = 2.0
_PANE_RESOLVE_MISSES = 2

# #651: after the answer keystrokes go out, wait this long for Claude's own
# transcript to record what the menu resolved to.  Bounded on purpose: the ✅ is
# held back until the outcome is known, so this is latency the user sees.
# Measured on staging, the tool_result lands ~1s after the keys — 12s is slack
# for a busy host, not an expected wait.
_ANSWER_CONFIRM_TIMEOUT = 12.0
_ANSWER_CONFIRM_POLL = 0.5

# #399: the prose context above the menu is posted as its own message(s).
# Up to _CONTEXT_MAX_MSGS sequential chunks deliver the text IN FULL — clipping
# while registering the full text would suppress the flush and lose the
# clipped part forever (review blocker 1). Beyond the budget the TAIL is kept
# (the recommendation 推し is conventionally last) and the text is NOT
# registered, so the later jsonl flush delivers the full text as a late
# duplicate — degraded mode is duplication, never loss.
_CONTEXT_MSG_LIMIT = 1900
_CONTEXT_MAX_MSGS = 3


def _context_chunks(text: str) -> tuple[list[str], bool]:
    """Split *text* into ≤``_CONTEXT_MSG_LIMIT`` chunks. Returns (chunks, truncated)."""
    budget = _CONTEXT_MSG_LIMIT * _CONTEXT_MAX_MSGS
    truncated = len(text) > budget
    if truncated:
        text = text[-budget:]
    chunks = [text[i : i + _CONTEXT_MSG_LIMIT] for i in range(0, len(text), _CONTEXT_MSG_LIMIT)]
    if truncated and chunks:
        chunks[0] = "…" + chunks[0]
    return chunks, truncated


def _close(thread_id: int, reason: str, message: object | None = None) -> None:
    """Mark this thread's menu closed for *reason* and stop tracking *message* (#536).

    Recording the reason is what lets a late click say something true; forgetting
    the message keeps a resolved menu from being blanked a second time as if it
    were a stale copy.
    """
    _ask_bus.note_closed(thread_id, reason)
    _ask_menus.forget(thread_id, getattr(message, "id", None))


def _mention(user_id: int | None) -> str | None:
    """Message content that pings *user_id*, or None (#480).

    A blocked AskUserQuestion menu only pushes a notification when the *message
    content* (not the embed) carries ``<@id>``. None ⇒ post without a mention.
    """
    return f"<@{user_id}>" if user_id is not None else None


def _answer_undeliverable_notice(selected: list[str]) -> str:
    """Told to the thread when the chosen answer never reached Claude (#600).

    Echo the choice back: the menu is gone from Discord's side, so without this
    the person's decision is simply lost and they have to guess what they picked.
    """
    choice = " / ".join(s for s in selected if s) or "(選択なし)"
    return (
        "-# ⚠️ 選んだ回答を Claude に届けられませんでした"
        f"（選択: {choice}）。"
        "スレッドの tmux ウィンドウが見つかりませんでした。"
        "もう一度送るか、`/tmux-screenshot` でセッションの状態を確認してください。"
    )


_NOT_ANSWERED_REASON = (
    "キーは送れましたが、Claude 側には「回答なし」として渡りました"
    "（メニューが回答を受け取らずに閉じています）"
)
_NO_WINDOW_REASON = "スレッドの tmux ウィンドウが見つかりませんでした"


async def _transcript_dir(runner: TmuxClaudeRunner) -> Path | None:
    """The runner's transcript directory, or None when it cannot be determined."""
    getter = getattr(runner, "transcript_project_dir", None)
    if not callable(getter):
        return None
    try:
        result = getter()
        if inspect.isawaitable(result):
            result = await result
    except Exception:  # pragma: no cover - defensive: never fail a turn on this
        logger.debug("could not resolve the transcript dir for verification", exc_info=True)
        return None
    return result if isinstance(result, Path) else None


async def _menu_is_gone(runner: TmuxClaudeRunner) -> bool | None:
    """True/False if the pane says the menu closed, None when it cannot say.

    Every step is defensive: this runs on the answer path, where an exception
    would strand the menu in its interim state — worse than the missing check
    it replaces.
    """
    try:
        state = getattr(runner, "peek_menu_state", None)
        if callable(state):
            result = state()
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, tuple) and len(result) == 2:
                menu, capture_ok = result
                # #485: an empty capture is "unknown", never "the menu closed".
                return None if not capture_ok else menu is None
        peek = getattr(runner, "peek_pending_ask", None)
        if callable(peek):
            result = peek()
            if inspect.isawaitable(result):
                result = await result
            return result is None
    except Exception:  # pragma: no cover - defensive
        logger.debug("pane check failed while confirming the answer", exc_info=True)
    return None


async def _verify_answer_reached_claude(
    runner: TmuxClaudeRunner,
    project_dir: Path | None,
    ask_ref: tuple[str, Path] | None,
) -> AskOutcome:
    """Did the answer actually reach Claude? (#651)

    Evidence, best first:

    1. **The transcript.** Claude Code records the menu's ``tool_result``, and it
       states outright whether the user answered or the tool was rejected. This
       is the only evidence that is about the *answer*; everything else is about
       the machinery around it. It is what distinguishes the #650 failure — keys
       delivered, menu closed, answer discarded — from a real answer.
    2. **The pane.** Where there is no transcript to read (no tmux pane path, a
       non-tmux runner), "the menu is gone" is the best available proxy. Weaker,
       but far better than the pre-#651 answer of not checking at all.

    Polling is bounded by ``_ANSWER_CONFIRM_TIMEOUT`` because the ✅ waits on it.
    """
    deadline = asyncio.get_running_loop().time() + _ANSWER_CONFIRM_TIMEOUT
    while True:
        if project_dir is not None and ask_ref is not None:
            tool_use_id, session_path = ask_ref
            outcome = classify_ask_result(
                await asyncio.to_thread(read_ask_result, project_dir, tool_use_id, session_path)
            )
            if outcome != ASK_UNKNOWN:
                return outcome
        elif await _menu_is_gone(runner) is True:
            # No transcript to consult — the menu closing is all we have.
            return ASK_ANSWERED
        if asyncio.get_running_loop().time() >= deadline:
            return ASK_UNKNOWN
        await asyncio.sleep(_ANSWER_CONFIRM_POLL)


async def _finalize_menu_message(
    msg, question: AskQuestion, selected: list[str], outcome: AskOutcome, reason: str = ""
) -> None:
    """Replace the menu with what actually happened to the answer (#651).

    The menu message is the one place a reader looks back at to see what was
    asked and what became of it, so the verified outcome belongs there — not in
    a separate line that scrolls away from the question it belongs to.
    """
    if outcome == ASK_ANSWERED:
        embed = ask_answered_embed(question.question, question.header, selected)
    elif outcome == ASK_NOT_ANSWERED:
        embed = ask_undelivered_embed(
            question.question, question.header, selected, reason or _NOT_ANSWERED_REASON
        )
    else:
        embed = ask_unconfirmed_embed(question.question, question.header, selected)
    # Never let the report itself break the turn: a menu stuck in its interim
    # state is worse than the missing check this replaces.
    with contextlib.suppress(Exception):
        await msg.edit(content=None, embed=embed, view=None)


async def _report_answer_delivery(
    thread: discord.Thread, *, delivered: bool, selected: list[str]
) -> None:
    """Say something only when the answer did not land (#600).

    A delivered answer needs no message — the menu closing is the feedback. A
    lost one previously produced nothing at all, which is why a stalled thread
    went unnoticed for two days.
    """
    if delivered:
        return
    logger.warning(
        "ask answer could not be delivered to the TUI (thread=%d selected=%r) (#600)",
        thread.id,
        selected,
    )
    with contextlib.suppress(Exception):
        await thread.send(_answer_undeliverable_notice(selected))


async def bridge_pane_ask(
    thread: discord.Thread,
    question: AskQuestion,
    runner: TmuxClaudeRunner,
    ask_repo: PendingAskRepository | None = None,
    authorizer: Authorizer | None = None,
    notify_user_id: int | None = None,
) -> None:
    """Bridge one in-pane AskUserQuestion menu to Discord buttons (#166).

    Shows an :class:`AskView`, waits for the user's choice, then answers the
    still-open TUI menu by sending keystrokes via *runner*:

    - a real option → ``runner.answer_menu(index)`` (Down×index + Enter)
    - free text ("✏️ Other") → ``runner.answer_menu_text`` on whichever
      affordance this menu has: the "Type something." row that follows the real
      options, or (preview layout) the ``Notes:`` field opened with ``n`` (#650)
    - timeout / no answer → ``runner.cancel_menu()`` (Esc)
    """
    # #535: registering IS claiming the menu. Several paths can spot the same
    # TUI menu (this poll-loop bridge, the transcript mirror, the #359
    # watchdog); the first one to register owns it and everyone else returns
    # here — before posting anything, so no second set of buttons and no second
    # copy of the pre-menu context reaches the thread. Returning immediately
    # also keeps the loser out of the 24h await that used to wedge the thread.
    # #536 AC7: a typed sentence may answer this menu only when the TUI has a
    # free-text row — ``allow_other`` is exactly that flag (plan-approval menus
    # set it False, and keystrokes typed into one would land nowhere useful).
    answer_queue = _ask_bus.register(thread.id, allow_free_text=question.allow_other)
    if answer_queue is None:
        logger.info(
            "bridge_pane_ask: thread %d already has an active menu bridge — declining",
            thread.id,
        )
        return

    # #535: from here on the thread is CLAIMED. Everything below therefore runs
    # under a single release-on-exit guard: the claim is taken before the menu
    # is posted, so a failure in between (a Discord outage on the send, a #315
    # pre-emption cancel) must still give it back. A leaked claim used to be
    # harmless — the next bridge simply overwrote the waiter — but now it would
    # silently block every future menu in this thread.
    try:
        await _bridge_claimed_menu(
            thread,
            question,
            runner,
            answer_queue,
            ask_repo=ask_repo,
            authorizer=authorizer,
            notify_user_id=notify_user_id,
        )
    finally:
        _ask_bus.unregister(thread.id)


async def _bridge_claimed_menu(
    thread: discord.Thread,
    question: AskQuestion,
    runner: TmuxClaudeRunner,
    answer_queue: asyncio.Queue[list[str]],
    ask_repo: PendingAskRepository | None = None,
    authorizer: Authorizer | None = None,
    notify_user_id: int | None = None,
) -> None:
    """Body of :func:`bridge_pane_ask`, run with the thread's menu claim held.

    Split out so the claim's lifetime is one ``try/finally`` in the caller
    rather than something every early return has to remember (#535).
    """
    # #399: deliver the prose Claude spoke right above the menu (経緯・推し) as
    # its own silent message BEFORE the menu embed. The CLI buffers the jsonl
    # chunk containing the menu until resolution, so the transcript mirror
    # cannot deliver this text while the menu is open — and the embed message
    # is wiped (``embed=None``) once the menu resolves, so the context must
    # live in a message of its own to stay readable afterwards. Register the
    # delivered text so the mirror suppresses the CLI's post-resolution flush
    # of the same text (AC3); on send failure nothing is registered — the
    # late flush then delivers it instead.
    #
    # Order-independence (plan path): ExitPlanMode flushes the prose as a normal
    # text event BEFORE the menu, so the mirror may have already posted it. Skip
    # our own post when a mirror-sourced delivery of the same text exists, else
    # we double-post (the dedup is bidirectional — see bridged_context).
    #
    # #680: one AskUserQuestion may carry several questions. The CLI draws them
    # one menu at a time, so this runs once per question — each time re-reading
    # the SAME prose from the pane. The cross-source dedup above cannot catch
    # that (it ignores our own entries by design), so ask the pane-side ledger
    # whether this text is already in the thread.
    if question.context and _bridged_context.already_delivered(thread.id, question.context):
        logger.info(
            "bridge_pane_ask: pre-menu prose already in the thread — skipping "
            "duplicate (thread=%d context_chars=%d) (#680)",
            thread.id,
            len(question.context),
        )
    elif question.context:
        if _bridged_context.consume_match(thread.id, question.context, source="mirror"):
            # The mirror delivered it. That entry is one-shot, so without the
            # ledger the NEXT question of this same ask would find nothing to
            # match and post the pane's copy on top of the mirror's (#680).
            _bridged_context.note_delivered(thread.id, question.context)
        else:
            chunks, truncated = _context_chunks(question.context)
            posted: list[discord.Message] = []
            try:
                for chunk in chunks:
                    posted.append(await thread.send(content=chunk, silent=True))
            except discord.HTTPException:
                logger.warning(
                    "bridge_pane_ask: context post failed for thread %d", thread.id, exc_info=True
                )
            else:
                # It is in the thread now — later questions of this same ask
                # must not post it again (#680). Recorded even when truncated:
                # a partial delivery is still a delivery, and re-posting it is
                # still a copy.
                _bridged_context.note_delivered(thread.id, question.context)
                # Register only when the FULL text was delivered: suppressing
                # the flush twin of a partially-posted text would lose the rest.
                # #686: the messages ride along, so the side that suppresses the
                # flush can rewrite this TUI rendering into the CLI's markdown
                # instead of dropping the only readable copy.
                if not truncated:
                    _bridged_context.register(
                        thread.id, question.context, source="pane", messages=posted
                    )

    view = AskView(question, thread_id=thread.id, q_idx=0, ask_repo=ask_repo, authorizer=authorizer)
    embed = ask_embed(question.question, question.header, question.options, question.multi_select)
    if not question.context:
        # #549: with no readable 経緯 the menu looks like a question asked out of
        # nowhere, and the prose then turns up AFTER the answer looking like a
        # new statement. The pane is the only source while the menu is open (the
        # CLI buffers the jsonl chunk until resolution — measured on staging:
        # nothing of the turn is written for 90s, then all of it at once), so
        # when the tall re-capture also comes up empty there is nothing to post
        # first. Say that, rather than leaving the gap unexplained.
        embed.set_footer(text="この質問の経緯は、回答後にまとめて届きます")
    msg = await thread.send(
        content=_mention(notify_user_id),
        embed=embed,
        view=view,
    )
    # #536: while this menu is answerable it must be reachable, so that
    # resolving any OTHER copy can blank this one out (and vice versa).
    _ask_menus.register(thread.id, msg)

    resolved_note = "-# ✅ 端末で回答済み（このボタンは無効です）"

    async def _wait_tui_resolved() -> None:
        """Return once the menu is no longer open in the pane (#359).

        The human can answer/cancel the menu directly in the tmux pane.  Without
        this the bridge would wait for a Discord click for ``ASK_ANSWER_TIMEOUT``
        while the suspended ``run_claude`` holds the thread lock, freezing it.
        """
        if not hasattr(runner, "peek_pending_ask"):
            await asyncio.sleep(ASK_ANSWER_TIMEOUT)  # non-tmux runner: click-only
            return
        misses = 0
        while misses < _PANE_RESOLVE_MISSES:
            await asyncio.sleep(_PANE_RESOLVE_POLL)
            # #485: an EMPTY pane capture (window mapping momentarily unresolved
            # under concurrent-session churn) is NOT evidence the menu closed.
            # Counting it as a miss is what falsely resolved a still-open menu,
            # which the next reply then selected. Only count a miss when the
            # capture succeeded and showed no menu; ignore empty captures.
            if hasattr(runner, "peek_menu_state"):
                menu, capture_ok = await runner.peek_menu_state()
                if not capture_ok:
                    continue  # unknown — keep waiting, do not count as gone
                misses = misses + 1 if menu is None else 0
            elif await runner.peek_pending_ask() is None:
                misses += 1
            else:
                misses = 0

    click_task: asyncio.Future = asyncio.ensure_future(answer_queue.get())
    tui_task: asyncio.Future = asyncio.ensure_future(_wait_tui_resolved())
    try:
        done, _ = await asyncio.wait(
            {click_task, tui_task},
            timeout=ASK_ANSWER_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (click_task, tui_task):
            t.cancel()

    # #536: every exit below records WHY the menu stopped accepting answers.
    # A click that lands afterwards (a stale copy, a slow finger) is then told
    # the truth instead of the blanket "the bot was restarted" it used to get.
    if not done:
        # 24h timeout with the menu still open → dismiss it.
        _close(thread.id, CLOSE_TIMEOUT, msg)
        with contextlib.suppress(discord.HTTPException):
            await msg.edit(
                content="-# ⏰ Question timed out — send a new message to continue.",
                embed=None,
                view=None,
            )
        await runner.cancel_menu()
        return

    if click_task not in done:
        # Menu was answered/cancelled directly in the pane — buttons are stale.
        _close(thread.id, CLOSE_TERMINAL, msg)
        with contextlib.suppress(discord.HTTPException):
            await msg.edit(content=resolved_note, embed=None, view=None)
        return

    selected = click_task.result()
    # The menu may have been resolved in the TUI in the same instant the click
    # arrived; sending keystrokes then would leak into the idle prompt (#359).
    if hasattr(runner, "peek_pending_ask") and await runner.peek_pending_ask() is None:
        _close(thread.id, CLOSE_TERMINAL, msg)
        with contextlib.suppress(discord.HTTPException):
            await msg.edit(content=resolved_note, embed=None, view=None)
        return

    if not selected:
        # An EMPTY answer is the #315 pre-emption signal, not a choice. The menu
        # is about to be Esc'd away, so its buttons must go with it (#536) —
        # leaving them live is what produced menus that stayed clickable long
        # after the question was gone.
        _close(thread.id, CLOSE_INTERRUPTED, msg)
        with contextlib.suppress(discord.HTTPException):
            await msg.edit(
                content="-# ⚡ 新しい指示が届いたので、この質問は取り消しました。",
                embed=None,
                view=None,
            )
        await runner.cancel_menu()
        return

    _close(thread.id, CLOSE_ANSWERED, msg)

    # #651: identify the menu in Claude's own transcript BEFORE answering it, so
    # the outcome can be read back from the authoritative place. Done up front
    # because after the answer a *new* menu may already be the newest one.
    project_dir = await _transcript_dir(runner)
    ask_ref = (
        await asyncio.to_thread(latest_ask_tool_use, project_dir)
        if project_dir is not None
        else None
    )

    labels = [opt.label for opt in question.options]
    indices = [labels.index(s) for s in selected if s in labels]
    if question.multi_select and indices:
        # multiSelect: toggle every chosen option and Submit (#418).  Using
        # answer_menu (single-select) here dropped all but selected[0].
        delivered = await runner.answer_menu_multi(indices, len(question.options))
    elif selected[0] in labels:
        delivered = await runner.answer_menu(labels.index(selected[0]))
    else:
        # Free text from the "Other" modal.  How it is typed depends on the
        # layout the parser read off the pane (#650): the classic menu numbers a
        # "Type something." row immediately after the real options, while a
        # preview menu has no such row and takes the text in its ``Notes:``
        # field instead.  Sending the wrong one answers "(No answer provided)".
        delivered = await runner.answer_menu_text(
            len(question.options), selected[0], mode=question.free_text_mode
        )
    # #600: the keystrokes can go nowhere (thread with no tmux window). Saying so
    # is what keeps the menu from silently staying open and being re-posted.
    await _report_answer_delivery(thread, delivered=delivered is not False, selected=selected)

    # #651: keystrokes accepted by tmux is NOT the same as the answer reaching
    # Claude — #650 delivered every key, closed the menu, and still recorded
    # "(No answer provided)". Confirm before the menu is allowed to read as
    # answered.
    if delivered is False:
        await _finalize_menu_message(msg, question, selected, ASK_NOT_ANSWERED, _NO_WINDOW_REASON)
        return
    outcome = await _verify_answer_reached_claude(runner, project_dir, ask_ref)
    if outcome != ASK_ANSWERED:
        logger.warning(
            "ask answer outcome=%s for thread=%d (selected=%r) (#651)",
            outcome,
            thread.id,
            selected,
        )
    await _finalize_menu_message(msg, question, selected, outcome)


async def collect_ask_answers(
    thread: discord.Thread,
    questions: list[AskQuestion],
    session_id: str,
    ask_repo: PendingAskRepository | None = None,
    authorizer: Authorizer | None = None,
    notify_user_id: int | None = None,
) -> str | None:
    """Show Discord UI for each question and return the formatted answer string.

    Processes questions sequentially (one at a time).  For each question:
    1. Saves it to the DB (for bot-restart recovery).
    2. Registers a Queue with ask_bus and shows the AskView.
    3. Awaits the answer for up to 24 hours via asyncio.wait_for.
    4. Cleans up the DB entry once answered or timed out.

    Returns a human-readable string to inject as the next human turn, or None
    if no question received an answer.
    """
    # Serialise questions once for DB storage.
    questions_dicts = [
        {
            "question": q.question,
            "header": q.header,
            "multi_select": q.multi_select,
            "options": [{"label": o.label, "description": o.description} for o in q.options],
        }
        for q in questions
    ]

    parts: list[str] = []
    for q_idx, q in enumerate(questions):
        # Persist so on_ready can re-register the view after a bot restart.
        if ask_repo is not None:
            await ask_repo.save(
                thread_id=thread.id,
                session_id=session_id,
                questions=questions_dicts,
                question_idx=q_idx,
            )

        # Register a waiter in the bus before showing the view so there is no
        # race between the user clicking and the queue being registered.
        # #535: a None means another bridge already has this thread's menu on
        # screen. Posting a second copy is the bug we are fixing, and its
        # answers would go to the owner's queue anyway — so skip this question
        # and let the owner's menu collect the answer.
        answer_queue = _ask_bus.register(thread.id)
        if answer_queue is None:
            logger.warning(
                "collect_ask_answers: thread %d already has an active menu bridge — "
                "skipping duplicate menu for q_idx=%d",
                thread.id,
                q_idx,
            )
            continue

        view = AskView(
            q, thread_id=thread.id, q_idx=q_idx, ask_repo=ask_repo, authorizer=authorizer
        )
        msg = await thread.send(
            content=_mention(notify_user_id),
            embed=ask_embed(q.question, q.header, q.options, q.multi_select),
            view=view,
        )
        _ask_menus.register(thread.id, msg)  # #536: keep stale copies addressable

        try:
            selected = await asyncio.wait_for(answer_queue.get(), timeout=ASK_ANSWER_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 — asyncio.TimeoutError != builtins.TimeoutError on Python 3.10
            _close(thread.id, CLOSE_TIMEOUT, msg)
            _ask_bus.unregister(thread.id)
            if ask_repo is not None:
                await ask_repo.delete(thread.id)
            # Remove buttons from the timed-out message so they stay inert.
            with contextlib.suppress(discord.HTTPException):
                await msg.edit(
                    content="-# ⏰ Question timed out — please send a new message to continue.",
                    embed=None,
                    view=None,
                )
            logger.info(
                "AskUserQuestion timed out after %ds for thread %d: %r",
                ASK_ANSWER_TIMEOUT,
                thread.id,
                q.question,
            )
            continue
        finally:
            _ask_bus.unregister(thread.id)

        if ask_repo is not None:
            await ask_repo.delete(thread.id)

        if not selected:
            _close(thread.id, CLOSE_INTERRUPTED, msg)
            continue

        _close(thread.id, CLOSE_ANSWERED, msg)
        # #651: on this path the answer needs no verification — it is returned
        # from here and injected as Claude's next prompt, so it cannot be lost
        # in a menu. Say so, rather than leaving the click's interim ⏳ standing.
        await _finalize_menu_message(msg, q, selected, ASK_ANSWERED)

        answer_text = ", ".join(selected)
        parts.append(f"**{q.question}**\nAnswer: {answer_text}")

    if not parts:
        return None

    return (
        "[Response to AskUserQuestion]\n\n"
        + "\n\n".join(parts)
        + "\n\nPlease continue based on these answers."
    )
