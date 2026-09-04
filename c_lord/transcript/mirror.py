"""Per-thread JSONL transcript → Discord sink pipe.

A :class:`TranscriptMirror` owns one asyncio task that tails the active
Claude Code session jsonl for a project directory and pushes each rendered
event to an awaitable ``sink`` callback (typically ``discord.Thread.send``).
The task survives transient sink errors so a flaky Discord call does not
permanently silence the mirror.

Verbosity modes (``CLORD_MIRROR_VERBOSITY`` env var, default ``minimal``):

- ``minimal``: only final ``assistant_text`` events reach Discord.
  ``tool_use`` / ``tool_result`` events are buffered and written to a
  temporary ``progress.txt`` file that is attached to the assistant reply
  via ``file_sink``.  When ``file_sink`` is ``None``, the assistant text is
  posted via the plain ``sink`` (graceful degradation).
- ``full``: all rendered events are posted to ``sink`` in real time
  (original behaviour, useful for debugging).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path

from ..claude.types import AskQuestion, _parse_ask_questions
from ..discord_ui.ask_bus import ask_bus
from ..discord_ui.bridged_context import bridged_context
from ..discord_ui.pane_context import replace_pane_context
from ..discord_ui.turn_progress import DEFAULT_QUIET_SECONDS, TurnProgress
from .formatter import RenderedEvent, render_event
from .pane_echo import pane_echo
from .tail import tail_events

logger = logging.getLogger(__name__)

Sink = Callable[[str], Awaitable[None]]
FileSink = Callable[[str, str], Awaitable[None]]
# Called with the first AskUserQuestion of a tool_use to bridge it to Discord
# buttons (#232). Constructed by TranscriptMirrorCog (knows tmux + thread).
AskBridgeCb = Callable[[AskQuestion], Coroutine[object, object, None]]


def _first_ask_question(event: dict) -> AskQuestion | None:
    """Extract the first AskUserQuestion menu from a raw transcript event.

    #232: AskUserQuestion is a main-agent tool whose ``tool_use`` always lands
    in the JSONL transcript (richer than pane scraping). The mirror tails this,
    so a menu raised outside a bot ``run_claude`` turn (e.g. autonomous
    task-notification continuation) can still be bridged. Returns ``None`` when
    the event is not an AskUserQuestion tool_use. Only the first question is
    returned — the pane answers one menu at a time (multi-question is a known
    limitation shared with the in-pane bridge).
    """
    content = event.get("message", {}).get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == "AskUserQuestion"
        ):
            questions = _parse_ask_questions(block.get("input", {}) or {})
            if questions and questions[0].options:
                return questions[0]
    return None


def _first_ask_tool_use_id(event: dict) -> str | None:
    """Return the ``tool_use`` id of the first AskUserQuestion block, if any.

    Pairs with :func:`_first_ask_question`: the id is what a later ``tool_result``
    references (``tool_use_id``), so it lets the mirror tell whether the menu has
    already been answered (#262).
    """
    content = event.get("message", {}).get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == "AskUserQuestion"
        ):
            tool_use_id = block.get("id")
            return tool_use_id if isinstance(tool_use_id, str) else None
    return None


def _transcript_has_ask_result(project_dir: Path, tool_use_id: str) -> bool:
    """Return True if any jsonl in *project_dir* answers *tool_use_id* (#262).

    A ``tool_result`` whose ``tool_use_id`` matches means the AskUserQuestion menu
    is already closed/answered. Scanning is cheap (AskUserQuestion is rare and the
    id substring pre-filters lines before JSON parsing), so this runs off-thread
    only when a menu is about to be bridged.
    """
    for path in sorted(project_dir.glob("*.jsonl")):
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if tool_use_id not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    content = event.get("message", {}).get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            and block.get("tool_use_id") == tool_use_id
                        ):
                            return True
        except OSError:
            continue
    return False


# Kinds that are buffered (not posted individually) in minimal mode.
_BUFFERED_KINDS = frozenset({"tool_use", "tool_result"})

# Maximum byte size for progress.txt content.  Caps runaway tool output so
# that progress.txt stays well within Discord's 8 MB file-upload limit.
_PROGRESS_MAX_BYTES = 50_000  # 50 KB


def _truncate_progress(content: str) -> str:
    """Truncate *content* to ``_PROGRESS_MAX_BYTES`` bytes if needed."""
    encoded = content.encode("utf-8")
    if len(encoded) <= _PROGRESS_MAX_BYTES:
        return content
    truncated = encoded[: _PROGRESS_MAX_BYTES - 30].decode("utf-8", errors="ignore")
    return truncated + "\n… [truncated]"


def bridge_mode_jsonl() -> bool:
    """Return True unless ``CLORD_BRIDGE_MODE`` names another mode explicitly.

    #216/#492: jsonl is the default delivery path (mirrors ``skills_enabled()``
    in ``c_lord.skills.injector``, which must stay in lockstep with this
    default so the two paths never disagree for an unset/unrecognized value).
    """
    return os.getenv("CLORD_BRIDGE_MODE", "jsonl").strip().lower() == "jsonl"


def verbosity_mode() -> str:
    """Return the mirror verbosity mode from ``CLORD_MIRROR_VERBOSITY``.

    Defaults to ``"minimal"`` (only final assistant text reaches Discord).
    Set to ``"full"`` for the original behaviour (all events posted live).
    """
    return os.getenv("CLORD_MIRROR_VERBOSITY", "minimal").strip().lower()


def silent_posts_enabled() -> bool:
    """Return True unless ``CLORD_SILENT_POSTS`` is explicitly ``0/false/no``.

    Defaults to True — intermediate posts do not trigger push notifications.
    """
    return os.getenv("CLORD_SILENT_POSTS", "1").strip().lower() not in ("0", "false", "no")


def reply_to_trigger_enabled() -> bool:
    """Return True unless ``CLORD_REPLY_TO_TRIGGER`` is explicitly ``0/false/no``.

    Defaults to True — final answers are sent as Discord replies to the
    message that triggered the Claude turn, so they thread visually.
    """
    return os.getenv("CLORD_REPLY_TO_TRIGGER", "1").strip().lower() not in ("0", "false", "no")


def show_url_embeds_enabled() -> bool:
    """Return True only when ``CLORD_SHOW_URL_EMBEDS`` is explicitly truthy.

    Defaults to False (#372): URL OGP/link-preview cards in Claude's replies
    are suppressed (``suppress_embeds=True`` on each send) so a link doesn't
    expand into a tall preview card. Set ``CLORD_SHOW_URL_EMBEDS=1/true/yes/on``
    to restore Discord's default link-preview expansion.
    """
    return os.getenv("CLORD_SHOW_URL_EMBEDS", "false").strip().lower() in ("1", "true", "yes", "on")


def turn_progress_enabled() -> bool:
    """Return True unless ``CLORD_TURN_PROGRESS`` is explicitly ``0/false/no``.

    Defaults to True (#539): a long turn showing nothing at all is the failure
    this fixes, so it has to be on without the consumer wiring anything up.
    """
    return os.getenv("CLORD_TURN_PROGRESS", "1").strip().lower() not in ("0", "false", "no")


def turn_progress_quiet_seconds() -> float:
    """Seconds of silence before the progress line appears (#539).

    Defaults to 90. Measured on production (147 gaps, 2026-08-26): a 60s
    threshold would fire on 34% of gaps and compete with Claude's own
    narration, which already lands every ~39s (median); 90s targets the
    ~15-20% tail that is actually painful.
    """
    raw = os.getenv("CLORD_TURN_PROGRESS_QUIET_SECONDS", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_QUIET_SECONDS
    return value if value > 0 else DEFAULT_QUIET_SECONDS


def idle_flush_seconds() -> float:
    """Return the idle-flush window from ``CLORD_MIRROR_IDLE_FLUSH_SECONDS``.

    In ``minimal`` mode the final assistant text is normally flushed (as a
    pinging reply) when a turn-end marker is seen.  Current Claude Code builds
    no longer emit ``result`` and do not reliably emit ``system/turn_duration``
    (Issue #218), so this idle window is a marker-agnostic safety net: when a
    pending final answer is held and no new JSONL event arrives within this many
    seconds, it is flushed as the final reply.  Defaults to ``8.0``.
    """
    raw = os.getenv("CLORD_MIRROR_IDLE_FLUSH_SECONDS", "8").strip()
    try:
        return float(raw)
    except ValueError:
        return 8.0


# Module-level alias so ``TranscriptMirror.__init__`` can read the env default
# without the ``idle_flush_seconds`` constructor parameter shadowing the helper.
idle_flush_seconds_env = idle_flush_seconds


def _null_progress() -> TurnProgress:
    """A TurnProgress whose sinks do nothing — used when no Cog wired one up."""

    async def _post(text: str) -> None:
        return None

    async def _edit(handle: object, text: str) -> None:
        return None

    async def _delete(handle: object) -> None:
        return None

    return TurnProgress(post=_post, edit=_edit, delete=_delete)


def _is_turn_end(event: dict) -> bool:
    """Return True for JSONL events that signal the end of a Claude turn.

    Handles both ``{"type": "result"}`` (older Claude Code builds) and
    ``{"type": "system", "subtype": "turn_duration"}`` (current production).
    """
    t = event.get("type")
    return t == "result" or (t == "system" and event.get("subtype") == "turn_duration")


_KIND_PREFIX = {
    "assistant_text": "",
    "tool_use": "",  # tool_use bodies already start with the 🔧 emoji
    "tool_result": "↳ ",
    "user_input": "👤 ",
}


def _format_body(rendered: RenderedEvent) -> str:
    prefix = _KIND_PREFIX.get(rendered.kind, "")
    return f"{prefix}{rendered.body}"


class TranscriptMirror:
    """Tail one project's jsonl and forward rendered events to ``sink``."""

    def __init__(
        self,
        *,
        thread_id: int,
        project_dir: Path,
        sink: Sink,
        reply_sink: Sink | None = None,
        file_sink: FileSink | None = None,
        reply_cursor_sink: Sink | None = None,
        verbosity: str = "minimal",
        poll_interval: float = 0.5,
        idle_flush_seconds: float | None = None,
        ask_bridge_cb: AskBridgeCb | None = None,
        progress: TurnProgress | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.project_dir = project_dir
        self._sink = sink
        # #539: fills long silences with one self-updating line. Defaults to an
        # inert instance so the loop below never has to None-check it; the Cog
        # supplies a real one, so consumers get the feature by upgrading alone.
        self._progress = progress if progress is not None else _null_progress()
        self._reply_sink = reply_sink
        self._file_sink = file_sink
        # #232: bridges an AskUserQuestion menu (detected in the transcript) to
        # Discord buttons even when no run_claude poll loop is active.
        self._ask_bridge_cb = ask_bridge_cb
        self._ask_bridge_task: asyncio.Task[None] | None = None
        # Issue #215: called with the uuid of the last assistant_text of each
        # completed turn, so a restart can tell whether the final answer was
        # already delivered and avoid re-posting it.
        self._reply_cursor_sink = reply_cursor_sink
        self._verbosity = verbosity
        self._poll_interval = poll_interval
        self._idle_flush_seconds = (
            idle_flush_seconds if idle_flush_seconds is not None else idle_flush_seconds_env()
        )
        self._task: asyncio.Task[None] | None = None

    def note_turn_started(self) -> None:
        """Tell the progress line a turn just began (#539).

        Called when c-lord *accepts* the prompt, which is earlier and more honest
        than the first transcript event: Claude's startup happens in between, and
        the reader has been waiting for all of it.
        """
        self._progress.begin_turn(restart=True)

    def start(self) -> None:
        """Spawn the tail task.  Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name=f"transcript-mirror-{self.thread_id}")

    async def stop(self) -> None:
        """Cancel the tail task and wait for it to settle.  Safe to call repeatedly."""
        task = self._task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self._task = None
        with contextlib.suppress(Exception):
            await self._progress.end_turn()
        await self._cancel_ask_bridge()

    async def _cancel_ask_bridge(self) -> None:
        t = self._ask_bridge_task
        if t is None:
            return
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
        self._ask_bridge_task = None

    async def _maybe_bridge_ask(self, event: dict) -> None:
        """Bridge an AskUserQuestion menu found in *event* to Discord buttons (#232).

        Runs regardless of bridge-trigger source (human / task-notification /
        autonomous). Dedups against the run_claude poll-loop bridge via
        ``ask_bus.is_active`` (whoever registers first owns the menu) and against
        itself via the pending task guard. The bridge is spawned as a background
        task because it awaits the user's click for up to 24h — awaiting inline
        would freeze transcript tailing.

        #262: ``ask_bus.is_active`` is only a point-in-time check. The live bridge
        releases ``ask_bus`` the instant it gets an answer, so a mirror that tails
        the ``tool_use`` line late (queue lag / restart replay) would see
        ``is_active=False`` and re-bridge an already-answered menu — a dead,
        duplicate set of buttons posted after the answer. Guard against that by
        skipping when the transcript already contains the menu's ``tool_result``.
        """
        if self._ask_bridge_cb is None:
            return
        if self._ask_bridge_task is not None and not self._ask_bridge_task.done():
            return
        if ask_bus.is_active(self.thread_id):
            return
        question = _first_ask_question(event)
        if question is None:
            return
        tool_use_id = _first_ask_tool_use_id(event)
        if tool_use_id is not None and await asyncio.to_thread(
            _transcript_has_ask_result, self.project_dir, tool_use_id
        ):
            logger.debug(
                "TranscriptMirror: skipping already-answered AskUserQuestion "
                "thread=%d tool_use_id=%s",
                self.thread_id,
                tool_use_id,
            )
            return
        logger.info(
            "TranscriptMirror: bridging post-turn AskUserQuestion thread=%d header=%r",
            self.thread_id,
            question.header,
        )
        self._ask_bridge_task = asyncio.create_task(
            self._ask_bridge_cb(question), name=f"mirror-ask-bridge-{self.thread_id}"
        )

    async def _run(self) -> None:
        logger.info(
            "TranscriptMirror starting: thread=%d project_dir=%s verbosity=%s",
            self.thread_id,
            self.project_dir,
            self._verbosity,
        )
        # Buffer for tool_use / tool_result lines in minimal mode.
        progress_buf: list[str] = []
        # Pending assistant_text held until we know if it's intermediate or final.
        # When a subsequent event arrives we can decide: another assistant_text or
        # tool event → flush silently; result/user_input/stop → flush as reply.
        _pending_text: str | None = None
        _pending_progress: list[str] = []  # snapshot of progress_buf at capture time
        # uuid of the text currently held in _pending_text — not yet known to be
        # the turn's final answer.
        _pending_uuid: str | None = None
        # Issue #215: uuid of the last text actually DELIVERED as a final answer.
        # Committed at each turn boundary so a restart knows it was delivered.
        #
        # #553: this used to be "the most recent assistant_text of the turn",
        # which is a different thing. An intermediate message (text followed by a
        # tool call) is posted silently and the turn continues, but its uuid
        # stayed here — so a shutdown mid-turn committed a cursor pointing PAST
        # the last completed turn's final answer, and the restart rescue then
        # mistook that answer for a dropped one and re-posted it. The cursor must
        # only ever advance on a real final-answer delivery.
        _delivered_uuid: str | None = None

        async def _commit_cursor() -> None:
            nonlocal _delivered_uuid
            if self._reply_cursor_sink is not None and _delivered_uuid:
                try:
                    await self._reply_cursor_sink(_delivered_uuid)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "TranscriptMirror cursor sink failed for thread=%d",
                        self.thread_id,
                        exc_info=True,
                    )
            _delivered_uuid = None

        async def _flush_pending_silently() -> None:
            nonlocal _pending_text, _pending_progress, _pending_uuid
            if _pending_text is None:
                return
            await self._try_sink(_pending_text)
            # #399: an intermediate text posted silently may be the prose above
            # a not-yet-bridged menu (the plan path flushes it BEFORE the menu).
            # Register it as source="mirror" so the later pane-bridge skips its
            # own duplicate (order-independent dedup — see bridged_context).
            bridged_context.register(self.thread_id, _pending_text, source="mirror")
            # Merge the snapshot back so subsequent tool output accumulates.
            progress_buf[:0] = _pending_progress
            _pending_text = None
            _pending_progress = []
            # #553: an intermediate message is NOT a final answer, so it must not
            # move the delivery cursor.
            _pending_uuid = None

        async def _flush_pending_as_reply() -> None:
            nonlocal _pending_text, _pending_progress, _pending_uuid, _delivered_uuid
            if _pending_text is None:
                return
            await self._flush_as_reply(_pending_text, _pending_progress)
            # This IS the final answer for the turn — the one delivery that may
            # advance the cursor (#553).
            if _pending_uuid:
                _delivered_uuid = _pending_uuid
            _pending_text = None
            _pending_progress = []
            _pending_uuid = None

        # Drive the tail through a queue so the consumer can apply an idle
        # timeout (Issue #218) without cancelling the tail generator: a bare
        # ``async for`` blocks indefinitely between events, leaving no chance to
        # flush a pending final answer when no turn-end marker is emitted.
        queue: asyncio.Queue = asyncio.Queue()

        async def _producer() -> None:
            async for event in tail_events(self.project_dir, poll_interval=self._poll_interval):
                await queue.put(event)

        producer = asyncio.create_task(_producer(), name=f"transcript-tail-{self.thread_id}")
        idle_timeout = (
            self._idle_flush_seconds
            if self._idle_flush_seconds and self._idle_flush_seconds > 0
            else None
        )
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
                # asyncio.TimeoutError is a distinct class from builtin
                # TimeoutError on Python 3.10 (merged only in 3.11); the project
                # supports 3.10, so the aliased form is required for correctness.
                except asyncio.TimeoutError:  # noqa: UP041
                    # #539: the idle window is also the progress line's heartbeat.
                    # It is finer than the line's refresh interval, so no separate
                    # timer task (with a lifetime to keep in sync) is needed.
                    await self._progress.tick()
                    # Idle: no new JSONL event within the window. Flush any held
                    # final answer as a pinging reply — independent of whether a
                    # ``result`` / ``turn_duration`` marker was ever written.
                    if self._verbosity == "minimal" and _pending_text is not None:
                        logger.info(
                            "TranscriptMirror idle-flush: thread=%d "
                            "(final answer with no turn-end marker within %.1fs)",
                            self.thread_id,
                            idle_timeout,
                        )
                        await _flush_pending_as_reply()
                        # Record delivery (Issue #215) so a restart does not
                        # re-post this idle-flushed final answer.
                        await _commit_cursor()
                    continue

                # #232: bridge an open AskUserQuestion menu regardless of
                # verbosity / turn-end / who triggered the turn.
                await self._maybe_bridge_ask(event)

                if self._verbosity == "minimal" and _is_turn_end(event):
                    # #539: the turn is over — take the progress line away before
                    # the final answer lands so it never trails below the answer.
                    await self._progress.end_turn()
                    # Turn boundary: flush pending as the final reply.
                    await _flush_pending_as_reply()
                    await _commit_cursor()
                    # #399: disarm unconsumed pane-bridge entries — the
                    # legitimate flush always precedes its turn end, so a
                    # surviving entry could only swallow a future real message.
                    bridged_context.clear_thread(self.thread_id)
                    continue

                rendered = render_event(event)

                # #682: the other half of the ZWSP echo test. A menu answer is
                # typed with ``send_literal``, which leaves the marker off on
                # purpose (#172/#650), so the formatter cannot tell this event
                # from human pane input — but c-lord recorded what it typed, so
                # ask. Dropped exactly like a marked echo (no turn bookkeeping):
                # Discord already has the sentence the user wrote.
                if (
                    rendered is not None
                    and rendered.kind == "user_input"
                    and pane_echo.consume_match(self.thread_id, rendered.body)
                ):
                    logger.info(
                        "TranscriptMirror: suppressed unmarked c-lord pane echo thread=%d",
                        self.thread_id,
                    )
                    rendered = None

                # #539: record this event's activity BEFORE ticking, so the line
                # reflects what we just read rather than the state before it —
                # otherwise the tick that fires on a fresh tool event still
                # renders the previous (stale) "nothing has moved" state.
                # Tool traffic is the evidence a silent turn is alive: it keeps
                # arriving here while nothing reaches Discord, which is exactly
                # the gap being filled. Arming here (not only on user_input) also
                # covers turns started outside Discord (scheduler / webhook / the
                # tmux pane).
                if rendered is not None and rendered.kind in _BUFFERED_KINDS:
                    self._progress.begin_turn()
                    self._progress.note_activity(
                        rendered.body if rendered.kind == "tool_use" else None
                    )
                await self._progress.tick()

                if rendered is None:
                    continue

                if self._verbosity == "minimal":
                    if rendered.kind in _BUFFERED_KINDS:
                        # Tool event: if there's pending text, it was intermediate.
                        await _flush_pending_silently()
                        progress_buf.append(_format_body(rendered))
                    elif rendered.kind == "assistant_text":
                        body = _format_body(rendered)
                        # #399 AC3: the CLI flushes the prose preceding an
                        # AskUserQuestion/plan menu only after the menu
                        # resolves; if the pane-ask bridge already delivered
                        # it as the menu's context message, re-posting it here
                        # would duplicate it. Still record its uuid (#215) so
                        # a restart does not re-post it as a missed final.
                        bridged = bridged_context.take_match(self.thread_id, body, source="pane")
                        if bridged is not None:
                            await _flush_pending_silently()
                            # #686: what the pane could deliver is the TUI
                            # *rendering* — box-drawn tables, hard wraps, no
                            # markdown — because while the menu was open the
                            # jsonl held nothing to read. THIS is the readable
                            # version of the same words, and dropping it left
                            # the thread with only the unreadable one. Rewrite
                            # the messages already posted instead; on any
                            # failure they stay exactly as they are.
                            replaced = False
                            if bridged.messages:
                                replaced = await replace_pane_context(bridged.messages, body)
                            # The pane bridge already delivered this text, so it
                            # counts as delivered for cursor purposes (#215).
                            _delivered_uuid = event.get("uuid") or _delivered_uuid
                            logger.info(
                                "TranscriptMirror: suppressed pane-bridged ask context "
                                "thread=%d (markdown replacement: %s)",
                                self.thread_id,
                                "applied" if replaced else "not applied",
                            )
                            # Commit immediately: the text IS delivered, and a
                            # turn-end marker may never arrive — without this a
                            # hard-killed bot would re-post it on restart (#215).
                            await _commit_cursor()
                            continue
                        # Another text while one is pending → previous was intermediate.
                        if _pending_text is not None:
                            await _flush_pending_silently()
                        _pending_text = body
                        _pending_progress = list(progress_buf)
                        _pending_uuid = event.get("uuid")
                        progress_buf.clear()
                    elif rendered.kind == "user_input":
                        # #539: a new prompt both closes the previous turn's line
                        # and arms the next one.
                        await self._progress.end_turn()
                        self._progress.begin_turn()
                        # Human turn: previous assistant turn is over → flush as reply.
                        await _flush_pending_as_reply()
                        await _commit_cursor()
                        bridged_context.clear_thread(self.thread_id)  # #399 (see turn_end)
                        await self._post(rendered)
                    else:
                        await _flush_pending_silently()
                        await self._post(rendered)
                else:
                    await self._post(rendered)

        except asyncio.CancelledError:
            pass
        finally:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer
            await self._cancel_ask_bridge()
            if self._verbosity == "minimal":
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await _flush_pending_as_reply()
                    await _commit_cursor()
            logger.info("TranscriptMirror stopped: thread=%d", self.thread_id)

    async def _flush_as_reply(self, text: str, progress: list[str]) -> None:
        """Flush pending text as the final reply for the current turn."""
        # #539: this IS the end of a turn from the reader's point of view. Turn-end
        # markers are not reliably emitted (#218), so disarming here rather than
        # only on the marker is what keeps a finished thread from carrying a line.
        await self._progress.end_turn()
        if progress and self._file_sink is not None:
            await self._flush_with_progress(text, progress)
        elif self._reply_sink is not None:
            await self._try_reply_sink(text)
        else:
            await self._try_sink(text)

    async def _flush_with_progress(self, body: str, progress_buf: list[str]) -> None:
        """Write buffered tool lines to a tempfile and call file_sink."""
        await self._progress.note_output()
        assert self._file_sink is not None
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                prefix=f"clord_progress_{self.thread_id}_",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(_truncate_progress("\n".join(progress_buf)))
                tmp_path = f.name
            try:
                await self._file_sink(body, tmp_path)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "TranscriptMirror file_sink failed for thread=%d",
                    self.thread_id,
                    exc_info=True,
                )
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    async def _post(self, rendered: RenderedEvent) -> None:
        """Format and send a rendered event via plain sink."""
        await self._try_sink(_format_body(rendered))

    async def _try_sink(self, body: str) -> None:
        # #539: something a reader can see is reaching the thread, so the filler
        # must step aside — otherwise it would sit *below* the output it stood in
        # for. Done before the send so the ordering holds even if the send is slow.
        await self._progress.note_output()
        try:
            await self._sink(body)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "TranscriptMirror sink failed for thread=%d",
                self.thread_id,
                exc_info=True,
            )

    async def _try_reply_sink(self, body: str) -> None:
        await self._progress.note_output()
        assert self._reply_sink is not None
        try:
            await self._reply_sink(body)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "TranscriptMirror reply_sink failed for thread=%d",
                self.thread_id,
                exc_info=True,
            )
