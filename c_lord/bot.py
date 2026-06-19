"""Discord Bot class."""

from __future__ import annotations

import contextlib
import logging
import sys
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .claude.types import AskOption, AskQuestion
from .concurrency import SessionRegistry
from .coordination.service import CoordinationService
from .discord_ui.ask_bus import ask_bus
from .discord_ui.ask_view import AskView
from .discord_ui.permission_help import command_error_help

if TYPE_CHECKING:
    from .database.ask_repo import PendingAskRepository
    from .database.lounge_repo import LoungeRepository
    from .discord_ui.thread_dashboard import ThreadStatusDashboard
    from .session_dir import SessionDirManager
    from .tmux import TmuxSessionManager

logger = logging.getLogger(__name__)


class ClaudeDiscordBot(commands.Bot):
    """Discord bot that bridges messages to Claude Code CLI."""

    def __init__(
        self,
        channel_id: int,
        owner_id: int | None = None,
        coordination_channel_id: int | None = None,
        ask_repo: PendingAskRepository | None = None,
        lounge_repo: LoungeRepository | None = None,
        lounge_channel_id: int | None = None,
        session_dir_manager: SessionDirManager | None = None,
        tmux_manager: TmuxSessionManager | None = None,
        expected_bot_user_id: int | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        super().__init__(
            # Accept both "!cmd" and "@bot cmd" so the text-command twins of the
            # slash commands (#209) can be triggered by a mention as well as the
            # "!" prefix.  E2E/webhook tests use "!"; humans can use either.
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
        )
        self.channel_id = channel_id
        self.owner_id = owner_id
        self.session_registry = SessionRegistry()
        # Optional repo for AskUserQuestion restart recovery
        self.ask_repo: PendingAskRepository | None = ask_repo
        # Populated after on_ready when the channel is resolved
        self.thread_dashboard: ThreadStatusDashboard | None = None
        # Coordination channel for multi-session awareness (optional)
        self.coordination = CoordinationService(self, coordination_channel_id)
        # AI Lounge — casual shared space for concurrent Claude sessions (optional)
        self.lounge_repo: LoungeRepository | None = lounge_repo
        self.lounge_channel_id: int | None = lounge_channel_id
        # Session directory lifecycle manager — cleans up clone dirs after runs
        self.session_dir_manager: SessionDirManager | None = session_dir_manager
        # Tmux session lifecycle manager (optional)
        self.tmux_manager: TmuxSessionManager | None = tmux_manager
        # Issue #323: when set, on_ready verifies the logged-in identity and
        # kills the process on mismatch (guards against booting with an
        # inherited production token — the #322 env-contamination class).
        self.expected_bot_user_id: int | None = expected_bot_user_id

    async def process_commands(self, message: discord.Message, /) -> None:
        """Override to allow webhook messages to trigger text commands.

        The default ``commands.Bot.process_commands`` silently drops all
        messages from bot authors (``message.author.bot``).  Since webhook
        messages are marked as bot-authored, this prevents E2E testing via
        webhooks.  We relax the check: webhook messages (identified by
        ``message.webhook_id``) are allowed through.
        """
        if message.author.bot and not message.webhook_id:
            return
        ctx = await self.get_context(message)
        await self.invoke(ctx)

    async def on_error(self, event_method: str, /, *args: object, **kwargs: object) -> None:
        """Handle uncaught exceptions in event handlers.

        Issue #91: if the error is ``RuntimeError("Session is closed")``, the
        aiohttp session died during shutdown and the bot is likely a zombie
        (process alive, Discord connection dead).  Exit with non-zero code so
        that a supervisor (systemd, nohup wrapper) can restart the process.
        """
        import traceback

        exc_info = sys.exc_info()
        exc = exc_info[1]
        if isinstance(exc, RuntimeError) and "Session is closed" in str(exc):
            logger.error(
                "on_error[%s]: aiohttp session closed — bot is in zombie state, exiting",
                event_method,
                exc_info=True,
            )
            sys.exit(1)
        logger.error("Unhandled exception in %s", event_method, exc_info=True)
        traceback.print_exc()

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Log slash command errors AND surface an actionable reply (#444).

        Logging alone (the pre-#444 behavior) left the user staring at Discord's
        generic "アプリが応答しませんでした" with no idea what went wrong.  We now also
        reply ephemerally with a sanitized message (no raw traceback): a
        permission hint for ``discord.Forbidden``, otherwise a generic
        "unexpected error" notice.  The reply itself is best-effort — if it also
        fails (e.g. the interaction expired) we swallow that, since this is the
        last-resort net.
        """
        logger.error("Slash command error: %s", error, exc_info=error)
        message = command_error_help(error)
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def on_command_error(
        self, context: commands.Context, error: commands.CommandError
    ) -> None:
        """Surface text-command (``!cmd``) errors to the user (#444).

        discord.py's default handler only prints a traceback to stderr (invisible
        on bot hosts), so before #444 a failing ``!clord``/``!stop`` looked like a
        silent hang.  ``CommandNotFound`` is ignored (mentions and unrelated ``!``
        text are not errors); everything else is logged and answered with a
        sanitized message via ``ctx.send``.
        """
        if isinstance(error, commands.CommandNotFound):
            return
        logger.error("Text command error: %s", error, exc_info=error)
        message = command_error_help(error)
        with contextlib.suppress(discord.HTTPException):
            await context.send(message)

    def _assert_expected_identity(self) -> None:
        """Fail fast when the logged-in identity is not the expected one.

        Issue #323: with ``load_dotenv(override=False)`` semantics and shell
        env inheritance, a staging launch can silently boot with the
        production token (#322). When ``expected_bot_user_id`` is configured,
        a mismatch means the process is running as the WRONG bot — refuse to
        run rather than whisper one log line and continue.
        """
        if self.expected_bot_user_id is None:
            return
        actual = self.user.id if self.user else None
        if actual != self.expected_bot_user_id:
            logger.critical(
                "IDENTITY MISMATCH: logged in as bot user id %s but "
                "EXPECTED_BOT_USER_ID=%s — refusing to run (wrong token in "
                "env? see #322/#323). Exiting.",
                actual,
                self.expected_bot_user_id,
            )
            sys.exit(1)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "?")
        logger.info("Watching channel ID: %d", self.channel_id)
        # Issue #323: verify identity BEFORE doing anything else.
        self._assert_expected_identity()

        # Re-register persistent AskViews for any questions that were pending
        # when the bot last shut down.  This prevents "Interaction Failed" on
        # old buttons; instead users see a clear "session ended" message.
        await self._restore_pending_ask_views()

        # Initialise the thread-status dashboard once we have a live channel object
        channel = self.get_channel(self.channel_id)
        if isinstance(channel, discord.TextChannel):
            from .discord_ui.thread_dashboard import ThreadStatusDashboard

            self.thread_dashboard = ThreadStatusDashboard(
                channel=channel,
                owner_id=self.owner_id,
            )
            await self.thread_dashboard.initialize()
            logger.info("Thread status dashboard initialised in channel %d", self.channel_id)
        else:
            logger.warning(
                "Could not resolve channel %d to a TextChannel; dashboard disabled",
                self.channel_id,
            )

        # Sync slash commands — guild sync first for instant propagation,
        # then global sync so commands are available outside the guild too.
        try:
            channel = self.get_channel(self.channel_id)
            guild = getattr(channel, "guild", None)
            if guild is not None:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info("Synced slash commands to guild %d (instant)", guild.id)
            synced = await self.tree.sync()
            logger.info("Synced %d slash commands (global)", len(synced))
        except Exception:
            logger.exception("Failed to sync slash commands")

    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        """Detect manual renames in the Discord UI (Issue #95).

        When a user changes the thread name themselves, extract the
        topic body (strip the leading status emoji + trailing ``#N``
        suffix) and persist it as the new stable topic with
        ``auto_topic_locked = 1`` so c-lord stops auto-rewriting it.
        """
        if before.name == after.name:
            return

        session_repo = getattr(self, "session_repo", None)
        if session_repo is None:
            return

        try:
            record = await session_repo.get(after.id)
        except Exception:
            logger.debug("on_thread_update: lookup failed for %d", after.id, exc_info=True)
            return
        if record is None:
            return  # not a c-lord thread

        from .thread_name import parse_topic_from_name

        body = parse_topic_from_name(after.name)
        if not body:
            return
        # Avoid recursive renames: if our own state-sync loop just changed
        # only the emoji or the #N suffix, the body will still match the
        # stored topic — no need to lock or rewrite.
        if record.topic == body and record.auto_topic_locked:
            return

        try:
            await session_repo.set_topic(after.id, body, source="manual")
            await session_repo.lock_topic(after.id)
            logger.info("Manual rename detected for thread %d: topic=%r (locked)", after.id, body)
        except Exception:
            logger.warning(
                "on_thread_update: failed to persist manual rename for %d",
                after.id,
                exc_info=True,
            )

    async def _cleanup_orphaned_session_dirs(self) -> None:
        """Remove leftover clean session directories from previous bot runs.

        Runs in a background task so it does not block on_ready().
        """
        import asyncio

        assert self.session_dir_manager is not None  # caller ensures this
        try:
            results = await asyncio.to_thread(
                self.session_dir_manager.cleanup_orphaned,
                set(),  # no active sessions at startup
            )
            removed = [r for r in results if r.removed]
            skipped = [r for r in results if not r.removed and "does not exist" not in r.reason]
            if removed:
                logger.info(
                    "Startup session dir cleanup: removed %d orphaned dir(s): %s",
                    len(removed),
                    [r.path for r in removed],
                )
            if skipped:
                logger.warning(
                    "Startup session dir cleanup: skipped %d dir(s) (dirty or locked): %s",
                    len(skipped),
                    [(r.path, r.reason) for r in skipped],
                )
        except Exception:
            logger.exception("Error during startup session dir cleanup")

    async def _cleanup_orphaned_tmux_sessions(self) -> None:
        """Kill leftover tmux sessions from previous bot runs.

        Runs in a background task so it does not block on_ready().
        """
        import asyncio

        assert self.tmux_manager is not None  # caller ensures this
        try:
            killed = await asyncio.to_thread(
                self.tmux_manager.cleanup_orphaned,
                set(),  # no active sessions at startup
            )
            if killed:
                logger.info("Startup tmux cleanup: killed %d orphaned session(s)", killed)
        except Exception:
            logger.exception("Error during startup tmux session cleanup")

    async def _restore_pending_ask_views(self) -> None:
        """Re-register persistent AskViews for questions pending before restart.

        For each pending ask found in the DB, we create an AskView and call
        ``bot.add_view()`` so discord.py can route button clicks to it.  When
        clicked, the view tries ``ask_bus.post_answer()`` which returns False
        (no live session), so it sends an ephemeral "session ended" message and
        cleans up the DB entry.
        """
        if self.ask_repo is None:
            return

        records = await self.ask_repo.list_all()
        if not records:
            return

        logger.info(
            "Restoring %d pending AskUserQuestion view(s) from previous run",
            len(records),
        )
        for record in records:
            questions_raw = record.questions()
            for q_idx in range(record.question_idx, len(questions_raw)):
                q_raw = questions_raw[q_idx]
                question = AskQuestion(
                    question=q_raw.get("question", ""),
                    header=q_raw.get("header") or "",
                    multi_select=q_raw.get("multi_select", False),
                    options=[
                        AskOption(
                            label=o.get("label", ""),
                            description=o.get("description") or "",
                        )
                        for o in q_raw.get("options", [])
                    ],
                )
                view = AskView(
                    question,
                    thread_id=record.thread_id,
                    q_idx=q_idx,
                    bus=ask_bus,
                    ask_repo=self.ask_repo,
                )
                self.add_view(view)
                logger.debug(
                    "Restored AskView for thread %d q_idx=%d",
                    record.thread_id,
                    q_idx,
                )
