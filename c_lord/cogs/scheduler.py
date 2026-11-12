# <<REDACTED 9 lines>>
  c-lord only manages scheduling.
# <<REDACTED 13 lines>>
from ..discord_ui.thread_dashboard import board_turn, dashboard_of
from ..model_scopes import begin_turn_for, effort_for_launch
from ..notify_policy import owner_notify_id
from ..thread_settings import resolve_auto_archive_duration
from ..turn_events import StartedBy
from ..utils.logger import log_ctx
# <<REDACTED 4 lines>>
    from ..claude.config import ClaudeConfig
    # <<REDACTED 13 lines>>
        runner: ClaudeConfig with CLI settings.
        # <<REDACTED 6 lines>>
        runner: ClaudeConfig,
        # <<REDACTED 26 lines>>
        due_ids = [t["id"] for t in due]
        logger.info("SchedulerCog: %d task(s) due (ids=%s)", len(due), due_ids)
        # <<REDACTED 3 lines>>
                logger.debug("%s still running — skipping", log_ctx(task_id=task_id))
                # <<REDACTED 8 lines>>
                name=f"clord-scheduler-{task_id}",
            # <<REDACTED 6 lines>>
    async def _resolve_tmux_manager(self, channel_id: int, *, thread_id: int | None):
        """Resolve a TmuxSessionManager for the given channel via ChannelRepoCog."""
        from .channel_repo import ChannelRepoCog

        channel_cog = self.bot.get_cog("ChannelRepoCog")
        if channel_cog is not None and isinstance(channel_cog, ChannelRepoCog):
            # Scheduled tasks are bound to a channel, never a thread (#600 audit).
            return await channel_cog.resolve_tmux_manager(channel_id, thread_id=None)
        return None

    async def _start_transcript_mirror(
        self, task_id: int, thread_id: int, working_dir: str | None
    ) -> None:
        """Tail this run's transcript into its thread (#621, jsonl bridge mode).

        Every run of a task shares one ``working_dir``, hence one Claude Code
        project dir, hence one transcript — so the previous run's mirror has to
        stop or it replays this run into last week's thread.  That used to be
        arranged here from a ``task_id → last thread`` dict, whose comment
        claimed "in-memory only: a restart kills the mirrors too".  It does not:
        ``TranscriptMirrorCog.on_ready`` restores a mirror for every *open*
        session row, and last week's row is open.  A restart therefore erased
        the guard's memory while resurrecting the very mirror it existed to
        stop, and the two ran side by side for a week (#719).

        So the rule now lives where it cannot be forgotten: ``start_for``
        below hands the project dir to this thread and stops whoever held it,
        remembering nothing across calls.
        """
        mirror_cog = getattr(self.bot, "transcript_mirror_cog", None)
        if mirror_cog is None or not working_dir:
            return

        try:
            mirror_cog.start_for(thread_id, working_dir)
        except Exception:
            logger.warning(
                "%s could not start the transcript mirror (thread=%d, dir=%s)",
                log_ctx(task_id=task_id),
                thread_id,
                working_dir,
                exc_info=True,
            )

    # <<REDACTED 2 lines>>
        from ..claude.tmux_runner import TmuxClaudeRunner

        # <<REDACTED 1 lines>>
        ctx = log_ctx(task_id=task_id, channel_id=task["channel_id"])
        # <<REDACTED 1 lines>>
        logger.info("%s _run_task: enter (name=%s)", ctx, task["name"])
        # <<REDACTED 3 lines>>
                logger.warning("%s channel not found (name=%s)", ctx, task["name"])
                # <<REDACTED 2 lines>>
                logger.warning("%s channel is not a TextChannel", ctx)
                # <<REDACTED 2 lines>>
            # A scheduled task targets a channel, never a thread (#600 audit).
            tmux = await self._resolve_tmux_manager(channel.id, thread_id=None)
            if tmux is None:
                logger.warning("%s no tmux manager (name=%s)", ctx, task["name"])
                return

            # <<REDACTED 5 lines>>
            archive_minutes = await resolve_auto_archive_duration(
                getattr(self.bot, "settings_repo", None)
            )
            # <<REDACTED 2 lines>>
                auto_archive_duration=archive_minutes,
            # <<REDACTED 2 lines>>
            working_dir = task.get("working_dir") or self.runner.working_dir

            # #621: create the tmux window BEFORE building the runner.  This
            # call was simply missing, so ``start_claude`` found no window,
            # returned False, and every scheduled run ended two seconds in with
            # a single ❌ embed.  ClaudeChatCog has always done this (see
            # ``claude_chat.py`` → ``tmux_manager.create_session``); the
            # scheduled path just never learned it.
            window = await asyncio.to_thread(tmux.create_session, thread.id, working_dir or ".")
            if not await asyncio.to_thread(tmux.session_exists, thread.id):
                # create_session also returns a name when tmux itself is
                # unavailable, so the name alone proves nothing — ask whether a
                # window is really there before starting Claude at it.
                logger.error(
                    "%s could not create a tmux window for thread %d (name=%s) — aborting the run",
                    ctx,
                    thread.id,
                    task["name"],
                )
                await thread.send(
                    "❌ このスケジュール実行を動かす tmux ウィンドウを作れませんでした。"
                    "ホストで tmux が使えるか、チャンネルが `/clord-init` で repo に"
                    "紐づいているかを確認してください。"
                )
                return
            logger.info("%s tmux window for thread %d: %s", ctx, thread.id, window)

            # #621: in the default jsonl bridge mode nothing else carries
            # Claude's answer back — the thread would show tool embeds and then
            # go quiet.  ClaudeChatCog starts the mirror on every turn; so must
            # this path.
            await self._start_transcript_mirror(task_id, thread.id, working_dir)

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
                timeout_seconds=self.runner.timeout_seconds,
                dangerously_skip_permissions=True,
                effort=effort_for_launch(model, turn.effort if turn else self.runner.effort),
            )
# <<REDACTED 2 lines>>
            run_config = RunConfig(
                # <<REDACTED 1 lines>>
                runner=runner,
                # #687: persist the session row. Without it the thread is
                # UNTRACKED to :mod:`c_lord.session_resume`, so every reply to a
                # scheduled run's report was dropped with 「復元できるワークスペース
                # がありません」 — while its tmux window sat there alive. The
                # thread a scheduled run creates is a place people answer, so it
                # has to be a session like any other. ``working_dir`` goes with
                # it because that is the checkout the run really used, and both
                # the reply path (#687) and the mirror restore (#71) read it back.
                repo=getattr(self.bot, "session_repo", None),
                # #739: a scheduled run posts the same buttons as any other, so
                # it needs the same allowlist to gate them.
                authorizer=getattr(self.bot, "authorizer", None),
                working_dir=working_dir,
                # <<REDACTED 3 lines>>
                # #480: scheduled turns have no human poster — fall back to
                # the bot owner so a question-mode pause still pings someone
                # (#525: unless this deployment turned that fallback off).
                notify_user_id=owner_notify_id(self.bot, kind="blocked"),
                # #681: same for a turn that never ran. Nobody is watching a
                # scheduled thread, so silence here is indistinguishable from
                # success.
                failure_notify_id=owner_notify_id(self.bot, kind="failure"),
                # #917: the turn-end event names the task that started the run.
                started_by=StartedBy("scheduler", str(task["id"]), str(task["name"])),
            # <<REDACTED 1 lines>>
            # #754: a scheduled run is a live session like any other, so it is
            # on 📊 Session Status while it runs. It used to be invisible there.
            # Labelled by its (already public) name — the prompt stays private.
            async with board_turn(dashboard_of(self.bot), thread.id, f"[Scheduled] {task['name']}"):
                await run_claude_with_config(run_config)

            # #621: nobody is watching a scheduled thread, so a run that only
            # failed into a red embed used to leave the log looking like a
            # clean exit.  Say it out loud.
            if run_config.outcome.error:
                logger.error(
                    "%s task run failed (name=%s): %s",
                    ctx,
                    task["name"],
                    run_config.outcome.error,
                # <<REDACTED 3 lines>>
            logger.exception("%s task failed (name=%s)", ctx, task["name"])
        # <<REDACTED 2 lines>>
            logger.info("%s _run_task: exit", ctx)
