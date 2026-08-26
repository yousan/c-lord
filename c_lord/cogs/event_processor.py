"""Event processor for Claude Code stream-json output.

Encapsulates all the state and logic for processing a single Claude Code
CLI session: tracking session IDs, streaming text to Discord, posting tool
embeds, handling AskUserQuestion interrupts, and posting the final result.

This class is extracted from the monolithic run_claude_in_thread() function
so that individual event handlers can be tested in isolation.
"""

from __future__ import annotations

import contextlib
import logging

from ..claude.tmux_runner import NO_RESPONSE_ERROR_PREFIX
from ..claude.types import AskQuestion, MessageType, SessionState, StreamEvent
from ..discord_ui.elicitation_view import ElicitationFormView, ElicitationUrlView
from ..discord_ui.embeds import (
    elicitation_embed,
    permission_embed,
    plan_embed,
    redacted_thinking_embed,
    session_start_embed,
    thinking_embed,
    todo_embed,
    tool_result_embed,
    tool_use_embed,
    unknown_tui_prompt_embed,
)
from ..discord_ui.permission_view import PermissionView
from ..discord_ui.plan_view import PlanApprovalView
from ..discord_ui.progress_folder import ProgressFolder
from ..discord_ui.tool_timer import LiveToolTimer
from ..utils.logger import log_ctx
from .run_config import RunConfig

logger = logging.getLogger(__name__)

# Max characters for tool result display.
# Sized to show ~30 lines of typical output (100 chars/line × 30 = 3000).
# The embed description limit is 4096, so this leaves room for code block markers.
_TOOL_RESULT_MAX_CHARS = 3000

# Caption for a standalone ``progress.txt`` post (#542).  Sent as Discord
# subtext (``-# ``) so it identifies the attachment without adding a full-size
# message to the thread — an attachment on an otherwise empty message tells the
# reader nothing about what the file is.
PROGRESS_ATTACHMENT_NOTE = "-# 📄 progress.txt — このターンのツール実行ログ"


def _truncate_result(content: str) -> str:
    """Truncate tool result content for display."""
    if len(content) <= _TOOL_RESULT_MAX_CHARS:
        return content
    return content[:_TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"


class EventProcessor:
    """Processes stream-json events and dispatches Discord actions.

    One instance per Claude Code session run. Call process(event) for each
    event from the runner; call finalize() in a finally block to clean up.

    State machine:
    - session_start_sent: prevents duplicate session start embeds
    - assistant_text_sent: prevents duplicate result text posts
    - pending_ask: set when AskUserQuestion detected; caller should drain runner

    Example usage (see _run_helper.run_claude_with_config for the full flow)::

        processor = EventProcessor(config)
        try:
            async for event in config.runner.run(prompt):
                if processor.should_drain:
                    continue
                await processor.process(event)
        finally:
            await processor.finalize()

        if processor.pending_ask and processor.session_id:
            # Handle AskUserQuestion (see run_helper)
            ...

        return processor.session_id
    """

    def __init__(self, config: RunConfig) -> None:
        self._config = config
        self._state = SessionState(
            session_id=config.session_id,
            thread_id=config.thread.id,
        )

        # Guards against duplicate embeds/messages in the same run.
        self._session_start_sent: bool = False

        # Set when AskUserQuestion is detected. Caller should drain the runner
        # (skip events) then handle the ask after the stream ends.
        self._pending_ask: list[AskQuestion] | None = None

        # Folds intermediate progress noise into a single progress.txt
        # attachment on the final response message.
        self._folder = ProgressFolder()
        # The most recently sent assistant-text Discord message; the file
        # is attached to it on completion.
        self._last_response_msg = None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        """The current session ID, updated as SYSTEM events arrive."""
        return self._state.session_id

    @property
    def pending_ask(self) -> list[AskQuestion] | None:
        """Set when AskUserQuestion was detected. None otherwise."""
        return self._pending_ask

    @property
    def should_drain(self) -> bool:
        """True while the runner should be drained (AskUserQuestion detected)."""
        return self._pending_ask is not None

    @property
    def assistant_text_sent(self) -> bool:
        """Kept for API compat; always False since Claude posts via the skill."""
        return False

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    @property
    def _notify_mention(self) -> str | None:
        """Message content that pings the turn's poster, or None (#480).

        Interactive prompts (permission / plan / elicitation / ask) block the
        turn awaiting input. Discord only pushes reliably when the *message
        content* carries ``<@id>`` — an embed never pings — so each prompt is
        posted with this content. None when no notify user is configured, in
        which case the prompt is posted with no content (unchanged behaviour).
        """
        uid = self._config.notify_user_id
        return f"<@{uid}>" if uid is not None else None

    async def process(self, event: StreamEvent) -> None:
        """Dispatch a single stream event to the appropriate handler."""
        if event.message_type == MessageType.SYSTEM:
            await self._on_system(event)
        elif event.message_type == MessageType.ASSISTANT:
            await self._on_assistant(event)
        elif event.message_type == MessageType.USER:
            await self._on_tool_result(event)
        elif event.message_type == MessageType.PROGRESS:
            await self._on_progress(event)

        if event.is_complete:
            await self._on_complete(event)

    async def finalize(self) -> None:
        """Cancel any running timers. Call in a finally block."""
        for task in self._state.active_timers.values():
            if not task.done():
                task.cancel()
        self._state.active_timers.clear()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_system(self, event: StreamEvent) -> None:
        """Handle SYSTEM events — capture session_id, post start embed, compact notification."""
        # Context compaction notification
        if event.is_compact and self._config.status:
            await self._config.status.set_compact()
            pre = event.compact_pre_tokens
            trigger = event.compact_trigger or "auto"
            label = f"\U0001f5dc\ufe0f Context compacted ({trigger})"
            if pre:
                label += f" \u2014 was {pre:,} tokens"
            with contextlib.suppress(Exception):
                msg = await self._config.thread.send(f"-# {label}")
                self._folder.track(msg, label)

        # Permission request — show Allow/Deny buttons
        if event.permission_request is not None:
            await self._handle_permission_request(event)
            return

        # MCP elicitation — show form or URL button
        if event.elicitation is not None:
            await self._handle_elicitation(event)
            return

        # In-pane AskUserQuestion (jsonl/tmux mode) — show Discord buttons and
        # answer the open TUI menu by sending keystrokes back to the pane (#166).
        if event.pane_ask is not None:
            await self._handle_pane_ask(event)
            return

        # Unknown TUI interactive prompt — warn Discord so the session doesn't stall silently
        if event.unknown_tui_prompt is not None:
            with contextlib.suppress(Exception):
                await self._config.thread.send(
                    embed=unknown_tui_prompt_embed(event.unknown_tui_prompt)
                )
            return

        if not event.session_id:
            return

        self._state.session_id = event.session_id
        if self._config.repo:
            await self._config.repo.save(
                self._config.thread.id,
                self._state.session_id,
                working_dir=self._config.working_dir,
            )

        # Guard: post session_start_embed only once (Claude can emit multiple SYSTEM events).
        if not self._config.session_id and not self._session_start_sent:
            msg = await self._config.thread.send(embed=session_start_embed(self._state.session_id))
            self._folder.track(msg, f"[session start] {self._state.session_id}", meaningful=False)
            self._session_start_sent = True

    async def _on_assistant(self, event: StreamEvent) -> None:
        """Handle ASSISTANT events — thinking, streaming text, tool use."""
        # Extended thinking — only post on complete events (not partials).
        if event.thinking and not event.is_partial:
            msg = await self._config.thread.send(embed=thinking_embed(event.thinking))
            self._folder.track(msg, f"[thinking]\n{event.thinking}")

        # Redacted thinking — only post on complete events.
        if event.has_redacted_thinking and not event.is_partial:
            msg = await self._config.thread.send(embed=redacted_thinking_embed())
            self._folder.track(msg, "[thinking] (redacted)")

        # Text events are no longer posted to Discord (#53) — Claude pushes
        # its final answer via the discord-reply skill instead. The scrape
        # path that produced these events is retained only for completion
        # detection and tool-use embeds.

        # Tool use — post embed and start live timer.
        if event.tool_use:
            await self._handle_tool_use(event)

        # TodoWrite — post or edit the live todo progress embed.
        if event.todo_list is not None:
            await self._handle_todo_write(event)

        # ExitPlanMode — show plan embed with Approve/Cancel buttons.
        if event.is_plan_approval and not event.is_partial:
            await self._handle_plan_approval(event)

        # AskUserQuestion — set pending and signal caller to interrupt runner.
        if event.ask_questions:
            self._pending_ask = event.ask_questions
            await self._config.runner.interrupt()

    async def _on_tool_result(self, event: StreamEvent) -> None:
        """Handle USER events (tool results) — cancel timer, update embed."""
        if not event.tool_result_id:
            return

        if self._config.status:
            await self._config.status.set_thinking()

        # Cancel the elapsed-time timer for this tool.
        timer_task = self._state.active_timers.pop(event.tool_result_id, None)
        if timer_task and not timer_task.done():
            timer_task.cancel()

        # Update the tool embed with result content.
        tool_msg = self._state.active_tools.get(event.tool_result_id)
        if tool_msg and event.tool_result_content:
            truncated = _truncate_result(event.tool_result_content)
            with contextlib.suppress(Exception):
                await tool_msg.edit(
                    embed=tool_result_embed(
                        tool_msg.embeds[0].title or "",
                        truncated,
                    )
                )
            self._folder.update_tool_result(event.tool_result_id, truncated)

    async def _on_progress(self, event: StreamEvent) -> None:
        """Handle PROGRESS events — reset stall timer (compact in progress)."""
        if self._config.status:
            self._config.status._reset_stall_timer()

    async def _on_complete(self, event: StreamEvent) -> None:
        """Handle RESULT events — update status, save session_id.

        Final response text used to flow through this method into Discord
        via chunk_message + thread.send. Since #53 Claude posts its own
        final answer via the ``discord-reply`` skill (REST API), so we no
        longer post ``event.text`` here. Only the side-effects c-lord still
        owns are kept: error embed, status reaction, session_id persistence.
        """
        from ._run_helper import _make_error_embed

        if event.error:
            # #562: tell the caller this turn produced nothing, so the turn-end
            # ping says "応答がありませんでした" instead of "Claude has finished".
            if event.error.startswith(NO_RESPONSE_ERROR_PREFIX):
                self._config.outcome.no_response = True
            err_msg = await self._config.thread.send(embed=_make_error_embed(event.error))
            if err_msg is not None:
                self._last_response_msg = err_msg
            if self._config.status:
                await self._config.status.set_error()
        else:
            if self._config.status:
                await self._config.status.set_done()

        # Fold collected progress messages into a progress.txt attachment on
        # the final response, then delete the originals.
        await self._fold_progress()

        if event.session_id:
            if self._config.repo:
                await self._config.repo.save(
                    self._config.thread.id,
                    event.session_id,
                    working_dir=self._config.working_dir,
                )
            self._state.session_id = event.session_id

    # ------------------------------------------------------------------
    # Text streaming helpers
    # ------------------------------------------------------------------

    async def _handle_tool_use(self, event: StreamEvent) -> None:
        """Post tool use embed and start the live timer."""
        assert event.tool_use is not None

        self._state.partial_text = ""

        if self._config.status:
            await self._config.status.set_tool(event.tool_use.category)

        embed = tool_use_embed(event.tool_use, in_progress=True)
        try:
            msg = await self._config.thread.send(embed=embed)
        except Exception:
            logger.debug("Failed to send tool embed", exc_info=True)
            return
        self._state.active_tools[event.tool_use.tool_id] = msg
        self._folder.track_tool(
            event.tool_use.tool_id,
            msg,
            f"[tool] {event.tool_use.tool_name} {event.tool_use.tool_input!r}",
        )

        timer = LiveToolTimer(msg, event.tool_use)
        self._state.active_timers[event.tool_use.tool_id] = timer.start()

        await self._bump_stop()

    async def _handle_plan_approval(self, event: StreamEvent) -> None:
        """Post the plan embed with Approve/Cancel buttons (ExitPlanMode)."""
        plan_text = event.text or ""
        embed = plan_embed(plan_text)
        # ExitPlanMode does not carry a request_id in the current CLI protocol;
        # we use the session_id as a stable identifier for the inject payload.
        request_id = self._state.session_id or "plan"
        view = PlanApprovalView(self._config.runner, request_id, authorizer=self._config.authorizer)
        with contextlib.suppress(Exception):
            await self._config.thread.send(content=self._notify_mention, embed=embed, view=view)
        logger.info("Plan approval prompt posted (session=%s)", request_id)

    async def _handle_permission_request(self, event: StreamEvent) -> None:
        """Post permission embed with Allow/Deny buttons."""
        assert event.permission_request is not None
        embed = permission_embed(event.permission_request)
        view = PermissionView(
            self._config.runner, event.permission_request, authorizer=self._config.authorizer
        )
        with contextlib.suppress(Exception):
            await self._config.thread.send(content=self._notify_mention, embed=embed, view=view)
        logger.info(
            "Permission request posted: %s (request_id=%s)",
            event.permission_request.tool_name,
            event.permission_request.request_id,
        )

    async def _handle_pane_ask(self, event: StreamEvent) -> None:
        """Bridge an in-pane AskUserQuestion menu to Discord buttons (#166).

        Blocks until the user answers (or the view times out); the runner is
        suspended at its ``yield`` meanwhile, so the pane is not re-polled.  The
        chosen option is delivered back to the open TUI menu as keystrokes.
        """
        assert event.pane_ask is not None
        runner = self._config.runner
        if not hasattr(runner, "answer_menu"):
            # Non-tmux runner — nothing to answer in a pane.  Skip gracefully.
            logger.warning("pane_ask received but runner cannot answer menus; skipping")
            return
        # #535: this menu may already be on screen — the transcript mirror and
        # the #359 watchdog bridge the same TUI menu from their own triggers.
        # Bridging it again posted a second, identical set of buttons and stole
        # the first bridge's answer queue.  ``bridge_pane_ask`` refuses the
        # duplicate on its own (register is the atomic claim); checking here as
        # well keeps the "bridged and answered" log honest and skips the work.
        from ..discord_ui.ask_bus import ask_bus

        if ask_bus.is_active(self._config.thread.id):
            logger.info(
                "%s pane_ask ignored — another bridge already owns this menu (#535)",
                log_ctx(thread_id=self._config.thread.id),
            )
            return
        from ..discord_ui.ask_handler import bridge_pane_ask

        await bridge_pane_ask(
            self._config.thread,
            event.pane_ask,
            runner,
            ask_repo=self._config.ask_repo,
            authorizer=self._config.authorizer,
            notify_user_id=self._config.notify_user_id,
        )
        logger.info(
            "AskUserQuestion bridged and answered for thread %d",
            self._config.thread.id,
        )

    async def _handle_elicitation(self, event: StreamEvent) -> None:
        """Post elicitation embed with appropriate UI (URL button or form Modal button)."""
        assert event.elicitation is not None
        req = event.elicitation
        embed = elicitation_embed(req)
        if req.mode == "url-mode":
            view = ElicitationUrlView(self._config.runner, req, authorizer=self._config.authorizer)
        else:
            view = ElicitationFormView(self._config.runner, req, authorizer=self._config.authorizer)
        with contextlib.suppress(Exception):
            await self._config.thread.send(content=self._notify_mention, embed=embed, view=view)
        logger.info(
            "Elicitation posted: %s (%s, request_id=%s)",
            req.server_name,
            req.mode,
            req.request_id,
        )

    async def _handle_todo_write(self, event: StreamEvent) -> None:
        """Post or edit the live todo progress embed.

        On the first TodoWrite call the embed is posted as a new message.
        On subsequent calls (Claude updating the list) the same message is
        edited in-place so the user sees a single, live progress view.
        """
        assert event.todo_list is not None

        embed = todo_embed(event.todo_list)
        if self._state.todo_message is None:
            # First time: post a new embed and store the reference.
            with contextlib.suppress(Exception):
                self._state.todo_message = await self._config.thread.send(embed=embed)
            if self._state.todo_message is not None:
                self._folder.track(
                    self._state.todo_message,
                    "[todo]\n" + "\n".join(f"  [{t.status}] {t.content}" for t in event.todo_list),
                )
        else:
            # Subsequent updates: edit in-place.
            with contextlib.suppress(Exception):
                await self._state.todo_message.edit(embed=embed)

    async def _fold_progress(self) -> None:
        """Attach the collected progress transcript to the final response message.

        On success this replaces all the streaming intermediate embeds (thinking,
        tool use/result, todos, etc.) with a single ``progress.txt`` attachment
        on the final response message. The intermediate messages are then deleted
        so the thread stays clean.

        Failures are suppressed — folding is best-effort and must never crash a
        completed run.
        """
        if self._folder.is_empty:
            # Nothing worth reading was collected — do NOT ship a progress.txt
            # whose only line is the session-start banner (#542).  The tracked
            # embeds are still cleaned up, so the thread ends in the same state
            # as a folded turn, just without the empty bubble.
            await self._folder.cleanup_messages()
            return

        file = self._folder.build_file()
        if file is None:
            await self._folder.cleanup_messages()
            return

        attached = False
        if self._last_response_msg is not None:
            try:
                await self._last_response_msg.edit(attachments=[file])
                attached = True
            except Exception:
                logger.debug("Failed to attach progress.txt to final message", exc_info=True)

        if not attached:
            # No final response message (or edit failed): rebuild and send standalone.
            # #542: always carry a one-line caption — a bare attachment on an
            # empty message gives the reader no idea what the file is.
            file = self._folder.build_file()
            if file is not None:
                with contextlib.suppress(Exception):
                    await self._config.thread.send(content=PROGRESS_ATTACHMENT_NOTE, file=file)

        await self._folder.cleanup_messages()

    async def _bump_stop(self) -> None:
        """Move the Stop button to the bottom of the thread if configured."""
        if self._config.stop_view:
            await self._config.stop_view.bump(self._config.thread)
