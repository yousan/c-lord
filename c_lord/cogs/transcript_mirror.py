"""Cog: JSONL transcript → Discord thread mirror (Issue #71).

This Cog tails ``~/.claude/projects/<slug>/`` for every thread that has a stored
``working_dir`` and forwards rendered events to the corresponding Discord
thread. Since #712 it is the *only* delivery path: it reads what Claude Code
already wrote, so a turn cannot go undelivered because Claude forgot to post it
(the failure mode of the retired skill-push path, #491).

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
import os
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..discord_ui.turn_progress import TurnProgress
from ..notify_policy import owner_notify_id
from ..transcript.mirror import (
    TranscriptMirror,
    UserFileRequest,
    reply_to_trigger_enabled,
    send_user_file_enabled,
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

#: Attachments Discord accepts on one message.  A message carrying more is
#: rejected whole, so a ``SendUserFile`` call with more files than this is split
#: across several messages rather than truncated (#233).
MAX_ATTACHMENTS = 10

#: Default ceiling for one attachment.  Discord's own limit depends on the
#: guild's boost tier (10 MB unboosted, 50/100 MB boosted); this is a sanity
#: guard so a stray multi-gigabyte path is reported instead of uploaded.
#: Override with ``CLORD_USER_FILE_MAX_BYTES``.
DEFAULT_USER_FILE_MAX_BYTES = 25 * 1024 * 1024


def user_file_max_bytes() -> int:
    """Per-file ceiling for ``SendUserFile`` attachments (#233)."""
    raw = os.getenv("CLORD_USER_FILE_MAX_BYTES", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_USER_FILE_MAX_BYTES
    return value if value > 0 else DEFAULT_USER_FILE_MAX_BYTES


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _code(name: str) -> str:
    """Render *name* as inline code for a Discord message.

    The backtick is the one character that would let a filename escape the span
    and turn the rest of the sentence into markdown of its own choosing — the
    name reaches us from a tool call's argument, so it is replaced rather than
    trusted.
    """
    return f"`{name.replace('`', '_')}`"


class TranscriptMirrorCog(commands.Cog):
    """Owns a ``TranscriptMirror`` per active thread — and one per project dir.

    A thread normally has a working copy to itself (``c-lord-sessions/<ch>/<thr>``),
    so "one mirror per thread" and "one mirror per transcript" mean the same
    thing.  They come apart whenever a ``working_dir`` is *named* rather than
    derived: a scheduled task makes a new thread every run while keeping its
    checkout, and ``/clord-thread-init`` can point two threads at one path.  Both
    threads then resolve to the same ``~/.claude/projects/<slug>`` and tail the
    same jsonl, so one thread's turn is posted into the other as well — last
    week's finished thread fills up with this week's work, one message at a time
    (#719).  :attr:`_owners` is what keeps that to one mirror.
    """

    def __init__(self, bot: commands.Bot, *, session_repo: SessionRepository) -> None:
        self.bot = bot
        self._session_repo = session_repo
        self._mirrors: dict[int, TranscriptMirror] = {}
        self._trigger_messages: dict[int, int] = {}
        # project dir → the one thread allowed to mirror it (#719).  Derived
        # state, kept beside ``_mirrors`` rather than scanned out of it so the
        # on_ready walk over every session row stays linear.
        self._owners: dict[Path, int] = {}
        # In-flight cancellations of displaced mirrors — see :meth:`_release`.
        self._releasing: set[asyncio.Task[None]] = set()

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
        # Sessions are bounded by Discord usage; 10k is well above realistic.
        rows = await self._session_repo.list_all(limit=10_000)
        started = 0
        recovered = 0
        closed = 0
        duplicate = 0
        # #719: ``list_all`` is ordered ``last_used_at DESC``, so for a shared
        # working_dir the first row is the thread that used the workspace last —
        # the one the transcript belongs to.  Every later row pointing at the
        # same project dir is last run's thread; restoring a mirror (or a #215
        # recovery post) for it is what replays this run's work into it.
        claimed: dict[Path, int] = {}
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
            project_dir = derive_project_dir(row.working_dir)
            owner = claimed.get(project_dir)
            if owner is not None:
                duplicate += 1
                logger.warning(
                    "TranscriptMirrorCog: not mirroring thread=%d — project_dir=%s is already "
                    "mirrored by thread=%d, which used it more recently; they share a "
                    "working_dir and one transcript may only feed one thread (#719)",
                    row.thread_id,
                    project_dir,
                    owner,
                )
                continue
            claimed[project_dir] = row.thread_id
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
            if self.start_for(row.thread_id, row.working_dir, expect_turn=False):
                started += 1
        logger.info(
            "TranscriptMirrorCog: started %d mirror(s) from %d session row(s) "
            "(%d closed row(s) skipped, %d sharing an already-mirrored working_dir), "
            "recovered %d dropped final answer(s)",
            started,
            len(rows),
            closed,
            duplicate,
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

    def start_for(self, thread_id: int, working_dir: str, *, expect_turn: bool = True) -> bool:
        """Spawn a mirror for ``thread_id`` if one is not already running.

        Returns True if a new mirror was started, False if one already exists.

        ``expect_turn`` says whether somebody is waiting on this mirror right
        now, which is what decides whether "I cannot find this thread's
        transcript" is worth telling the thread about (#773).  True for every
        caller that is about to run a turn (chat, scheduler, webhook); the
        ``on_ready`` restore passes False, because a workspace nobody has run
        Claude in legitimately has nothing to read and there are hundreds of
        them on this host.

        **At most one mirror per project dir** (#719).  A caller here is a
        thread whose turn is *starting*, so it is the session about to write
        that transcript: it takes the claim over from whoever held it and the
        previous holder's mirror is stopped.  Refusing the newcomer instead
        would leave the live thread with no delivery path at all — the jsonl
        mirror is the only one there is (#712).  ``on_ready`` never reaches this
        branch: it walks the rows newest-first and skips the stale duplicates
        itself, because there the *first* claim is the right one.
        """
        if thread_id in self._mirrors:
            return False

        project_dir = derive_project_dir(working_dir)
        incumbent = self._owners.get(project_dir)
        if incumbent is not None and incumbent != thread_id:
            logger.warning(
                "TranscriptMirrorCog: thread=%d takes the mirror of project_dir=%s over from "
                "thread=%d — they share a working_dir, so only the thread whose turn is "
                "starting may tail it (#719)",
                thread_id,
                project_dir,
                incumbent,
            )
            self._release(incumbent)

        sink = self._make_sink(thread_id)
        reply_sink = self._make_reply_sink(thread_id)
        file_sink = self._make_file_sink(thread_id)
        reply_cursor_sink = self._make_cursor_sink(thread_id)
        fold_post, fold_edit = self._make_fold(thread_id)
        mirror = TranscriptMirror(
            thread_id=thread_id,
            project_dir=project_dir,
            sink=sink,
            reply_sink=reply_sink,
            file_sink=file_sink,
            user_file_sink=self._make_user_file_sink(thread_id),
            reply_cursor_sink=reply_cursor_sink,
            verbosity=verbosity_mode(),
            ask_bridge_cb=self._make_ask_bridge(thread_id),
            progress=self._make_progress(thread_id),
            fold_post=fold_post,
            fold_edit=fold_edit,
            expect_turn=expect_turn,
        )
        mirror.start()
        self._mirrors[thread_id] = mirror
        self._owners[project_dir] = thread_id
        logger.info(
            "TranscriptMirrorCog: started mirror thread=%d project_dir=%s",
            thread_id,
            project_dir,
        )
        return True

    def _release(self, thread_id: int) -> None:
        """Drop ``thread_id``'s mirror now; let its tail task unwind in the background.

        Ownership has to change hands inside :meth:`start_for`, which is sync and
        called from the turn-start path — an ``await`` there would let the two
        mirrors overlap for exactly as long as the cancellation takes.  Dropping
        the bookkeeping first and awaiting the cancellation afterwards keeps the
        overlap at zero posts: the tail is cancelled before Claude has written
        anything for the new turn.
        """
        mirror = self._mirrors.pop(thread_id, None)
        if mirror is None:
            return
        self._forget_owner(thread_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - start_for always runs on the loop
            return
        task = loop.create_task(
            self._stop_quietly(mirror), name=f"transcript-mirror-release-{thread_id}"
        )
        # Hold the reference: asyncio keeps only a weak one, and a release task
        # collected mid-flight would leave the displaced mirror tailing — the
        # exact thing this call exists to prevent.
        self._releasing.add(task)
        task.add_done_callback(self._releasing.discard)

    async def _stop_quietly(self, mirror: TranscriptMirror) -> None:
        try:
            await mirror.stop()
        except Exception:  # pragma: no cover - defensive; a stop must not surface
            logger.warning(
                "TranscriptMirrorCog: displaced mirror thread=%d did not stop cleanly",
                mirror.thread_id,
                exc_info=True,
            )

    def _forget_owner(self, thread_id: int) -> None:
        for project_dir, owner in list(self._owners.items()):
            if owner == thread_id:
                del self._owners[project_dir]

    async def stop_for(self, thread_id: int) -> None:
        mirror = self._mirrors.pop(thread_id, None)
        self._forget_owner(thread_id)
        if mirror is not None:
            await mirror.stop()

    async def cog_unload(self) -> None:
        await asyncio.gather(
            *(m.stop() for m in self._mirrors.values()),
            # Displaced mirrors are no longer in ``_mirrors`` but may still be
            # unwinding, so they are waited on here too (#719).
            *tuple(self._releasing),
            return_exceptions=True,
        )
        self._mirrors.clear()
        self._owners.clear()

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

    def _make_fold(self, thread_id: int):
        """Post/edit for the #747 repeat counter: one message that keeps the count.

        Wired here so an upgrade alone turns it on (Zero-Config Principle). Sent
        like any intermediate message — silent, no link cards — because it
        stands in for exactly those messages.
        """
        bot = self.bot

        async def post(text: str):
            channel = await self._resolve_channel(bot, thread_id)
            send = getattr(channel, "send", None) if channel is not None else None
            if send is None:
                return None
            return await self._send_chunks(send, text, silent=silent_posts_enabled())

        async def edit(handle, text: str) -> None:
            # discord.py's edit() defaults to suppress=False, which clears the
            # flag the send set — a quoted URL would then unfurl (#372).
            await handle.edit(content=text, suppress=not show_url_embeds_enabled())

        return post, edit

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
                tmux_manager = await channel_cog.resolve_tmux_manager(
                    parent_id, thread_id=thread_id
                )
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
                # #739: without this the AskView is built with no authorizer,
                # and a View with no authorizer could not tell who was allowed.
                authorizer=getattr(bot, "authorizer", None),
                notify_user_id=owner_notify_id(bot, kind="blocked"),
            )

        return ask_bridge

    def _make_sink(self, thread_id: int):
        """Return an awaitable callable that posts intermediate messages silently.

        Silent, but not second-class: a GFM table here is rendered to a PNG just
        as it is on the final answer (#683).  Discord draws no markdown tables,
        so skipping this left an intermediate table as a column of raw pipes —
        and those messages stay in the thread after the turn ends, so the
        unreadable version is what the reader is left with.
        """
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
                await self._send_chunks(
                    send,
                    text,
                    silent=silent_posts_enabled(),
                    tables=True,
                )
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
            reference = await self._build_trigger_reference(thread_id)
            try:
                last_msg = await self._send_chunks(send, text, reference=reference, tables=True)
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
            # progress.txt rides on the last message and takes one of its 10
            # attachment slots; _send_chunks gives that message's tables one
            # fewer — the turn log must never be the thing dropped (#683).
            files = [discord.File(file_path, filename="progress.txt")]
            reference = await self._build_trigger_reference(thread_id)

            from ..skills.reply_tracker import record_reply_message

            try:
                last_msg = await self._send_chunks(
                    send, text, reference=reference, files=files, tables=True
                )
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

    def _make_user_file_sink(self, thread_id: int):
        """Return the sink that puts ``SendUserFile`` files on Discord (#233).

        Returns ``None`` when ``CLORD_SEND_USER_FILE=0``.  Wired here rather
        than left to consumers: before this, the harness tool reported success
        while nothing arrived, so an upgrade alone has to fix it (Zero-Config).

        Deliberately its own message rather than a rider on the final answer:
        a call can carry more files than one message holds, the prose that
        introduces them ("下に貼ります") is written *before* the call, and a turn
        that dies before its final answer would otherwise lose them entirely.
        Being its own message also means these files never compete with
        ``progress.txt`` / table images for the 10-attachment budget (#683).
        """
        if not send_user_file_enabled():
            return None
        bot = self.bot

        async def user_file_sink(request: UserFileRequest) -> None:
            channel = await self._resolve_channel(bot, thread_id)
            send = getattr(channel, "send", None) if channel is not None else None
            if send is None:
                logger.warning(
                    "TranscriptMirror user_file_sink: no channel for thread=%d — "
                    "%d attachment(s) not delivered",
                    thread_id,
                    len(request.paths),
                )
                return

            sendable, problems = self._vet_user_files(request.paths)
            delivered = 0
            for index, batch in enumerate(
                [
                    sendable[i : i + MAX_ATTACHMENTS]
                    for i in range(0, len(sendable), MAX_ATTACHMENTS)
                ]
            ):
                # The caption introduces the files, so it rides the first batch.
                caption = request.caption if index == 0 else None
                sent, failures = await self._send_user_file_batch(send, batch, caption)
                delivered += sent
                problems.extend(failures)

            if request.caption and not sendable:
                # Nothing to attach, but Claude still wrote a sentence about it —
                # posting the caption alone keeps the warning below in context.
                with contextlib.suppress(discord.HTTPException):
                    await self._send_chunks(send, request.caption, silent=True)

            if problems:
                # #678 / #233: the failure mode being fixed is silence. Say which
                # file did not arrive and why — in the thread, not only in a log.
                logger.info(
                    "TranscriptMirror user_file_sink: thread=%d delivered=%d undelivered=%d (%s)",
                    thread_id,
                    delivered,
                    len(problems),
                    "; ".join(problems),
                )
                note = "⚠️ 次のファイルは添付できませんでした:\n" + "\n".join(
                    f"- {p}" for p in problems
                )
                with contextlib.suppress(discord.HTTPException):
                    # Not silent: the reader is waiting for a file that is not
                    # coming, so this is exactly the case worth a notification.
                    await self._send_chunks(send, note)

        return user_file_sink

    @staticmethod
    def _vet_user_files(paths: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
        """Split *paths* into sendable ``(path, display_name)`` pairs and problems.

        Checked before touching Discord so a bad path costs a line of text
        instead of the whole message.  The display name is reduced to one
        harmless component — it goes into a Discord API payload, and the path
        came from a tool call, not from us.
        """
        from ..attachments import sanitize_filename

        max_bytes = user_file_max_bytes()
        sendable: list[tuple[str, str]] = []
        problems: list[str] = []
        for raw in paths:
            path = Path(raw)
            name = sanitize_filename(path.name)
            try:
                if not path.is_file():
                    problems.append(f"{_code(name)} — ファイルが見つかりません")
                    continue
                size = path.stat().st_size
            except OSError as exc:
                problems.append(f"{_code(name)} — 読めませんでした ({exc.strerror or exc})")
                continue
            if size > max_bytes:
                problems.append(
                    f"{_code(name)} — {_mb(size)} は上限 {_mb(max_bytes)} を超えています"
                )
                continue
            sendable.append((str(path), name))
        return sendable, problems

    @classmethod
    async def _send_user_file_batch(
        cls,
        send,
        batch: list[tuple[str, str]],
        caption: str | None,
    ) -> tuple[int, list[str]]:
        """Send one message carrying *batch*; retry file-by-file if it is rejected.

        Discord rejects a message as a whole, so a single oversized or unreadable
        file would otherwise take its nine innocent neighbours with it. Returns
        the number delivered and a problem line for each one that was not.
        """
        files: list[discord.File] = []
        try:
            files = [discord.File(p, filename=n) for p, n in batch]
            await cls._send_chunks(send, caption or "", silent=True, files=files)
            return len(batch), []
        except (discord.HTTPException, OSError) as exc:
            # Bound outside the handler: ``except ... as`` unbinds the name at
            # the end of the block, and the reason is needed below.
            failure: BaseException = exc
            # discord.File opens its path on construction, and a send that never
            # completed leaves those handles to the garbage collector. The retry
            # below reopens them, so close these now rather than accumulate one
            # dangling descriptor per rejected attachment.
            for f in files:
                with contextlib.suppress(Exception):
                    f.close()
            logger.warning(
                "TranscriptMirror user_file_sink: batch of %d rejected (%s) — "
                "retrying one at a time",
                len(batch),
                exc,
            )

        if len(batch) == 1:
            _path, name = batch[0]
            return 0, [f"{_code(name)} — Discord に拒否されました ({cls._reason(failure)})"]

        delivered = 0
        problems: list[str] = []
        for pair in batch:
            sent, failures = await cls._send_user_file_batch(send, [pair], None)
            delivered += sent
            problems.extend(failures)
        return delivered, problems

    @staticmethod
    def _reason(exc: BaseException) -> str:
        """A short, human-readable cause for a rejected attachment."""
        status = getattr(exc, "status", None)
        text = getattr(exc, "text", None) or str(exc)
        return f"{status}: {text}" if status else str(text)

    @staticmethod
    def _table_files(
        text: str, chunks: list[str], *, reserved: int = 0
    ) -> list[list[discord.File]]:
        """Render every GFM table in *text* to a ``discord.File``, per chunk (#683/#750).

        Shared by all three sinks (through :meth:`_send_chunks`) so that whether
        a table becomes an image never depends on which sink happened to post
        it. Entry ``i`` holds the images for the tables in ``chunks[i]`` — the
        message the reader sees the table in (#750). Each message gets Discord's
        10-per-message allowance; *reserved* is the number of slots the last
        message needs for files of its own (``progress.txt``).
        """
        from io import BytesIO

        from ..discord_ui.table_renderer import MAX_TABLE_IMAGES, get_table_images_per_chunk

        limits = [MAX_TABLE_IMAGES] * len(chunks)
        if limits:
            limits[-1] -= reserved
        return [
            [discord.File(BytesIO(img), filename=fname) for fname, img in images]
            for images in get_table_images_per_chunk(text, chunks, limits=limits)
        ]

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

    @classmethod
    async def _send_chunks(
        cls,
        send,
        text: str,
        *,
        silent: bool = False,
        reference: discord.MessageReference | None = None,
        files: list[discord.File] | None = None,
        tables: bool = False,
    ) -> discord.Message | None:
        """Send ``text`` split into Discord-sendable chunks (Issue #235).

        Long bodies are split instead of truncated. The quote-reply
        ``reference`` rides on the first chunk; ``files`` ride on the last.
        With ``tables=True`` each GFM table is also attached as a PNG to the
        chunk that contains it (#683), not to the last one (#750).
        Returns the last sent ``Message`` (so callers can track it for
        in-place edits — e.g. the context-usage line).
        """
        from ..discord_ui.reply_chunker import chunk_discord_content

        chunks = chunk_discord_content(text)
        extra = list(files or [])
        table_files = (
            cls._table_files(text, chunks, reserved=len(extra)) if tables else [[] for _ in chunks]
        )
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
            attach = (extra if idx == last else []) + table_files[idx]
            if attach:
                kwargs["files"] = attach
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
