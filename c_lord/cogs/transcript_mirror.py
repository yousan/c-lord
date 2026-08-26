"""Cog: JSONL transcript → Discord thread mirror (Issue #71).

When ``CLORD_BRIDGE_MODE=jsonl``, this Cog tails ``~/.claude/projects/<slug>/``
for every thread that has a stored ``working_dir`` and forwards rendered events
to the corresponding Discord thread.  It coexists with the legacy skill-based
reply path: the skill injection itself is suppressed under jsonl mode so each
event is posted at most once.

Lifecycle:
- ``on_ready``: walk the sessions table and start a mirror task for every row
  whose ``working_dir`` resolves to an existing Claude Code project dir.
- :meth:`start_for` is called from :mod:`c_lord.cogs.claude_chat` when a new
  thread is provisioned (or an existing thread continues a session).
- :meth:`cog_unload` cancels all running mirror tasks cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..discord_ui.turn_progress import TurnProgress
from ..notify_policy import owner_notify_id
from ..transcript.mirror import (
    TranscriptMirror,
    bridge_mode_jsonl,
    reply_to_trigger_enabled,
    show_url_embeds_enabled,
    silent_posts_enabled,
    turn_progress_enabled,
    turn_progress_quiet_seconds,
    verbosity_mode,
)
from ..transcript.recovery import final_answer_needs_recovery_async
from ..transcript.resolver import derive_project_dir

if TYPE_CHECKING:
    from ..database.repository import SessionRepository

logger = logging.getLogger(__name__)


class TranscriptMirrorCog(commands.Cog):
    """Owns a ``TranscriptMirror`` per active thread."""

    def __init__(self, bot: commands.Bot, *, session_repo: SessionRepository) -> None:
        self.bot = bot
        self._session_repo = session_repo
        self._mirrors: dict[int, TranscriptMirror] = {}
        self._trigger_messages: dict[int, int] = {}

    def set_trigger_message(self, thread_id: int, message_id: int) -> None:
        """Record the Discord message ID that triggered the current Claude turn.

        Called by ClaudeChatCog before each run so that the reply_sink can
        thread the final answer back to the user's message.
        """
        self._trigger_messages[thread_id] = message_id
        # #539: this is the earliest point c-lord knows a turn started, so it is
        # where the "how long have I been waiting" clock should start.
        mirror = self._mirrors.get(thread_id)
        if mirror is not None:
            mirror.note_turn_started()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not bridge_mode_jsonl():
            logger.info("TranscriptMirrorCog: CLORD_BRIDGE_MODE != jsonl — staying idle")
            return
        # Sessions are bounded by Discord usage; 10k is well above realistic.
        rows = await self._session_repo.list_all(limit=10_000)
        started = 0
        recovered = 0
        closed = 0
        for row in rows:
            if not row.working_dir:
                continue
            # Issue #537: a closed workspace (``!close-workspace``) keeps its
            # row and its transcript — often the biggest ones on disk. Nobody is
            # waiting on it, so neither the recovery scan nor a mirror is worth
            # the startup cost.
            if getattr(row, "closed_at", None):
                closed += 1
                continue
            # Issue #215: re-deliver a final answer that was written to the
            # jsonl while the bot was down (mirror not tailing). The resumed
            # mirror tails from EOF and would otherwise skip it forever.
            try:
                if await self._recover_final_answer(row.thread_id, row.working_dir, row):
                    recovered += 1
            except Exception:
                logger.warning(
                    "TranscriptMirrorCog: final-answer recovery failed thread=%d",
                    row.thread_id,
                    exc_info=True,
                )
            if self.start_for(row.thread_id, row.working_dir):
                started += 1
        logger.info(
            "TranscriptMirrorCog: started %d mirror(s) from %d session row(s) "
            "(%d closed row(s) skipped), recovered %d dropped final answer(s)",
            started,
            len(rows),
            closed,
            recovered,
        )

    async def _recover_final_answer(self, thread_id: int, working_dir: str, row) -> bool:
        """Re-deliver the last completed turn's final answer if it was dropped.

        Returns True if a recovery post was made. Whether an answer counts as
        delivered is decided by :func:`final_answer_needs_recovery` — by the
        *position* of the stored ``mirror_replied_uuid`` cursor relative to the
        answer, not by equality with it (#553).
        """
        stored = getattr(row, "mirror_replied_uuid", None)
        # #553: "not equal to the cursor" is NOT the same as "was dropped". A
        # turn still running at shutdown leaves the cursor on a later line than
        # the last completed turn's final answer, and the equality test then read
        # that as a drop and re-posted an answer the user had already read. Ask
        # the ordering question instead: has the cursor already passed it?
        # Awaited off the loop (#537): the scan reads a whole transcript.
        fa = await final_answer_needs_recovery_async(derive_project_dir(working_dir), stored)
        if fa is None:
            return False
        if stored is None:
            # First time we track this session (e.g. right after the column was
            # added by migration). We cannot tell whether the pre-fix mirror
            # delivered this answer, so assume it did and only seed the cursor —
            # otherwise every existing thread would be spammed with its last
            # answer on the first deploy. Genuine drops are caught on the *next*
            # restart, when the cursor is set and a newer turn differs.
            await self._session_repo.set_mirror_replied_uuid(thread_id, fa.uuid)
            return False
        # The cursor sits BEFORE this answer: it completed while the mirror was
        # down, so nothing ever posted it. Re-deliver it once.
        reply_sink = self._make_reply_sink(thread_id)
        await reply_sink(fa.text)
        await self._session_repo.set_mirror_replied_uuid(thread_id, fa.uuid)
        logger.info(
            "TranscriptMirrorCog: recovered dropped final answer thread=%d uuid=%s",
            thread_id,
            fa.uuid,
        )
        return True

    def start_for(self, thread_id: int, working_dir: str) -> bool:
        """Spawn a mirror for ``thread_id`` if one is not already running.

        Returns True if a new mirror was started, False if one already exists
        or bridge mode is not jsonl.
        """
        if not bridge_mode_jsonl():
            return False
        if thread_id in self._mirrors:
            return False

        project_dir = derive_project_dir(working_dir)
        sink = self._make_sink(thread_id)
        reply_sink = self._make_reply_sink(thread_id)
        file_sink = self._make_file_sink(thread_id)
        reply_cursor_sink = self._make_cursor_sink(thread_id)
        mirror = TranscriptMirror(
            thread_id=thread_id,
            project_dir=project_dir,
            sink=sink,
            reply_sink=reply_sink,
            file_sink=file_sink,
            reply_cursor_sink=reply_cursor_sink,
            verbosity=verbosity_mode(),
            ask_bridge_cb=self._make_ask_bridge(thread_id),
            progress=self._make_progress(thread_id),
        )
        mirror.start()
        self._mirrors[thread_id] = mirror
        logger.info(
            "TranscriptMirrorCog: started mirror thread=%d project_dir=%s",
            thread_id,
            project_dir,
        )
        return True

    async def stop_for(self, thread_id: int) -> None:
        mirror = self._mirrors.pop(thread_id, None)
        if mirror is not None:
            await mirror.stop()

    async def cog_unload(self) -> None:
        await asyncio.gather(*(m.stop() for m in self._mirrors.values()), return_exceptions=True)
        self._mirrors.clear()

    def _make_progress(self, thread_id: int) -> TurnProgress | None:
        """Build the #539 silence filler for *thread_id*, or None when disabled.

        Wired here rather than left to consumers: a c-lord upgrade alone has to
        turn the feature on (Zero-Config Principle). ``CLORD_TURN_PROGRESS=0``
        opts out.
        """
        if not turn_progress_enabled():
            return None
        bot = self.bot

        async def post(text: str):
            channel = await self._resolve_channel(bot, thread_id)
            send = getattr(channel, "send", None) if channel is not None else None
            if send is None:
                return None
            # Silent: the whole point is a low-noise hint, not a notification.
            return await send(text, silent=True)

        async def edit(handle, text: str) -> None:
            await handle.edit(content=text)

        async def delete(handle) -> None:
            await handle.delete()

        return TurnProgress(
            post=post,
            edit=edit,
            delete=delete,
            quiet_seconds=turn_progress_quiet_seconds(),
        )

    def _make_cursor_sink(self, thread_id: int):
        """Return an awaitable that records the delivered final-answer uuid.

        Issue #215: persists ``mirror_replied_uuid`` after each completed turn
        so a restart can tell the final answer was already delivered.
        """

        async def cursor_sink(uuid: str) -> None:
            with contextlib.suppress(Exception):
                await self._session_repo.set_mirror_replied_uuid(thread_id, uuid)

        return cursor_sink

    def _make_ask_bridge(self, thread_id: int):
        """Return an async cb that bridges an AskUserQuestion menu to Discord (#232).

        Builds a TmuxClaudeRunner for the thread's tmux window (to deliver the
        chosen option as menu keystrokes) and shows Discord buttons via
        ``bridge_pane_ask`` — so a menu raised outside a bot ``run_claude`` turn
        (e.g. autonomous task-notification continuation) is no longer leaked.
        """
        bot = self.bot

        async def ask_bridge(question) -> None:
            from ..claude.tmux_runner import TmuxClaudeRunner
            from ..discord_ui.ask_bus import ask_bus
            from ..discord_ui.ask_handler import bridge_pane_ask
            from .channel_repo import ChannelRepoCog

            # Defer to the run_claude poll-loop bridge if it already owns this menu.
            if ask_bus.is_active(thread_id):
                return
            channel = await self._resolve_channel(bot, thread_id)
            if not isinstance(channel, discord.Thread):
                return
            parent_id = getattr(channel, "parent_id", None) or thread_id
            channel_cog = bot.get_cog("ChannelRepoCog")
            tmux_manager = None
            if isinstance(channel_cog, ChannelRepoCog):
                tmux_manager = await channel_cog.resolve_tmux_manager(parent_id)
            if tmux_manager is None:
                tmux_manager = getattr(bot, "tmux_manager", None)
            if tmux_manager is None:
                logger.warning(
                    "TranscriptMirror ask-bridge: no tmux manager for thread=%d", thread_id
                )
                return
            runner = TmuxClaudeRunner(tmux_manager=tmux_manager, thread_id=thread_id)
            # #480: this menu was raised outside a Discord-driven turn (terminal /
            # autonomous continuation), so there is no per-turn poster — fall back
            # to the bot owner so the blocking question still pings someone.
            await bridge_pane_ask(
                channel,
                question,
                runner,
                ask_repo=getattr(bot, "ask_repo", None),
                notify_user_id=owner_notify_id(bot, kind="blocked"),
            )

        return ask_bridge

    def _make_sink(self, thread_id: int):
        """Return an awaitable callable that posts intermediate messages silently."""
        bot = self.bot

        async def sink(text: str) -> None:
            channel = await self._resolve_channel(bot, thread_id)
            if channel is None:
                return
            send = getattr(channel, "send", None)
            if send is None:
                logger.warning(
                    "TranscriptMirror sink: channel %d has no .send (got %s)",
                    thread_id,
                    type(channel).__name__,
                )
                return
            try:
                await self._send_chunks(send, text, silent=silent_posts_enabled())
            except discord.HTTPException as exc:
                logger.warning(
                    "TranscriptMirror sink failed: thread=%d body_len=%d status=%s — %s",
                    thread_id,
                    len(text),
                    getattr(exc, "status", "?"),
                    exc,
                    exc_info=True,
                )

        return sink

    def _make_reply_sink(self, thread_id: int):
        """Return an awaitable callable for final assistant text (no progress.txt).

        Sends without silent flag (notifies user) and includes a reference to
        the trigger message so the reply threads visually in Discord.
        """
        bot = self.bot

        async def reply_sink(text: str) -> None:
            channel = await self._resolve_channel(bot, thread_id)
            if channel is None:
                return
            send = getattr(channel, "send", None)
            if send is None:
                return
            from io import BytesIO

            from ..discord_ui.table_renderer import get_table_images

            table_files = [
                discord.File(BytesIO(img), filename=fname) for fname, img in get_table_images(text)
            ]
            reference = await self._build_trigger_reference(thread_id)
            try:
                last_msg = await self._send_chunks(
                    send, text, reference=reference, files=table_files or None
                )
            except discord.HTTPException as exc:
                logger.warning(
                    "TranscriptMirror reply_sink failed: thread=%d body_len=%d status=%s — %s",
                    thread_id,
                    len(text),
                    getattr(exc, "status", "?"),
                    exc,
                    exc_info=True,
                )
                return
            if last_msg is not None:
                from ..skills.reply_tracker import record_reply_message

                record_reply_message(thread_id, last_msg)

        return reply_sink

    def _make_file_sink(self, thread_id: int):
        """Return an awaitable callable for final answers with progress.txt attachment.

        Sends without silent flag (notifies user) and includes a reference to
        the trigger message. Falls back to plain text if the attachment send fails.
        """
        bot = self.bot

        async def file_sink(text: str, file_path: str) -> None:
            channel = await self._resolve_channel(bot, thread_id)
            if channel is None:
                return
            send = getattr(channel, "send", None)
            if send is None:
                return
            from io import BytesIO

            from ..discord_ui.table_renderer import get_table_images

            table_files = [
                discord.File(BytesIO(img), filename=fname) for fname, img in get_table_images(text)
            ]
            files = [discord.File(file_path, filename="progress.txt")] + table_files
            reference = await self._build_trigger_reference(thread_id)

            from ..skills.reply_tracker import record_reply_message

            try:
                last_msg = await self._send_chunks(send, text, reference=reference, files=files)
                if last_msg is not None:
                    record_reply_message(thread_id, last_msg)
                return
            except discord.HTTPException as exc:
                logger.warning(
                    "TranscriptMirror file_sink failed (with attachment): "
                    "thread=%d body_len=%d status=%s — retrying without attachment — %s",
                    thread_id,
                    len(text),
                    getattr(exc, "status", "?"),
                    exc,
                    exc_info=True,
                )
            # Fallback: text without attachment (still chunked so it is not truncated).
            try:
                last_msg = await self._send_chunks(send, text, reference=reference)
                if last_msg is not None:
                    record_reply_message(thread_id, last_msg)
            except discord.HTTPException as exc:
                logger.warning(
                    "TranscriptMirror file_sink fallback also failed: "
                    "thread=%d body_len=%d status=%s — %s",
                    thread_id,
                    len(text),
                    getattr(exc, "status", "?"),
                    exc,
                    exc_info=True,
                )

        return file_sink

    async def _build_trigger_reference(self, thread_id: int) -> discord.MessageReference | None:
        """Resolve the trigger message for ``thread_id`` into a reply reference.

        Returns ``None`` when reply-to-trigger is disabled or no trigger id is
        known. Shared by ``reply_sink`` and ``file_sink``.
        """
        if not reply_to_trigger_enabled():
            return None
        trigger_id = self._trigger_messages.get(thread_id)
        if trigger_id is None:
            with contextlib.suppress(Exception):
                record = await self._session_repo.get(thread_id)
                if record is not None:
                    trigger_id = record.trigger_message_id
        if trigger_id is None:
            return None
        return discord.MessageReference(
            message_id=trigger_id,
            channel_id=thread_id,
            fail_if_not_exists=False,
        )

    @staticmethod
    async def _send_chunks(
        send,
        text: str,
        *,
        silent: bool = False,
        reference: discord.MessageReference | None = None,
        files: list[discord.File] | None = None,
    ) -> discord.Message | None:
        """Send ``text`` split into Discord-sendable chunks (Issue #235).

        Long bodies are split instead of truncated. The quote-reply
        ``reference`` rides on the first chunk; ``files`` ride on the last.
        Returns the last sent ``Message`` (so callers can track it for
        in-place edits — e.g. the context-usage line).
        """
        from ..discord_ui.reply_chunker import chunk_discord_content

        chunks = chunk_discord_content(text)
        # #372: suppress URL OGP/link-preview cards on Claude's posts by default.
        # This is the production (jsonl-bridge) reply path, so the flag must be
        # honored here — not just in ext/api_server.py's skill-reply path.
        suppress_embeds = not show_url_embeds_enabled()
        last = len(chunks) - 1
        last_sent: discord.Message | None = None
        for idx, chunk in enumerate(chunks):
            kwargs: dict = {"content": chunk, "suppress_embeds": suppress_embeds}
            if silent:
                kwargs["silent"] = True
            if idx == 0 and reference is not None:
                kwargs["reference"] = reference
                kwargs["mention_author"] = False
            if idx == last and files:
                kwargs["files"] = files
            last_sent = await send(**kwargs)
        return last_sent

    @staticmethod
    async def _resolve_channel(bot, thread_id: int):
        """Fetch the channel/thread from cache or Discord API."""
        channel = bot.get_channel(thread_id)
        if channel is None:
            with contextlib.suppress(discord.HTTPException, discord.NotFound):
                channel = await bot.fetch_channel(thread_id)
        if channel is None:
            logger.warning(
                "TranscriptMirror sink: channel %d not found, dropping post",
                thread_id,
            )
        return channel
