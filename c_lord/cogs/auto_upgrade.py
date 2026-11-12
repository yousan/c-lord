"""Upgrade the installed package from Discord, then restart the bot.

Two ways in, one pipeline:

* a webhook posts the configured trigger line (a release workflow, typically);
* an authorised user runs ``/upgrade`` (off unless the config turns it on).

Either way a progress thread is opened and the steps run in it: the upgrade
command, the sync command and — when one is configured — the restart command,
after waiting for the turns that are running to finish.

Every command line comes from :class:`UpgradeConfig`. Nothing typed in Discord
is ever part of one, and nothing goes through a shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..command_gate import NOT_AUTHORIZED
from ..discord_ui.authorization import AuthorizedViewMixin, Authorizer, get_default_authorizer
from ..protocols import DrainAware
from ..thread_settings import resolve_auto_archive_duration
from ..voice_webhooks import is_voice_message

logger = logging.getLogger(__name__)

#: Seconds one upgrade / sync step may take unless the config says otherwise.
_STEP_TIMEOUT = 120

_OK = "✅"
_FAILED = "❌"


def _admits(allowlist: set[int] | None, candidate: int) -> bool:
    """Whether *candidate* passes *allowlist*; no list at all admits everything."""
    return allowlist is None or candidate in allowlist


class UpgradeApprovalView(AuthorizedViewMixin, discord.ui.View):
    """A single "approve" button, posted where people will actually see it.

    The button and the ✅ reaction in the upgrade thread are two doors to the
    same room: pressing it sets the event the cog is waiting on. Who may press
    it is :class:`AuthorizedViewMixin`'s business.
    """

    def __init__(
        self,
        *,
        approved_event: asyncio.Event,
        bot_id: int | None = None,
        label: str = "✅ Approve",
        authorizer: Authorizer | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self._event = approved_event
        self._bot_id = bot_id
        self._authorizer = authorizer
        self.approve.label = label

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._is_own_click(interaction):
            await interaction.response.defer()
            return
        self._seal(button)
        await interaction.response.edit_message(view=self)
        self._event.set()
        self.stop()

    def _is_own_click(self, interaction: discord.Interaction) -> bool:
        """Whether the bot itself pressed the button — which decides nothing."""
        return self._bot_id is not None and interaction.user.id == self._bot_id

    @staticmethod
    def _seal(button: discord.ui.Button) -> None:
        """Make *button* show that the answer was given, and take no more presses."""
        button.label = "✅ Approved"
        button.disabled = True


@dataclass(frozen=True)
class UpgradeConfig:
    """What to upgrade, how, and who may ask for it.

    ``upgrade_command`` / ``sync_command`` default to ``uv lock
    --upgrade-package <package_name>`` and ``uv sync``, both run in
    ``working_dir``. Without a ``restart_command`` the bot is not restarted.
    ``allowed_webhook_ids`` / ``channel_ids`` narrow which webhook messages
    count as the trigger; ``None`` means no restriction.
    """

    package_name: str
    #: The exact webhook message that starts an upgrade; also the thread's name.
    trigger_prefix: str = "🔄 upgrade"
    working_dir: str = "."
    upgrade_command: list[str] | None = None
    sync_command: list[str] | None = None
    restart_command: list[str] | None = None
    allowed_webhook_ids: set[int] | None = None
    channel_ids: set[int] | None = None
    step_timeout: int = _STEP_TIMEOUT
    #: Wait for a person to approve before restarting.
    restart_approval: bool = False
    #: Wait for a person to approve before running anything at all.
    upgrade_approval: bool = False
    #: Whether ``/upgrade`` does anything.
    slash_command_enabled: bool = False


class AutoUpgradeCog(commands.Cog):
    """Runs the upgrade pipeline when the trigger arrives.

    Example::

        config = UpgradeConfig(
            package_name="c-lord",
            restart_command=["sudo", "systemctl", "restart", "my-bot.service"],
        )
        await bot.add_cog(AutoUpgradeCog(bot, config))

    Before restarting, the cog waits until nothing is running. By default it
    asks every other cog that satisfies :class:`~c_lord.protocols.DrainAware`;
    pass ``drain_check`` to decide some other way. The wait gives up after
    ``drain_timeout`` seconds and restarts regardless.
    """

    def __init__(
        self,
        bot: commands.Bot,
        config: UpgradeConfig,
        drain_check: Callable[[], bool] | None = None,
        drain_timeout: int = 300,
        drain_poll_interval: int = 10,
    ) -> None:
        self.bot = bot
        self.config = config
        self._drain_check = drain_check
        self._drain_timeout = drain_timeout
        self._drain_poll_interval = drain_poll_interval
        # One upgrade at a time, whichever way it was asked for.
        self._lock = asyncio.Lock()

    # -- triggers ----------------------------------------------------------

    def claims_message(self, message: discord.Message) -> bool:
        """Whether *message* is this cog's upgrade trigger — #862.

        ``ClaudeChatCog`` asks before turning a webhook message in a thread with
        no session into a new conversation, so the trigger never also reaches
        Claude as a request.
        """
        if not message.webhook_id or is_voice_message(message):
            return False  # #971: c-lord speaking for a sub-agent triggers nothing
        if not _admits(self.config.allowed_webhook_ids, message.webhook_id):
            return False
        if not _admits(self.config.channel_ids, message.channel.id):
            return False
        return message.content.strip() == self.config.trigger_prefix

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Start an upgrade when the trigger line arrives from an allowed webhook."""
        if not self.claims_message(message):
            return
        logger.info("Auto-upgrade trigger received: %r", self.config.trigger_prefix)
        if self._in_progress:
            await message.reply("⏳ Upgrade is already running. Skipping.")
            return
        async with self._lock:
            await self._run_upgrade(message)

    @property
    def _in_progress(self) -> bool:
        """Whether an upgrade, however it was asked for, is running right now."""
        return self._lock.locked()

    def _is_allowed(self, user: discord.Member | discord.User) -> bool:
        """Whether *user* is on the process's allowlist (#781).

        The cog holds no :class:`Authorizer` of its own — it is the one
        ``ClaudeChatCog`` published on the bot, as for the approval buttons
        below. With none published there is nobody to ask, so the answer is no.
        """
        authorizer = getattr(self.bot, "authorizer", None) or get_default_authorizer()
        return authorizer is not None and authorizer.is_allowed(user)

    @app_commands.command(name="upgrade", description="Manually trigger a package upgrade")
    async def upgrade_command(self, interaction: discord.Interaction) -> None:
        """Run the upgrade pipeline on request from an authorised user.

        "Authorised" is the process's allowlist (#781) — it used to be anyone
        who could see the command. Checked before anything else, including
        whether the command is enabled at all.
        """
        if not self._is_allowed(interaction.user):
            logger.info(
                "/upgrade rejected: user %s is not authorized (#781)",
                getattr(interaction.user, "id", "?"),
            )
            await interaction.response.send_message(NOT_AUTHORIZED, ephemeral=True)
            return

        if not self.config.slash_command_enabled:
            await interaction.response.send_message(
                "⚠️ Slash command upgrades are not enabled for this bot.", ephemeral=True
            )
            return

        if self._in_progress:
            await interaction.response.send_message(
                "⏳ Upgrade is already running. Please wait.", ephemeral=True
            )
            return

        home = self._thread_home(interaction.channel)
        if home is None:
            await interaction.response.send_message(
                "⚠️ This command can only be used in a text channel or thread.", ephemeral=True
            )
            return

        # Acknowledge before taking the lock: the pipeline outlives Discord's
        # three seconds by a wide margin.
        await interaction.response.defer()
        archive_minutes = await resolve_auto_archive_duration(
            getattr(self.bot, "settings_repo", None)
        )
        async with self._lock:
            thread = await home.create_thread(
                name=self.config.trigger_prefix[:100],
                auto_archive_duration=archive_minutes,
            )
            await self._run_pipeline(thread, status_target=None)

    @staticmethod
    def _thread_home(channel: object) -> discord.TextChannel | None:
        """The text channel a progress thread can be opened in, seen from *channel*."""
        if isinstance(channel, discord.TextChannel):
            return channel
        if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.TextChannel):
            return channel.parent
        return None

    async def _run_upgrade(self, trigger_message: discord.Message) -> None:
        """Open the progress thread on the trigger message and run the pipeline in it."""
        archive_minutes = await resolve_auto_archive_duration(
            getattr(self.bot, "settings_repo", None)
        )
        thread = await trigger_message.create_thread(
            name=self.config.trigger_prefix[:100], auto_archive_duration=archive_minutes
        )
        await self._run_pipeline(thread, status_target=trigger_message)

    # -- the pipeline ------------------------------------------------------

    async def _run_pipeline(
        self, thread: discord.Thread, status_target: discord.Message | None
    ) -> None:
        """Upgrade, sync and (if configured) restart, reporting into *thread*.

        *status_target* is the message that gets the ✅ / ❌ verdict; the slash
        command has none and passes ``None``. Nothing raised in here leaves:
        a failure is said in the thread and marked on *status_target*.
        """
        try:
            await self._run_stages(thread, status_target)
        except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 — distinct types on 3.10
            await thread.send("❌ Step timed out.")
            await self._mark(status_target, _FAILED)
        except Exception:
            logger.exception("Auto-upgrade error")
            await thread.send("❌ Upgrade failed with an unexpected error.")
            await self._mark(status_target, _FAILED)

    async def _run_stages(
        self, thread: discord.Thread, status_target: discord.Message | None
    ) -> None:
        config = self.config
        if config.upgrade_approval:
            await self._wait_for_approval(
                status_target,
                thread,
                prompt=(
                    f"📦 New release of **{config.package_name}** detected. "
                    "React ✅ on this message to start the upgrade."
                ),
            )

        steps = (
            (
                "upgrade",
                config.upgrade_command or ["uv", "lock", "--upgrade-package", config.package_name],
            ),
            ("sync", config.sync_command or ["uv", "sync"]),
        )
        for step_name, command in steps:
            if not await self._run_step(thread, step_name, command):
                await self._mark(status_target, _FAILED)
                return

        if not config.restart_command:
            await self._mark(status_target, _OK)
            await thread.send("✅ Upgrade complete (no restart configured).")
            return

        if config.restart_approval:
            await self._wait_for_approval(status_target, thread)
        # Taken before draining: by the time the drain is over, the turns that
        # deserve a restart notice are no longer listed as running.
        interrupted = self._collect_active_thread_ids()
        await self._drain(thread)
        await self._mark_sessions_for_resume(interrupted, thread)
        await self._restart(status_target, thread)

    @staticmethod
    async def _mark(status_target: discord.Message | None, emoji: str) -> None:
        if status_target is not None:
            await status_target.add_reaction(emoji)

    async def _run_step(self, thread: discord.Thread, step_name: str, command: list[str]) -> bool:
        """Run one configured command, echoing it and its output into *thread*.

        Returns whether it exited 0. A step that outlasts ``step_timeout``
        raises the timeout to the caller; the process is not stopped.
        """
        await thread.send(f"⚙️ `{' '.join(command)}`")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.config.working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        captured, _ = await asyncio.wait_for(
            process.communicate(), timeout=self.config.step_timeout
        )
        output = (captured or b"").decode("utf-8", errors="replace").strip()
        if output:
            await thread.send(f"```\n{output[:1800]}\n```")

        if process.returncode != 0:
            await thread.send(f"❌ `{step_name}` failed (exit code {process.returncode}).")
            return False
        return True

    # -- restart: who is busy, waiting for them, telling them --------------

    def _other_cogs(self) -> list[object]:
        return [cog for cog in self.bot.cogs.values() if cog is not self]

    def _collect_active_thread_ids(self) -> frozenset[int]:
        """The threads with a turn running, read off whichever cogs track runners.

        Duck-typed on an ``_active_runners`` dict so that no cog class has to
        be imported here.
        """
        busy: set[int] = set()
        for cog in self._other_cogs():
            runners = getattr(cog, "_active_runners", None)
            if isinstance(runners, dict):
                busy.update(runners)
        return frozenset(busy)

    async def _mark_sessions_for_resume(
        self, thread_ids: frozenset[int], status_thread: discord.Thread
    ) -> None:
        """Record *thread_ids* in the pending-resumes table for a restart notice.

        Called just before the restart command fires, so that the threads which
        were active at upgrade time get a quiet restart notice on the next bot
        start-up.
        #406: we do **not** re-prompt Claude on restart — the ``claude`` process
        survives the upgrade restart and the observers self-restore, so
        ``on_ready`` only posts a quiet notice (it never re-runs Claude).

        ``session_id`` is auto-resolved from ``bot.session_repo`` when available
        (kept for diagnostics / future use).
        """
        resume_repo = getattr(self.bot, "resume_repo", None)
        if not thread_ids or resume_repo is None:
            return
        session_repo = getattr(self.bot, "session_repo", None)

        marked = 0
        for tid in thread_ids:
            try:
                record = await session_repo.get(tid) if session_repo is not None else None
                session_id = record.session_id if record is not None else None
                # resume_prompt is intentionally None (#406): on_ready posts a
                # quiet restart notice, it never re-runs Claude with a prompt.
                await resume_repo.mark(
                    tid,
                    session_id=session_id,
                    reason="bot_upgrade",
                    resume_prompt=None,
                )
                marked += 1
            except Exception:
                logger.warning("Failed to record thread %d for restart notice", tid, exc_info=True)

        if marked:
            await status_thread.send(
                f"📌 {marked} active session(s) noted — "
                "a quiet restart notice will be posted (no auto-resume)."
            )

    def _auto_drain_check(self) -> bool:
        """Whether every other :class:`DrainAware` cog reports nothing running."""
        return all(
            cog.active_count == 0 for cog in self._other_cogs() if isinstance(cog, DrainAware)
        )

    async def _drain(self, thread: discord.Thread) -> None:
        """Hold the restart until nothing is running, or until ``drain_timeout``.

        Running out of time is announced and the restart goes ahead.
        """
        is_idle = self._auto_drain_check if self._drain_check is None else self._drain_check
        if is_idle():
            return

        await thread.send(
            "⏳ Upgrade ready — waiting for active sessions to finish "
            f"(max {self._drain_timeout}s)..."
        )
        async for waited in self._drain_polls():
            if is_idle():
                await thread.send(f"✅ Sessions finished ({waited}s). Restarting now...")
                return
        await thread.send(f"⚠️ Drain timeout ({self._drain_timeout}s elapsed) — restarting anyway.")

    async def _drain_polls(self) -> AsyncIterator[int]:
        """Sleep one poll interval at a time, yielding the seconds waited so far.

        The count is the sum of the intervals slept, not a clock reading, and
        the polls stop once it reaches ``drain_timeout``.
        """
        waited = 0
        while waited < self._drain_timeout:
            await asyncio.sleep(self._drain_poll_interval)
            waited += self._drain_poll_interval
            yield waited

    # -- approval ----------------------------------------------------------

    async def _wait_for_approval(
        self,
        status_target: discord.Message | None,
        thread: discord.Thread,
        *,
        prompt: str | None = None,
    ) -> None:
        """Ask for a go-ahead and return once someone gives it.

        The question goes into *thread* with a ✅ to react to, and — because a
        thread is easy to miss — into its parent channel with a button. Either
        one approves. There is no deadline; a reminder is posted every
        ``drain_timeout`` seconds for as long as nobody answers.

        *status_target* is accepted for the callers' sake and not used: the
        question lives in the thread.
        """
        question = prompt or "📦 Update installed. React ✅ on this message to restart."
        approved = asyncio.Event()

        # Both posts are out before anything waits, so the button already
        # exists the first time this coroutine can be interrupted.
        ask_msg = await self._ask_in_thread(thread, question)
        button_msg = await self._offer_button(thread, question, approved)

        # Whichever door is used first wins; the other wait is torn down so a
        # button press does not sit behind a reaction wait that may never end.
        await self._first_of(self._reaction_given(thread, ask_msg, approved), approved.wait())

        if button_msg is not None:
            with contextlib.suppress(discord.NotFound):
                await button_msg.delete()
        await thread.send("👍 Restart approved!")

    @property
    def _own_user_id(self) -> int | None:
        return self.bot.user.id if self.bot.user else None

    @staticmethod
    async def _ask_in_thread(thread: discord.Thread, question: str) -> discord.Message:
        """Post *question* with the ✅ people are asked to press already on it."""
        ask_msg = await thread.send(question)
        await ask_msg.add_reaction(_OK)
        return ask_msg

    async def _offer_button(
        self, thread: discord.Thread, question: str, approved: asyncio.Event
    ) -> discord.Message | None:
        """Put an approve button in the thread's parent channel, if that can be done.

        Returns the message carrying it, or ``None`` when there is no parent or
        the post failed — the reaction in the thread still works then.
        """
        parent = getattr(thread, "parent", None)
        bot_id = self._own_user_id
        if parent is not None:
            view = UpgradeApprovalView(
                approved_event=approved,
                bot_id=bot_id,
                authorizer=getattr(self.bot, "authorizer", None),
            )
            try:
                return await parent.send(
                    f"🔔 **Approval needed** — {question}\n"
                    "(React ✅ in the upgrade thread above, or click here ↓)",
                    view=view,
                )
            except Exception:
                logger.debug("Could not post approval button to parent channel", exc_info=True)
        return None

    async def _reaction_given(
        self, thread: discord.Thread, ask_msg: discord.Message, approved: asyncio.Event
    ) -> None:
        """Return when someone other than the bot reacts ✅ to *ask_msg*, setting *approved*.

        Each ``drain_timeout`` seconds without one, a reminder goes into *thread*.
        """
        bot_id = self._own_user_id

        def approves(payload: discord.RawReactionActionEvent) -> bool:
            if payload.message_id != ask_msg.id or str(payload.emoji) != _OK:
                return False
            return bot_id is None or payload.user_id != bot_id

        while not approved.is_set():
            try:
                payload = await self.bot.wait_for(
                    "raw_reaction_add", check=approves, timeout=float(self._drain_timeout)
                )
            except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 — distinct on 3.10
                if not approved.is_set():
                    await thread.send(
                        "⏳ Still waiting for restart approval... "
                        "React ✅ above or click the button in the channel."
                    )
            else:
                logger.info("Restart approved by user %s", payload.user_id)
                approved.set()

    @staticmethod
    async def _first_of(*contenders: Coroutine[Any, Any, object]) -> None:
        """Run *contenders* together until one finishes, then cancel the rest.

        A contender that failed (rather than being cancelled here) has its
        exception raised to the caller.
        """
        tasks = [asyncio.create_task(contender) for contender in contenders]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome

    async def _restart(self, status_target: discord.Message | None, thread: discord.Thread) -> None:
        """Announce the restart and launch the restart command without waiting for it.

        The caller has already made sure a ``restart_command`` is configured.
        """
        await thread.send("🔄 Restarting...")
        await self._mark(status_target, _OK)
        # A moment for the two messages above to leave before the process dies.
        await asyncio.sleep(1)

        command = self.config.restart_command or []
        await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
