"""SchedulerCog — SQLite-backed periodic Claude Code task executor.

Design:
- Tasks are stored in ``scheduled_tasks`` DB table and registered via REST API
  (Claude Code calls POST /api/tasks from within a chat session).
- A single 30-second master loop checks for due tasks and spawns them.
- ``discord.ext.tasks`` is used only for the master loop — individual tasks
  are not @tasks.loop decorated (they are runtime-dynamic).
- Claude handles all domain logic (what to check, how to deduplicate).
  c-lord only manages scheduling.

See: Issue #90, CLAUDE.md §Key Design Decisions #7-9.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from ..notify_policy import owner_notify_id
from ..thread_settings import resolve_auto_archive_duration
from ..utils.logger import log_ctx
from ._run_helper import run_claude_with_config
from .run_config import RunConfig

if TYPE_CHECKING:
    from ..claude.config import ClaudeConfig
    from ..database.task_repo import TaskRepository

logger = logging.getLogger(__name__)

# How often the master loop wakes up to check for due tasks.
MASTER_LOOP_INTERVAL_SECONDS = 30


class SchedulerCog(commands.Cog):
    """Cog that periodically runs Claude Code tasks stored in SQLite.

    Args:
        bot: The Discord bot instance.
        runner: ClaudeConfig with CLI settings.
        repo: TaskRepository for reading/updating scheduled tasks.
    """

    def __init__(
        self,
        bot: commands.Bot,
        runner: ClaudeConfig,
        *,
        repo: TaskRepository,
    ) -> None:
        self.bot = bot
        self.runner = runner
        self.repo = repo
        # Track in-flight tasks to avoid double-running the same task_id.
        self._running: set[int] = set()
        # task_id → the thread of its most recent run (#621).  Every run of a
        # task shares one working_dir, hence one transcript; the previous run's
        # mirror has to be stopped or it replays this run into last week's
        # thread.  In-memory only: a restart kills the mirrors too.
        self._last_thread: dict[int, int] = {}

    async def cog_load(self) -> None:
        """Start the master loop when the Cog is loaded."""
        self._master_loop.start()
        logger.info("SchedulerCog loaded — master loop started")

    def cog_unload(self) -> None:
        """Cancel the master loop when the Cog is unloaded."""
        self._master_loop.cancel()
        logger.info("SchedulerCog unloaded — master loop stopped")

    @tasks.loop(seconds=MASTER_LOOP_INTERVAL_SECONDS)
    async def _master_loop(self) -> None:
        """Wake up every 30 s, find due tasks, and spawn them concurrently."""
        due = await self.repo.get_due()
        if not due:
            return

        due_ids = [t["id"] for t in due]
        logger.info("SchedulerCog: %d task(s) due (ids=%s)", len(due), due_ids)
        for task in due:
            task_id: int = task["id"]
            if task_id in self._running:
                logger.debug("%s still running — skipping", log_ctx(task_id=task_id))
                continue

            # Advance next_run_at *before* spawning to prevent duplicate runs
            # if the loop fires again before the task finishes.
            await self.repo.update_next_run(task_id, interval_seconds=task["interval_seconds"])

            asyncio.create_task(
                self._run_task(task),
                name=f"clord-scheduler-{task_id}",
            )

    @_master_loop.before_loop
    async def _before_master_loop(self) -> None:
        await self.bot.wait_until_ready()

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

        Also stops the mirror left behind by the previous run of the same task:
        the two runs share a ``working_dir``, so they share the Claude Code
        project dir the mirror tails, and a live mirror on last week's thread
        would post this week's whole turn into it a second time.
        """
        mirror_cog = getattr(self.bot, "transcript_mirror_cog", None)
        if mirror_cog is None or not working_dir:
            return

        previous = self._last_thread.get(task_id)
        if previous is not None and previous != thread_id:
            try:
                await mirror_cog.stop_for(previous)
            except Exception:
                logger.warning(
                    "%s could not stop the previous run's transcript mirror (thread=%d)",
                    log_ctx(task_id=task_id),
                    previous,
                    exc_info=True,
                )
        self._last_thread[task_id] = thread_id

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

    async def _run_task(self, task: dict) -> None:
        """Execute a single scheduled task in a Discord thread."""
        from ..claude.tmux_runner import TmuxClaudeRunner

        task_id: int = task["id"]
        ctx = log_ctx(task_id=task_id, channel_id=task["channel_id"])
        self._running.add(task_id)
        logger.info("%s _run_task: enter (name=%s)", ctx, task["name"])
        try:
            channel = self.bot.get_channel(task["channel_id"])
            if channel is None:
                logger.warning("%s channel not found (name=%s)", ctx, task["name"])
                return
            if not isinstance(channel, discord.TextChannel):
                logger.warning("%s channel is not a TextChannel", ctx)
                return

            # A scheduled task targets a channel, never a thread (#600 audit).
            tmux = await self._resolve_tmux_manager(channel.id, thread_id=None)
            if tmux is None:
                logger.warning("%s no tmux manager (name=%s)", ctx, task["name"])
                return

            # Post a starter message first so the thread appears in the channel
            # timeline and shows up in the left sidebar under the parent channel.
            # channel.create_thread() without a message only appears in the
            # Threads panel (🧵), not in the channel list.
            starter = await channel.send(f"🔄 **[Scheduled]** `{task['name']}`")
            archive_minutes = await resolve_auto_archive_duration(
                getattr(self.bot, "settings_repo", None)
            )
            thread = await starter.create_thread(
                name=f"[Scheduled] {task['name']}",
                auto_archive_duration=archive_minutes,
            )

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

            runner = TmuxClaudeRunner(
                tmux_manager=tmux,
                thread_id=thread.id,
                model=self.runner.model,
                working_dir=working_dir,
                timeout_seconds=self.runner.timeout_seconds,
                dangerously_skip_permissions=True,
                effort=self.runner.effort,
            )

            registry = getattr(self.bot, "session_registry", None)
            run_config = RunConfig(
                thread=thread,
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
                working_dir=working_dir,
                prompt=task["prompt"],
                session_id=None,
                registry=registry,
                # #480: scheduled turns have no human poster — fall back to
                # the bot owner so a question-mode pause still pings someone
                # (#525: unless this deployment turned that fallback off).
                notify_user_id=owner_notify_id(self.bot, kind="blocked"),
            )
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
                )

        except Exception:
            logger.exception("%s task failed (name=%s)", ctx, task["name"])
        finally:
            self._running.discard(task_id)
            logger.info("%s _run_task: exit", ctx)
