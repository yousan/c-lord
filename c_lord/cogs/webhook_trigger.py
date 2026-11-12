"""Lets a Discord webhook post start a Claude run that the server defined.

A CI job (or anything else that can post to a webhook) sends a message whose
text starts with a configured *prefix*; the prompt Claude then runs is the one
configured for that prefix on the bot's side.

What keeps this safe:

- Only webhook messages are considered — a person typing the prefix does nothing.
- An optional allowlist narrows it to particular webhook ids.
- The message chooses *which* trigger fires and nothing more; its text never
  reaches Claude.
- A prefix runs one at a time: a second post while one is running is dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from .. import turn_events
from ..concurrency import SessionRegistry
from ..discord_failures import DISCORD_CALL_FAILURES
from ..discord_ui.thread_dashboard import board_turn, dashboard_of
from ..model_scopes import begin_turn_for, effort_for_launch
from ..notify_policy import owner_notify_id
from ..thread_settings import resolve_auto_archive_duration
from ..utils.logger import log_ctx
from ..voice_webhooks import is_voice_message
from ._run_helper import run_claude_with_config
from .run_config import RunConfig

if TYPE_CHECKING:
    from ..claude.config import ClaudeConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookTrigger:
    """What one prefix runs.

    Args:
        prompt: The prompt Claude is given when the trigger fires.
        working_dir: Override the default working directory.
        timeout: Override the default timeout in seconds.
        allowed_tools: Override the default allowed tools list.
        dangerously_skip_permissions: Run Claude Code without permission prompts.
    """

    prompt: str
    working_dir: str | None = None
    timeout: int = 300
    allowed_tools: list[str] | None = None
    dangerously_skip_permissions: bool = True


class WebhookTriggerCog(commands.Cog):
    """Watches for webhook posts and runs the trigger whose prefix they carry.

    Args:
        bot: The bot the cog is loaded into.
        runner: ClaudeConfig with CLI settings.
        triggers: Prefix → what it runs. Checked in the dict's order.
        allowed_webhook_ids: Webhooks that may fire a trigger; ``None`` = any.
        channel_ids: Channels that are listened to; ``None`` = all.
        registry: Session registry; defaults to the bot's own.
    """

    def __init__(
        self,
        bot: commands.Bot,
        runner: ClaudeConfig,
        triggers: dict[str, WebhookTrigger],
        allowed_webhook_ids: set[int] | None = None,
        channel_ids: set[int] | None = None,
        registry: SessionRegistry | None = None,
    ) -> None:
        self.bot = bot
        self.runner = runner
        self.triggers = triggers
        self.allowed_webhook_ids = allowed_webhook_ids
        self.channel_ids = channel_ids
        self._registry = registry or getattr(bot, "session_registry", None)
        # Mutual exclusion per prefix, for the prefixes configured right now.
        # A run holds its prefix's lock from thread creation to the reaction.
        self._locks: dict[str, asyncio.Lock] = {prefix: asyncio.Lock() for prefix in triggers}
        self._active_count: int = 0

    # -- how many runs are under way (``DrainAware``) -------------------

    @property
    def active_count(self) -> int:
        """Trigger runs in progress — the upgrade cog waits for this to reach 0."""
        return self._active_count

    @contextlib.contextmanager
    def _counted_run(self) -> Iterator[None]:
        """Count one run as active for as long as the block lasts, however it ends."""
        self._active_count += 1
        try:
            yield
        finally:
            self._active_count -= 1

    # -- which messages fire which trigger ------------------------------

    def _sender_accepted(self, message: discord.Message) -> bool:
        """With an allowlist, only listed webhooks count (an empty list admits none)."""
        allowed = self.allowed_webhook_ids
        return allowed is None or message.webhook_id in allowed

    def _channel_watched(self, message: discord.Message) -> bool:
        """With a channel list, only posts in those channels count."""
        watched = self.channel_ids
        return watched is None or message.channel.id in watched

    def _trigger_named_by(self, text: str) -> tuple[str, WebhookTrigger] | None:
        """The first configured prefix *text* starts with, and what it runs.

        Case-sensitive; nothing has to separate the prefix from what follows.
        """
        text = text.strip()
        for prefix, trigger in self.triggers.items():
            if text.startswith(prefix):
                return prefix, trigger
        return None

    def _match(self, message: discord.Message) -> tuple[str, WebhookTrigger] | None:
        """The trigger *message* fires, or ``None`` when it is not one of ours."""
        if not message.webhook_id or is_voice_message(message):
            return None  # #971: c-lord speaking for a sub-agent triggers nothing
        if not (self._sender_accepted(message) and self._channel_watched(message)):
            return None
        return self._trigger_named_by(message.content)

    def claims_message(self, message: discord.Message) -> bool:
        """Whether *message* is a trigger this cog acts on — #862.

        ``ClaudeChatCog`` asks before turning a webhook message in a thread with
        no session into a new conversation, so a trigger is never also run as a
        request to Claude.
        """
        return self._match(message) is not None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Run the trigger a webhook post names — unless that trigger is busy."""
        matched = self._match(message)
        if matched is None:
            return
        matched_prefix, matched_trigger = matched
        logger.info(
            "%s Webhook trigger matched: prefix=%r, webhook_id=%d",
            log_ctx(channel_id=message.channel.id),
            matched_prefix,
            message.webhook_id,
        )

        # A busy prefix drops the post — it is neither queued nor run later.
        # Nothing is awaited between looking at the lock and taking it, so two
        # posts arriving together cannot both get through.
        in_use = self._locks[matched_prefix]
        if in_use.locked():
            await message.reply(f"⏳ `{matched_prefix}` is already running. Skipping.")
            return
        async with in_use:
            await self._execute_trigger(message, matched_prefix, matched_trigger)

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
            await message.add_reaction("❌")

    async def _execute_trigger(
        self,
        message: discord.Message,
        prefix: str,
        trigger: WebhookTrigger,
    ) -> None:
        """Run *trigger* in a thread under *message*, then mark it ✅ or ❌."""
        from ..claude.tmux_runner import TmuxClaudeRunner

        archive_minutes = await resolve_auto_archive_duration(
            getattr(self.bot, "settings_repo", None)
        )
        thread = await message.create_thread(
            name=prefix[:100], auto_archive_duration=archive_minutes
        )

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

        with self._counted_run():
            # #754: a CI-triggered run belongs on 📊 Session Status too.
            # Labelled by the public prefix — the prompt is server-side config.
            async with board_turn(dashboard_of(self.bot), thread.id, prefix):
                session_id = await run_claude_with_config(
                    RunConfig(
                        thread=thread,
                        runner=runner,
                        # A webhook run leaves no session row behind.
                        repo=None,
                        prompt=trigger.prompt,
                        session_id=None,
                        status=None,
                        registry=self._registry,
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
                    )
                )
            await message.add_reaction("✅" if session_id else "❌")
