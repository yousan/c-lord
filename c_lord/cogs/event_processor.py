"""Turns the runner's events into what c-lord itself posts for one turn.

The runner (``TmuxClaudeRunner``) yields two kinds of event: SYSTEM (the
session id, a TUI menu to bridge, an unrecognised TUI prompt) and RESULT (the
turn ended — cleanly, with an error, or on a plan limit).  This module records
the session id, bridges the menu, and on RESULT sets the lamp, posts the error
embed and takes the turn's session-start embed away again.

Claude's own words and its tool activity never pass through here — the
transcript mirror reads them from Claude Code's jsonl (#712, #723).  The
embeds, views and timer that once rendered tool use, thinking, todo lists,
permission / plan / elicitation prompts and compaction from runner events were
removed in #1316: nothing constructed those events any more.
# <<REDACTED 6 lines>>
from typing import TYPE_CHECKING
# <<REDACTED 1 lines>>
from ..api_error import API_ERROR_PREFIX, advice_for_error
from ..claude.tmux_runner import (
    FLEET_TMUX_RESTART_ERROR_PREFIX,
    LOGIN_REQUIRED_ERROR_PREFIX,
    NO_RESPONSE_ERROR_PREFIX,
    STOPPED_BY_USER_ERROR,
    TRUST_START_FAILED_ERROR_PREFIX,
    TRUST_STUCK_ERROR_PREFIX,
    unavailable_model,
)
# <<REDACTED 1 lines>>
from ..discord_ui.embeds import session_start_embed, unknown_tui_prompt_embed
from ..login_alert import login_alert
from ..usage_limit import usage_limit_notices
from ..utils.logger import log_ctx
# <<REDACTED 2 lines>>
if TYPE_CHECKING:
    import discord
# <<REDACTED 5 lines>>
    """Handles one turn's runner events and the Discord posts they call for.
# <<REDACTED 1 lines>>
    One instance per turn. Call process(event) for each event from the runner;
    call finalize() in a finally block.
# <<REDACTED 1 lines>>
    State:
    # <<REDACTED 30 lines>>
        # #856: ``RunConfig.on_session_saved`` fires once per turn.
        self._session_saved_notified: bool = False
# <<REDACTED 5 lines>>
        # The session-start embed this turn posted, if any. It is bookkeeping,
        # so it is taken away again when the turn ends.
        self._session_start_msg: discord.Message | None = None

    # <<REDACTED 23 lines>>
    @property
    def _failure_mention(self) -> str | None:
        """Message content that pings whoever is waiting on a broken turn (#681).

        The ❌ embed used to be posted bare. An embed never pushes, so a turn
        that died at startup reached nobody — #677 and #678 each ended on this
        exact message and were not noticed for two days. Falls back to
        ``notify_user_id`` so a caller that has not been taught the new field
        still pings the same person it would have pinged for a parked turn,
        rather than silently regressing to no mention at all.
        """
        uid = self._failure_notify_uid
        return f"<@{uid}>" if uid is not None else None

    @property
    def _failure_notify_uid(self) -> int | None:
        """Who :attr:`_failure_mention` pings (#681), or ``None``."""
        uid = self._config.failure_notify_id
        if uid is None:
            uid = self._config.notify_user_id
        return uid

    # <<REDACTED 11 lines>>
        """End-of-turn hook. Call in a finally block.

        Holds nothing to release since #1316 (it used to cancel the per-tool
        timers); kept so callers have one place to end a turn.
        """
# <<REDACTED 6 lines>>
        """Handle SYSTEM events — a TUI menu, an unknown prompt, or the session id."""
        # In-pane AskUserQuestion (jsonl/tmux mode) — show Discord buttons and
        # answer the open TUI menu by sending keystrokes back to the pane (#166).
        if event.pane_ask is not None:
            await self._handle_pane_ask(event)
            return

        # Unknown TUI interactive prompt — warn Discord so the session doesn't stall silently
        if event.unknown_tui_prompt is not None:
            # <<REDACTED 1 lines>>
                await self._config.thread.send(
                    embed=unknown_tui_prompt_embed(event.unknown_tui_prompt)
                )
            return

        # <<REDACTED 5 lines>>
            await self._config.repo.save(
                self._config.thread.id,
                self._state.session_id,
                working_dir=self._config.working_dir,
            )
            # #856: once per turn — the row now exists, so the naming pass's
            # stash can be written. A failure here is the hook's to log.
            hook = self._config.on_session_saved
            if hook is not None and not self._session_saved_notified:
                self._session_saved_notified = True
                # <<REDACTED 1 lines>>
                    await hook(self._config.thread.id)
# <<REDACTED 3 lines>>
            self._session_start_msg = await self._config.thread.send(
                embed=session_start_embed(self._state.session_id)
            )
            # <<REDACTED 3 lines>>
        """Handle ASSISTANT events — an AskUserQuestion carried on the event itself."""
        # <<REDACTED 6 lines>>
        """Handle RESULT events — the turn is over.
# <<REDACTED 1 lines>>
        Sets the lamp (🟡 / ❌), posts the error embed when the turn failed and
        records why on ``RunConfig.outcome``, removes the session-start embed,
        and saves the session id. Claude's answer is not posted here: the
        transcript mirror delivers it from the jsonl (#712).
        """
        # <<REDACTED 2 lines>>
        if event.error == STOPPED_BY_USER_ERROR:
            # #878: the user asked for this. The stop reply (⏹️ Session stopped)
            # already says so — an ❌ and an error embed with a failure ping
            # would report a requested stop as a breakdown. 🟡: their turn now.
            # <<REDACTED 2 lines>>
        elif event.error:
            # #562: tell the caller this turn produced nothing, so the turn-end
            # ping says "応答がありませんでした" instead of "Claude has finished".
            # #630: a turn blocked on the trust dialog also produced nothing, so
            # it must not be announced as finished either. The embed carries the
            # specific cause; this flag only stops the "終わりました" summons.
            # #684 joins them: a trust dialog that was answered but left no
            # claude also produced nothing, so it must not be announced as a
            # finished turn either.
            # #701 joins them for the same reason: a turn cut off when the
            # fleet's tmux server was replaced produced nothing either, so
            # announcing "終わりました" would summon the owner to read an answer
            # that does not exist.
            if event.error.startswith(
                (
                    NO_RESPONSE_ERROR_PREFIX,
                    TRUST_STUCK_ERROR_PREFIX,
                    TRUST_START_FAILED_ERROR_PREFIX,
                    FLEET_TMUX_RESTART_ERROR_PREFIX,
                )
            ):
                self._config.outcome.no_response = True
            # #812: a turn refused for want of /login produced nothing either,
            # and — like the limit below — must not be followed by "send it
            # again", so the turn-end line gets its own wording.
            if event.error.startswith(LOGIN_REQUIRED_ERROR_PREFIX):
                self._config.outcome.no_response = True
                self._config.outcome.login_required = True
            # #484: likewise for a model Claude Code will not use — only
            # ``/model`` can fix it.
            refused_model = unavailable_model(event.error)
            if refused_model is not None:
                self._config.outcome.no_response = True
                self._config.outcome.model_unavailable = refused_model
            # #908: Claude Code ended the turn on its own API error. It did
            # produce output before that, so this is not ``no_response`` — but
            # the turn-end line must say what stopped it and how to go on.
            if event.error.startswith(API_ERROR_PREFIX):
                self._config.outcome.api_error = advice_for_error(event.error)
            # #631: a rate-limited turn also produced nothing, but it knows why.
            # ``no_response`` is set too so no caller reads it as a completed
            # turn; ``usage_limit`` is what upgrades the wording from "send it
            # again" to "it resets at ...".
            if event.usage_limit is not None:
                self._config.outcome.no_response = True
                self._config.outcome.usage_limit = event.usage_limit
                # #631 AC8: tell the transcript mirror this thread has just been
                # given the limit in Japanese, so it does not follow up with the
                # same fact folded from Claude's English banner.  Recorded here
                # rather than at detection because what the mirror must not
                # duplicate is a message that actually went out.
                usage_limit_notices.note(self._config.thread.id)
            # #681: the mention rides in the *content* — an embed alone never
            # pushes, which is exactly how a dead turn goes unnoticed.
            err_msg = await self._config.thread.send(
                content=self._failure_mention,
                embed=_make_error_embed(event.error, usage_limit=event.usage_limit),
            )
            if err_msg is not None:
                # #915: the turn-end line must not ping the same person again.
                self._config.outcome.pinged_user_id = self._failure_notify_uid
            # <<REDACTED 2 lines>>
            # #1309: the embed reaches whoever opens the thread; the cure is on
            # the host, so the owner is told by DM — once per outage.
            if self._config.outcome.login_required:
                tmux = getattr(self._config.runner, "_tmux", None)
                session = getattr(tmux, "session_name", None)
                await login_alert.report(
                    self._config.thread, session=session if isinstance(session, str) else None
                )
        # <<REDACTED 3 lines>>
            # #1309: a turn that ran was logged in — the outage, if any, is over.
            await login_alert.recovered()
# <<REDACTED 1 lines>>
        await self._remove_session_start_embed()

        # <<REDACTED 2 lines>>
                await self._config.repo.save(
                    self._config.thread.id,
                    event.session_id,
                    working_dir=self._config.working_dir,
                )
            # <<REDACTED 2 lines>>
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

    async def _remove_session_start_embed(self) -> None:
        """Delete this turn's session-start embed, if it posted one.

        The embed says a session began; once the turn is over it is noise. It
        used to be one of several messages (tool-use, thinking, todo embeds)
        folded into a ``progress.txt`` here — the others were removed in #1316,
        and the banner alone was never worth a file (#542). The transcript
        mirror builds the ``progress.txt`` readers see, from the jsonl.

        Best-effort: a failed delete must never break a completed turn.
        """
        msg, self._session_start_msg = self._session_start_msg, None
        if msg is not None:
            # <<REDACTED 1 lines>>
                await msg.delete()
