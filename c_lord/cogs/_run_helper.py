"""Shared helper for running Claude Code CLI and streaming results to a Discord thread.

Both ClaudeChatCog and SkillCommandCog need to run Claude and post results.
This module is the thin orchestration layer that:
1. Builds ephemeral system context (lounge + concurrency notice) via --append-system-prompt
2. Delegates event processing to EventProcessor
3. Handles AskUserQuestion flow (recursive resume)

Primary API:
    run_claude_with_config(config: RunConfig) -> str | None

Legacy shim (kept for backward compatibility):
    run_claude_in_thread(thread, runner, repo, prompt, session_id, ...) -> str | None
"""

from __future__ import annotations

import contextlib
import logging
import re
import time

import discord

from ..claude.context_usage import (
    fallback_window,
    format_context_line,
    read_latest_usage,
)
from ..claude.tmux_runner import TmuxClaudeRunner
from ..discord_ui.ask_handler import (  # noqa: F401
    ASK_ANSWER_TIMEOUT,
    bridge_pane_ask,
    collect_ask_answers,
)
from ..discord_ui.embeds import error_embed, timeout_embed
from ..discord_ui.tool_timer import TOOL_TIMER_INTERVAL, LiveToolTimer  # noqa: F401
from ..lounge import build_lounge_prompt
from ..transcript.resolver import derive_project_dir, latest_session_jsonl
from ..utils.logger import log_ctx
from .event_processor import EventProcessor
from .run_config import RunConfig  # noqa: F401

logger = logging.getLogger(__name__)

# How long to wait for transcript_mirror.reply_sink to register the assistant
# reply before falling back to a fresh send.  jsonl bridge mode polls the
# JSONL on a separate loop, so reply_sink may fire slightly after run_claude
# returns; the line should ride on the same bubble in the common case.
_REPLY_WAIT_ATTEMPTS = 8
_REPLY_WAIT_INTERVAL = 0.2  # 8 × 200ms = up to 1.6 s

# Context-window total per session_id, learned via TmuxClaudeRunner's /context
# probe (or a per-model fallback) and reused for the rest of the session.  The
# value is ``(model, total)``: when the model in the transcript changes (e.g.
# the user runs /model in the pane) the window size may change too, so a model
# mismatch forces a re-probe.  The numerator is re-read every turn regardless.
_context_window_cache: dict[str, tuple[str | None, int]] = {}

# Max characters for tool result display (re-exported for backward compat).
TOOL_RESULT_MAX_CHARS = 3000

_TIMEOUT_PATTERN = re.compile(r"Timed out after (\d+) seconds")


def _make_error_embed(error: str) -> discord.Embed:
    """Return a timeout_embed for timeout errors, error_embed otherwise."""
    m = _TIMEOUT_PATTERN.match(error)
    if m:
        return timeout_embed(int(m.group(1)))
    return error_embed(error)


def _truncate_result(content: str) -> str:
    """Truncate tool result content for display (re-exported for backward compat)."""
    if len(content) <= TOOL_RESULT_MAX_CHARS:
        return content
    return content[:TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"


async def _build_system_context(config: RunConfig) -> str | None:
    """Build ephemeral system context from AI Lounge and concurrency notice.

    Returns a string to inject via --append-system-prompt, or None if no context
    is available. Injecting as a system prompt (rather than prepending to the user
    message) prevents this ephemeral metadata from accumulating in session history,
    which would otherwise cause "Prompt is too long" errors over long conversations.
    """
    parts: list[str] = []

    # Layer 3: AI Lounge context (recent messages + invitation).
    if config.lounge_repo is not None:
        try:
            recent = await config.lounge_repo.get_recent(limit=10)
            lounge_context = build_lounge_prompt(recent)
            parts.append(lounge_context)
            logger.debug("Lounge context built (%d recent message(s))", len(recent))
        except Exception:
            logger.warning("Failed to fetch lounge context — skipping", exc_info=True)

    # Layer 1 + 2: Register session and build concurrency notice.
    if config.registry is not None:
        config.registry.register(config.thread.id, config.prompt[:100], config.runner.working_dir)
        others = config.registry.list_others(config.thread.id)
        notice = config.registry.build_concurrency_notice(config.thread.id)
        parts.append(notice)
        logger.info(
            "Concurrency notice built for thread %d (%d other active session(s), dir=%s)",
            config.thread.id,
            len(others),
            config.runner.working_dir or "(default)",
        )
    else:
        logger.debug(
            "No session registry — concurrency notice skipped for thread %d", config.thread.id
        )

    return "\n\n".join(parts) if parts else None


async def _cleanup_session_dir(config: RunConfig) -> None:
    """Remove the session directory for this thread if it is clean.

    Runs git operations in a thread pool to avoid blocking the event loop.
    Logs the outcome but never raises — cleanup failures are non-fatal.
    """
    import asyncio

    assert config.session_dir_manager is not None  # caller ensures this

    try:
        result = await asyncio.to_thread(
            config.session_dir_manager.cleanup_for_thread,
            config.thread.id,
        )
        if result.removed:
            logger.info(
                "Cleaned up session dir for thread %d: %s",
                config.thread.id,
                result.path,
            )
        elif result.reason == "session directory does not exist":
            # Normal case — session dir was never created
            pass
        else:
            logger.warning(
                "Could not clean up session dir for thread %d (%s): %s",
                config.thread.id,
                result.path,
                result.reason,
            )
            # Notify the Discord thread if there are uncommitted changes
            if "uncommitted changes" in result.reason:
                with contextlib.suppress(Exception):
                    await config.thread.send(
                        f"⚠️ **Session directory not cleaned up** — `{result.path}` has "
                        f"uncommitted changes. Please commit or stash them, then remove "
                        f"the directory manually."
                    )
    except Exception:
        logger.exception(
            "Unexpected error during session dir cleanup for thread %d", config.thread.id
        )


async def _cleanup_tmux_session(config: RunConfig) -> None:
    """Kill the tmux session for this thread. Non-fatal on failure."""
    import asyncio

    assert config.tmux_manager is not None  # caller ensures this

    try:
        await asyncio.to_thread(config.tmux_manager.kill_session, config.thread.id)
    except Exception:
        logger.exception("Unexpected error during tmux cleanup for thread %d", config.thread.id)


async def _cleanup_image_tempfiles(image_paths: list[str]) -> None:
    """Delete image tempfiles downloaded for --image flags."""
    import os

    for path in image_paths:
        with contextlib.suppress(Exception):
            os.unlink(path)
            logger.debug("Deleted image tempfile: %s", path)


async def _resolve_context_window(
    config: RunConfig, session_id: str, model: str | None, used: int = 0
) -> int:
    """Return the context-window total for ``session_id`` running ``model``.

    Learned by scraping ``/context`` (Claude is idle at the prompt after a turn
    completes), then cached.  A re-probe is forced when ``model`` differs from
    the value cached for this session, since the window size can change with the
    model.  Falls back to a per-model default when the runner cannot probe or
    the pane cannot be parsed — ``used`` is passed so the fallback can never
    report a window smaller than the tokens already in it (#292).
    """
    cached = _context_window_cache.get(session_id)
    if cached is not None and cached[0] == model:
        return cached[1]

    total: int | None = None
    probe = getattr(config.runner, "probe_context_window", None)
    if probe is not None:
        with contextlib.suppress(Exception):
            result = await probe()
            total = result if isinstance(result, int) else None
    if total is not None:
        # Cache only successful probes — a transient pane-parse failure must
        # not lock the per-model fallback in for the rest of the session.
        _context_window_cache[session_id] = (model, total)
        return total
    return fallback_window(model or getattr(config.runner, "model", None), used)


async def _post_context_usage(config: RunConfig, session_id: str | None) -> None:
    """Post a subtle context-window usage line after a completed turn.

    Numerator (tokens in context) comes from the session transcript; the
    denominator from :func:`_resolve_context_window`.  Best-effort — any failure
    is swallowed so it never disturbs the turn.
    """
    working_dir = getattr(config.runner, "working_dir", None)
    if not session_id or not working_dir:
        return
    # The session_id stored by c-lord can be a synthetic ``tmux-<thread>`` (skill
    # mode) rather than Claude Code's real UUID, so locate the transcript by
    # picking the most recently written ``*.jsonl`` in the project dir instead
    # of constructing the path from session_id.
    jsonl = latest_session_jsonl(derive_project_dir(working_dir))
    if jsonl is None:
        return
    usage = read_latest_usage(jsonl)
    if usage is None:
        return
    total = await _resolve_context_window(config, session_id, usage.model, usage.used)
    line = format_context_line(usage.used, total)

    # Prefer appending to Claude's last reply message — keeps the addendum
    # inside the same bubble (no fresh avatar/timestamp chrome).  In jsonl
    # bridge mode the transcript_mirror.reply_sink races with run_claude's
    # loop end, so the message may not be registered yet — poll briefly
    # before falling back to a fresh send.
    import asyncio

    from ..skills.reply_tracker import get_last_reply_message

    last_reply = None
    for _ in range(_REPLY_WAIT_ATTEMPTS):
        last_reply = get_last_reply_message(config.thread.id)
        if last_reply is not None:
            break
        await asyncio.sleep(_REPLY_WAIT_INTERVAL)
    # #372: discord.py's Message.edit defaults suppress=False (NOT MISSING), so
    # an edit that omits suppress explicitly *un*-suppresses embeds — which would
    # re-enable the URL OGP card that reply_sink suppressed at send-time. Pass
    # suppress explicitly so appending the context line preserves the setting.
    from ..transcript.mirror import show_url_embeds_enabled

    suppress_embeds = not show_url_embeds_enabled()
    if last_reply is not None:
        existing = last_reply.content or ""
        combined = f"{existing}\n{line}" if existing else line
        if len(combined) <= 2000:
            with contextlib.suppress(discord.HTTPException):
                await last_reply.edit(content=combined, suppress=suppress_embeds)
            return
    with contextlib.suppress(discord.HTTPException):
        await config.thread.send(line, suppress_embeds=suppress_embeds)


async def run_claude_with_config(config: RunConfig) -> str | None:
    """Execute Claude Code CLI and stream results to a Discord thread.

    This is the primary entry point. All Cogs should create a RunConfig and
    pass it here, rather than using the legacy run_claude_in_thread() shim.

    Returns:
        The final session_id, or None if the run failed.
    """
    ctx = log_ctx(thread_id=config.thread.id, session_id=config.session_id)
    logger.info("%s run_claude: enter (prompt=%d chars)", ctx, len(config.prompt))

    # Build system context for side effects (session registry, lounge prompt).
    # The context string itself is not injected — tmux TUI mode does not
    # support --append-system-prompt.
    await _build_system_context(config)

    runner = config.runner
    processor = EventProcessor(config)
    # Issue #67: capture turn start so we can detect whether Claude ever
    # invoked the discord-reply skill before the runner finished.
    turn_started_at = time.monotonic()
    run_errored = False

    try:
        async for event in runner.run(config.prompt, session_id=config.session_id):
            if processor.should_drain:
                continue
            await processor.process(event)
            if event.is_complete and event.error:
                run_errored = True
    except Exception:
        logger.exception("%s Error running Claude CLI", ctx)
        run_errored = True
        # Wrap Discord sends in suppress — the connection may already be closed
        # (e.g. ServerDisconnectedError on bot shutdown), and sending would fail too.
        with contextlib.suppress(Exception):
            await config.thread.send(embed=error_embed("An unexpected error occurred."))
        if config.status:
            with contextlib.suppress(Exception):
                await config.status.set_error()
        return processor.session_id
    finally:
        await processor.finalize()
        if config.registry is not None:
            config.registry.unregister(config.thread.id)
        if config.image_paths:
            await _cleanup_image_tempfiles(config.image_paths)

    from ..skills.injector import skills_enabled

    # #219/#222: the run loop may have finalized just before an AskUserQuestion
    # menu rendered (or Claude called discord-reply and then asked a follow-up),
    # leaving Claude blocked on a TUI menu that was never bridged. Recover it by
    # re-checking the pane. This is INDEPENDENT of bridge mode and of whether
    # discord-reply was called — the menu is read from the pane, not the
    # skill-reply path — so it must run OUTSIDE the skills_enabled()/was_replied
    # guards that gate the #67 notice below. (Gating it there left prod's jsonl
    # mode never recovering a post-turn menu, so the user saw no choices — #222.)
    pending_pane_ask = None
    if (
        not run_errored
        and not processor.pending_ask
        and isinstance(runner, TmuxClaudeRunner)
        and not runner.stopped
    ):
        # ``runner.stopped`` guard (#315): a turn pre-empted by a follow-up message
        # was deliberately torn down; re-bridging its leftover menu here would
        # re-park the run on a fresh ``timeout=None`` await and the new turn could
        # never start (observed on staging — the ⚡ fired but no new run began).
        pending_pane_ask = await runner.peek_pending_ask()

    if pending_pane_ask is not None:
        logger.info(
            "%s Recovered open AskUserQuestion menu post-turn — bridging to Discord",
            ctx,
        )
        await bridge_pane_ask(
            config.thread,
            pending_pane_ask,
            runner,
            ask_repo=config.ask_repo,
        )
    elif not run_errored and not processor.pending_ask and skills_enabled():
        # Issue #67: surface a fallback notice when the skill-reply path was
        # active but Claude never called discord-reply — otherwise the user is
        # left staring at silence. Skill-mode only: in jsonl bridge mode the
        # reply arrives via the transcript mirror (which never calls
        # record_reply), so the tracker is always empty and this would
        # false-fire every turn.
        from ..skills.reply_tracker import was_replied_since

        if not was_replied_since(config.thread.id, turn_started_at):
            logger.warning(
                "%s Claude finished without calling discord-reply — posting fallback notice",
                ctx,
            )
            with contextlib.suppress(Exception):
                await config.thread.send(
                    "-# ⚠️ Claude finished without calling the `discord-reply` skill. "
                    "Check the tmux pane for the response, or retry the turn."
                )

    # After the stream ends, handle pending AskUserQuestion by showing Discord
    # UI and resuming the session with the user's answer.
    if processor.pending_ask and processor.session_id:
        answer_prompt = await collect_ask_answers(
            config.thread,
            processor.pending_ask,
            processor.session_id,
            ask_repo=config.ask_repo,
        )
        if answer_prompt:
            logger.info(
                "%s Resuming after AskUserQuestion answer",
                log_ctx(thread_id=config.thread.id, session_id=processor.session_id),
            )
            return await run_claude_with_config(config.with_prompt(answer_prompt))

    if not run_errored:
        await _post_context_usage(config, processor.session_id)

    logger.info(
        "%s run_claude: exit",
        log_ctx(thread_id=config.thread.id, session_id=processor.session_id),
    )
    return processor.session_id


async def run_claude_in_thread(
    thread: discord.Thread,
    runner,
    repo,
    prompt: str,
    session_id: str | None,
    status=None,
    registry=None,
    ask_repo=None,
    lounge_repo=None,
) -> str | None:
    """Backward-compatible shim. Prefer run_claude_with_config() for new code."""
    config = RunConfig(
        thread=thread,
        runner=runner,
        prompt=prompt,
        session_id=session_id,
        repo=repo,
        status=status,
        registry=registry,
        ask_repo=ask_repo,
        lounge_repo=lounge_repo,
    )
    return await run_claude_with_config(config)
