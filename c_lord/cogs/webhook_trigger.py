# <<REDACTED 15 lines>>
import contextlib
# <<REDACTED 7 lines>>
from .. import turn_events
# <<REDACTED 3 lines>>
from ..discord_failures import DISCORD_CALL_FAILURES
from ..discord_ui.thread_dashboard import board_turn, dashboard_of
from ..model_scopes import begin_turn_for, effort_for_launch
from ..notify_policy import owner_notify_id
from ..thread_settings import resolve_auto_archive_duration
from ..utils.logger import log_ctx
from ..voice_webhooks import is_voice_message
# <<REDACTED 2 lines>>
    from ..claude.config import ClaudeConfig
# <<REDACTED 10 lines>>
        working_dir: Override the default working directory.
        timeout: Override the default timeout in seconds.
        allowed_tools: Override the default allowed tools list.
        # <<REDACTED 18 lines>>
        runner: ClaudeConfig with CLI settings.
        # <<REDACTED 10 lines>>
        runner: ClaudeConfig,
        # <<REDACTED 19 lines>>
    def _match(self, message: discord.Message) -> tuple[str, WebhookTrigger] | None:
        """The trigger *message* fires, or ``None`` when it is not one of ours."""
        if not message.webhook_id or is_voice_message(message):
            return None  # #971: c-lord speaking for a sub-agent triggers nothing
# <<REDACTED 5 lines>>
            return None
# <<REDACTED 2 lines>>
            return None
# <<REDACTED 4 lines>>
                return prefix, trigger
        return None
# <<REDACTED 1 lines>>
    def claims_message(self, message: discord.Message) -> bool:
        """Whether *message* is a trigger this cog acts on — #862.

        ``ClaudeChatCog`` asks before turning a webhook message in a thread with
        no session into a new conversation, so a trigger is never also run as a
        request to Claude.
        """
        return self._match(message) is not None
# <<REDACTED 4 lines>>
        matched = self._match(message)
        if matched is None:
            # <<REDACTED 1 lines>>
        matched_prefix, matched_trigger = matched
# <<REDACTED 2 lines>>
            "%s Webhook trigger matched: prefix=%r, webhook_id=%d",
            log_ctx(channel_id=message.channel.id),
            # <<REDACTED 12 lines>>
    async def _resolve_tmux_manager(self, channel_id: int, *, thread_id: int | None):
        """Resolve a TmuxSessionManager for the given channel via ChannelRepoCog.

        #427: pass ``thread_id`` whenever a thread is in scope so a thread bound
        to its own repo resolves to that repo's session.
        """
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            return await channel_cog.resolve_tmux_manager(channel_id, thread_id=thread_id)
        return None

    def _start_transcript_mirror(self, thread_id: int, working_dir: str | None, ctx: str) -> None:
        """Tail this run's transcript into its thread (#629, as #621 did for the scheduler)."""
        mirror_cog = getattr(self.bot, "transcript_mirror_cog", None)
        if mirror_cog is None or not working_dir:
            return
        try:
            mirror_cog.start_for(thread_id, working_dir)
        except Exception:
            logger.warning(
                "%s could not start the transcript mirror (dir=%s)",
                ctx,
                working_dir,
                exc_info=True,
            )

    async def _report_missing_window(
        self, message: discord.Message, thread: discord.Thread
    ) -> None:
        """Tell the thread (and the owner) that the run never started (#629).

        A webhook thread has no human in it, so the text alone reaches nobody:
        the owner is mentioned the way #681 pings them for a turn that died at
        startup — this path stops before that ping could fire.
        """
        owner = owner_notify_id(self.bot, kind="failure")
        mention = f"<@{owner}> " if owner is not None else ""
        with contextlib.suppress(*DISCORD_CALL_FAILURES):
            await thread.send(
                f"{mention}❌ この webhook トリガーを動かす tmux ウィンドウを作れませんでした。"
                "ホストで tmux が使えるか、チャンネルが `/clord-init` で repo に"
                "紐づいているかを確認してください。",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=owner is not None
                ),
            )
        with contextlib.suppress(discord.HTTPException):
            # <<REDACTED 1 lines>>

    # <<REDACTED 7 lines>>
        from ..claude.tmux_runner import TmuxClaudeRunner

        archive_minutes = await resolve_auto_archive_duration(
            getattr(self.bot, "settings_repo", None)
        )
        thread = await message.create_thread(
            name=prefix[:100], auto_archive_duration=archive_minutes
        )
# <<REDACTED 1 lines>>
        tmux = await self._resolve_tmux_manager(message.channel.id, thread_id=thread.id)
        if tmux is None:
            await thread.send("⚠️ tmux is not configured for this channel.")
            return

        ctx = log_ctx(thread_id=thread.id, channel_id=message.channel.id)
        working_dir = trigger.working_dir or self.runner.working_dir

        # #629: create the tmux window BEFORE building the runner.  Nothing
        # downstream creates it (``run_claude_with_config`` / ``tmux_runner``
        # only type into an existing one), so without this every trigger ended
        # in a thread holding a single ❌ and no Claude — #621 in the webhook
        # path.  Same two checks as ``SchedulerCog._run_task``: create, then
        # ask whether a window is really there, because ``create_session`` also
        # returns a name when tmux itself is unavailable.
        window = await asyncio.to_thread(tmux.create_session, thread.id, working_dir or ".")
        if not await asyncio.to_thread(tmux.session_exists, thread.id):
            logger.error(
                "%s could not create a tmux window for webhook trigger %r — aborting the run",
                ctx,
                prefix,
            )
            await self._report_missing_window(message, thread)
            return
        logger.info("%s tmux window for webhook trigger %r: %s", ctx, prefix, window)

        # #629: the jsonl mirror is the only delivery path (#712).  Without it
        # Claude would run and answer into a transcript nobody reads back.
        self._start_transcript_mirror(thread.id, working_dir, ctx)

        # #918: the same layered model/effort a user turn would get.
        turn = await begin_turn_for(
            self.bot,
            getattr(self, "settings_repo", None) or getattr(self.bot, "settings_repo", None),
            self.runner,
            thread.id,
            getattr(thread, "parent_id", None),
        )
        model = (turn.model if turn else None) or self.runner.model
        runner = TmuxClaudeRunner(
            tmux_manager=tmux,
            thread_id=thread.id,
            model=model,
            working_dir=working_dir,
            timeout_seconds=trigger.timeout,
            dangerously_skip_permissions=trigger.dangerously_skip_permissions,
            effort=effort_for_launch(model, turn.effort if turn else self.runner.effort),
        )
# <<REDACTED 3 lines>>
            # #754: a CI-triggered run belongs on 📊 Session Status too.
            # Labelled by the public prefix — the prompt is server-side config.
            async with board_turn(dashboard_of(self.bot), thread.id, prefix):
                # <<REDACTED 9 lines>>
                        # #739: a webhook turn's buttons are pressed by humans, so
                        # they are gated by the human allowlist like any other run.
                        authorizer=getattr(self.bot, "authorizer", None),
                        # #480: no human poster on a webhook (CI/CD) turn — fall back
                        # to the bot owner so a question-mode pause still pings someone
                        # (#525: unless this deployment turned that fallback off).
                        notify_user_id=owner_notify_id(self.bot, kind="blocked"),
                        # #681: a webhook turn that dies at startup is the case
                        # nobody notices — there is no human in the thread to see
                        # the red embed. This is the ping that makes it visible.
                        failure_notify_id=owner_notify_id(self.bot, kind="failure"),
                        # #917: the turn-end event names the webhook that fired it.
                        started_by=turn_events.started_by_from_message(
                            message, requester=None, bot_is_author=False
                        ),
                    # <<REDACTED 9 lines>>
