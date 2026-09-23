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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..claude.types import AskQuestion, UsageLimit, _parse_ask_questions
from ..discord_ui.ask_bus import ask_bus
from ..discord_ui.bridged_context import bridged_context
from ..discord_ui.pane_context import replace_pane_context
from ..discord_ui.turn_progress import DEFAULT_QUIET_SECONDS, TurnProgress
from ..turn_end_bus import turn_end_bus
from ..usage_limit import (
    banner_only,
    folded_notice,
    is_rate_limit_event,
    is_refusal_shaped,
    usage_limit_notices,
)
from .formatter import RenderedEvent, render_event
from .pane_echo import pane_echo
from .repeat_fold import RepeatFold
from .tail import UNRESOLVED_NOTICE_SECONDS, UnresolvedTranscript, tail_events

logger = logging.getLogger(__name__)

Sink = Callable[[str], Awaitable[None]]
FileSink = Callable[[str, str], Awaitable[None]]
# Called with the first AskUserQuestion of a tool_use to bridge it to Discord
# buttons (#232). Constructed by TranscriptMirrorCog (knows tmux + thread).
AskBridgeCb = Callable[[AskQuestion], Coroutine[object, object, None]]


@dataclass(frozen=True)
class UserFileRequest:
    """One ``SendUserFile`` call: the files Claude meant to hand the reader (#233).

    ``tool_use_id`` is the transcript's own id for the call and is what keeps a
    re-read of the same line from posting the files twice.
    """

    tool_use_id: str | None
    paths: list[str]
    caption: str | None


UserFileSink = Callable[[UserFileRequest], Awaitable[None]]
# #747: posts the repeat counter and returns a handle to edit it with later.
FoldPost = Callable[[str], Awaitable[object | None]]
FoldEdit = Callable[[object, str], Awaitable[None]]

# The harness tool whose "1 file delivered to user." goes to the harness's own
# delivery channel — not to Discord (#233).
_SEND_USER_FILE_TOOL = "SendUserFile"


def _user_file_requests(event: dict) -> list[UserFileRequest]:
    """Extract every ``SendUserFile`` call from one raw transcript event (#233).

    Returns an empty list for anything else, including a call whose ``input``
    has a shape we did not expect — a malformed tool call must not be able to
    stop the mirror, and there is nothing to deliver from one anyway.  This runs
    on *every* event the tail yields, so it never assumes a shape: an exception
    here would kill the tail task, taking the whole thread's mirror with it.
    """
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    requests: list[UserFileRequest] = []
    for block in content:
        if (
            not isinstance(block, dict)
            or block.get("type") != "tool_use"
            or block.get("name") != _SEND_USER_FILE_TOOL
        ):
            continue
        inp = block.get("input")
        if not isinstance(inp, dict):
            continue
        raw_files = inp.get("files")
        if not isinstance(raw_files, list):
            continue
        paths = [p.strip() for p in raw_files if isinstance(p, str) and p.strip()]
        if not paths:
            continue
        caption = inp.get("caption")
        tool_use_id = block.get("id")
        requests.append(
            UserFileRequest(
                tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
                paths=paths,
                caption=caption.strip() if isinstance(caption, str) and caption.strip() else None,
            )
        )
    return requests


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

# Tools that wait on the *reader*, not on a process. Their ``tool_result`` only
# lands once someone answers the menu, so counting one as "running" would keep
# the progress line on 作業中 under a menu nobody has touched (#757).
_WAITS_ON_READER = frozenset({"AskUserQuestion", "ExitPlanMode"})


def _tool_call_ids(event: dict) -> tuple[list[str], list[str]]:
    """Return the tool-call ids *event* opens and closes (#757).

    Read from the raw event, not the rendering: a call that printed nothing
    (``sleep 150``) renders to ``None``, and missing its ``tool_result`` would
    leave the call looking open for the rest of the turn. Like every helper run
    on each tailed event, it never assumes a shape.
    """
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return [], []
    opened: list[str] = []
    closed: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "tool_use" and block.get("name") not in _WAITS_ON_READER:
            tool_id = block.get("id")
            if isinstance(tool_id, str):
                opened.append(tool_id)
        elif kind == "tool_result":
            tool_id = block.get("tool_use_id")
            if isinstance(tool_id, str):
                closed.append(tool_id)
    return opened, closed


# #631 AC8: the pane reader and this mirror witness the same limit, and the
# reader's ⏳ embed is the message that should survive — it names the scope, the
# reset time and what the reader can do about it.  Which one reaches Discord
# first, though, is a race, and folding the banner *won* that race: on staging
# (2026-09-14) the folded line beat the embed by 108 ms and the thread got both,
# which is exactly the duplication AC8 exists to remove.  So the fold waits this
# long and asks again.  The reader breaks its poll loop within a poll or two of
# the banner (0.5 s interval), so this is generous; and because the turn is
# stalled by definition, the delay costs the reader nothing.
USAGE_LIMIT_GRACE_SECONDS = 3.0

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


def send_user_file_enabled() -> bool:
    """Return True unless ``CLORD_SEND_USER_FILE`` is explicitly ``0/false/no``.

    Defaults to True (#233): before this, every ``SendUserFile`` call was
    dropped in silence while the tool told the session it had succeeded — the
    kind of breakage a consumer cannot even see, let alone opt into fixing.
    """
    return os.getenv("CLORD_SEND_USER_FILE", "1").strip().lower() not in ("0", "false", "no")


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


def _is_user_prompt(event: dict) -> bool:
    """Return True for "Claude read an instruction" — the start of a turn (#583).

    A ``user`` event whose content is a plain string is an instruction: c-lord's
    own (which carries the zero-width-space marker and is never rendered — see
    :func:`c_lord.transcript.formatter._render_user`), or one typed straight
    into the pane.  Tool results are ``user`` events too, but their content is a
    list of blocks, and counting one as the start of a turn would hand this turn
    the ending of the turn it displaced.

    Sidechains are a subagent's own conversation, not this thread's turn.
    """
    if event.get("type") != "user" or event.get("isMeta") or event.get("isSidechain"):
        return False
    content = (event.get("message") or {}).get("content")
    return isinstance(content, str) and bool(content.strip())


def _event_time(event: dict) -> datetime | None:
    """The event's own ``timestamp`` as a datetime, or None if unusable.

    #583 compares the marker against the moment c-lord delivered its prompt, so
    the *event's* time is what matters — not when the tail got around to
    reading it.  Anything unparseable degrades to None, which the bus dates at
    read time.
    """
    raw = event.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # Claude Code writes ``2026-09-08T06:21:54.551Z``; fromisoformat only
        # learned to read the ``Z`` in 3.11.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


_KIND_PREFIX = {
    "assistant_text": "",
    "tool_use": "",  # tool_use bodies already start with the 🔧 emoji
    "tool_result": "↳ ",
    "user_input": "👤 ",
}


def _format_body(rendered: RenderedEvent) -> str:
    prefix = _KIND_PREFIX.get(rendered.kind, "")
    return f"{prefix}{rendered.body}"


def _unresolved_notice(report: UnresolvedTranscript) -> str:
    """What to tell the thread when its transcript cannot be found (#773).

    The two cases need different advice, and getting that wrong wastes the
    reader's time:

    * **c-lord named this session** (a claim exists) — the transcript should be
      there.  Restarting Claude re-names it, and ``/claude-restart`` keeps the
      conversation.
    * **c-lord never named it** — the session predates #773 (it was started by
      an older c-lord, or it is still running from before the upgrade).  A
      ``/claude-restart`` will **not** help: it resumes with ``--continue``,
      which reuses the very transcript nothing can recognise and names nothing.
      Only a new session gets a name, and that is ``/clear``.  The workspace —
      the checkout, the branch, the files — is untouched; the conversation is
      what does not carry over, and saying so is the honest trade.
    """
    if report.candidates == 0:
        detail = "このワークスペースには transcript がまだ 1 つもありません。"
    else:
        detail = (
            f"transcript は {report.candidates} 本ありますが、"
            "どれもこのスレッドのセッションのものと確認できません。"
        )
    if report.claimed_session_id:
        recovery = (
            "`/claude-restart` で Claude を立て直してください（会話の文脈は引き継がれます）。"
        )
    else:
        recovery = (
            "このセッションは c-lord がセッションに名前を付けるようになる前"
            "（#773 以前）に起動したものです。`/claude-restart` では直りません"
            "（`--continue` は名前の無いセッションをそのまま開き直すため）。"
            "`/clear` で新しいセッションを始めてください — 作業ディレクトリ"
            "（チェックアウト・ブランチ・ファイル）はそのままで、会話の文脈だけが"
            "引き継がれません。"
        )
    return (
        "⚠️ このスレッドの transcript が見つからないため、Claude の返事を "
        f"Discord に転送できていません（{report.seconds:.0f} 秒間）。\n"
        f"{detail}\n"
        f"{recovery}"
    )


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
        user_file_sink: UserFileSink | None = None,
        reply_cursor_sink: Sink | None = None,
        verbosity: str = "minimal",
        poll_interval: float = 0.5,
        idle_flush_seconds: float | None = None,
        usage_limit_grace: float = USAGE_LIMIT_GRACE_SECONDS,
        ask_bridge_cb: AskBridgeCb | None = None,
        progress: TurnProgress | None = None,
        fold_post: FoldPost | None = None,
        fold_edit: FoldEdit | None = None,
        expect_turn: bool = False,
        unresolved_after: float = UNRESOLVED_NOTICE_SECONDS,
    ) -> None:
        self.thread_id = thread_id
        self.project_dir = project_dir
        self._sink = sink
        # #539: fills long silences with one self-updating line. Defaults to an
        # inert instance so the loop below never has to None-check it; the Cog
        # supplies a real one, so consumers get the feature by upgrading alone.
        self._progress = progress if progress is not None else _null_progress()
        # #747: one message stands in for a loop's copies of the same lines.
        # Without an editor (older consumers, tests) the counter goes out through
        # the plain sink and simply cannot show a number — it still stops the flood.
        self._fold = RepeatFold(
            thread_id=thread_id,
            post=self._fold_poster(sink, fold_post),
            edit=fold_edit if fold_post is not None else None,
        )
        self._reply_sink = reply_sink
        self._file_sink = file_sink
        # #233: delivers the files of a SendUserFile call. Optional so a mirror
        # built without one (older consumers, tests) keeps working unchanged.
        self._user_file_sink = user_file_sink
        # tool_use ids already delivered — a transcript line re-read after a
        # rewrite (#433) must not attach the same files a second time.
        self._delivered_user_files: set[str] = set()
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
        # #631 AC8: how long to let c-lord's own ⏳ embed win the race before
        # folding the banner ourselves.  A knob only so tests need not sleep.
        self._usage_limit_grace = usage_limit_grace
        self._task: asyncio.Task[None] | None = None
        # #773: is somebody waiting on this mirror right now?  Only then is
        # "I cannot find this thread's transcript" news worth posting — a mirror
        # restored at startup for an idle workspace has nothing to read by
        # design, and ``on_ready`` restores one for every open session on the
        # host.
        self._turn_active = expect_turn
        self._unresolved_after = unresolved_after
        # One notice per turn: the tail keeps reporting for as long as the
        # outage lasts, which is what lets the *next* turn be told too.
        self._unresolved_told = False

    def note_turn_started(self) -> None:
        """Tell the progress line a turn just began (#539).

        Called when c-lord *accepts* the prompt, which is earlier and more honest
        than the first transcript event: Claude's startup happens in between, and
        the reader has been waiting for all of it.
        """
        self._progress.begin_turn(restart=True)
        # #773: from here on, silence is a symptom rather than an idle thread.
        self._turn_active = True
        self._unresolved_told = False

    async def _on_unresolved(self, report: UnresolvedTranscript) -> None:
        """Say out loud that this thread's transcript cannot be found (#773/#585).

        The jsonl mirror is the only delivery path there is (#712), so a mirror
        that resolves nothing is a thread that receives nothing — and #627 made
        that failure *silent* on purpose, to avoid the worse failure of posting
        a stranger's conversation.  Silence is still the right thing to post; it
        is the wrong thing to *say*, which is why this exists.  In #773 the
        fleet was mute for three days and the only trace was one log line per
        mirror.
        """
        logger.error(
            "TranscriptMirror: nothing to read for thread=%d in %s after %.0fs "
            "(%d jsonl file(s) present, claimed session id %s) — Claude's replies "
            "cannot reach this thread (#773)",
            self.thread_id,
            report.project_dir,
            report.seconds,
            report.candidates,
            report.claimed_session_id or "none",
        )
        if not self._turn_active or self._unresolved_told:
            return
        self._unresolved_told = True
        await self._try_sink(_unresolved_notice(report))

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
        # #631 AC9: has this turn already reported the plan limit?  The CLI
        # retries internally and writes the same refusal again on each attempt —
        # six times in one turn on 2026-09-04 — and every copy says exactly the
        # same thing, so only the first is worth a message.
        _limit_reported = False

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
            # #747: a loop's copy is counted in one message instead of posted.
            if not await self._fold.offer(_pending_text):
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
            # #747: the answer is never folded, and it ends any loop before it.
            await self._fold.reset()
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
            async for event in tail_events(
                self.project_dir,
                poll_interval=self._poll_interval,
                on_unresolved=self._on_unresolved,
                unresolved_after=self._unresolved_after,
            ):
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
                    # #747: a loop that stalled still shows its latest count.
                    await self._fold.flush()
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

                # #583: the first half of "whose turn ended?" — Claude read an
                # instruction. The runner's poll loop needs it to tell its own
                # turn's ending from that of the turn its prompt displaced.
                if _is_user_prompt(event):
                    turn_end_bus.note_prompt(self.thread_id, at=_event_time(event))
                    _limit_reported = False
                    usage_limit_notices.clear_thread(self.thread_id)

                if _is_turn_end(event):
                    # #773: nobody is waiting on this mirror until the next turn.
                    self._turn_active = False
                    # #631: a limit reported for the turn that just ended says
                    # nothing about the next one — the limit may well have reset
                    # in between, and a thread that silently stops explaining why
                    # it is stuck is the bug this came from.  c-lord's own notice
                    # is scoped the same way: AC8 asks whether *this* turn has
                    # already been told, and leaving the entry standing made the
                    # next turn silent for the rest of the TTL (staging,
                    # 2026-09-14: announced 12:53:16, wrongly suppressed 12:54:35).
                    _limit_reported = False
                    usage_limit_notices.clear_thread(self.thread_id)
                    if self._verbosity == "minimal":
                        # #539: the turn is over — take the progress line away
                        # before the final answer lands so it never trails
                        # below the answer.
                        await self._progress.end_turn()
                        # #747: a loop never spans a turn boundary.
                        await self._fold.reset()
                        # Turn boundary: flush pending as the final reply.
                        await _flush_pending_as_reply()
                        await _commit_cursor()
                        # #399: disarm unconsumed pane-bridge entries — the
                        # legitimate flush always precedes its turn end, so a
                        # surviving entry could only swallow a future real
                        # message.
                        bridged_context.clear_thread(self.thread_id)
                    # #583: the pane cannot see a turn boundary, so the runner's
                    # poll loop learns it here — this is the only place in
                    # c-lord that reads Claude's own turn-end marker. Marked
                    # AFTER the flush above on purpose: the runner reacts by
                    # ending the turn (and posting the 📊 context footer), and
                    # that must never overtake the answer the footer belongs to.
                    if not event.get("isSidechain"):
                        turn_end_bus.mark(self.thread_id, at=_event_time(event))
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
                # #757: the ids tell a call that is still running (no result
                # yet) from a turn that has genuinely gone quiet.
                opened, closed = _tool_call_ids(event)
                is_tool = rendered is not None and rendered.kind in _BUFFERED_KINDS
                if is_tool or opened or closed:
                    self._progress.begin_turn()
                    self._progress.note_activity(
                        rendered.body
                        if rendered is not None and rendered.kind == "tool_use"
                        else None,
                        started=opened,
                        finished=closed,
                    )
                await self._progress.tick()

                if rendered is None:
                    continue

                # #631 AC7: Claude's rate-limit refusal is written to the
                # transcript as an ordinary assistant message, so the mirror used
                # to post it verbatim — a bare English line with no explanation
                # and no recovery time, six times over in the worst thread.  Fold
                # it into one Japanese line carrying the reset time instead.
                #
                # Deliberately NOT held as _pending_text: the refusal is not an
                # answer, and the CLI often retries past it (the turn then runs
                # anyway, as it did in six of the eight threads on 2026-09-04).
                # Letting it become the turn's final answer would ping the owner
                # with it and, worse, take the place of the answer that follows.
                # Any text already pending is left pending for the same reason:
                # a refusal arriving behind it is no proof it was intermediate,
                # and flushing it here would silently strip its ping.  The cost
                # is that the notice can precede it in the thread, which is only
                # an ordering wobble in a case the banner rarely takes (it is
                # normally the turn's first assistant event).
                if rendered.kind == "assistant_text":
                    limit = banner_only(rendered.body)
                    marked = is_rate_limit_event(event) and is_refusal_shaped(rendered.body)
                    if limit is not None or marked:
                        await self._report_usage_limit(limit, reported=_limit_reported)
                        _limit_reported = True
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
                            if replaced or not bridged.folded:
                                # The pane bridge already delivered this text, so
                                # it counts as delivered for cursor purposes
                                # (#215).
                                _delivered_uuid = event.get("uuid") or _delivered_uuid
                                logger.info(
                                    "TranscriptMirror: suppressed pane-bridged ask context "
                                    "thread=%d (markdown replacement: %s)",
                                    self.thread_id,
                                    "applied" if replaced else "not applied",
                                )
                                # Commit immediately: the text IS delivered, and
                                # a turn-end marker may never arrive — without
                                # this a hard-killed bot would re-post it on
                                # restart (#215).
                                await _commit_cursor()
                                continue
                            # #686: what is in the thread is a FOLD — a pointer
                            # saying the readable version is coming — and the
                            # markdown would not fit into it. Suppressing here
                            # would leave a pointer to nothing, so fall through
                            # and post it. Not a #680 duplicate: the pointer
                            # never held the prose.
                            logger.info(
                                "TranscriptMirror: pane-bridged ask context was folded and the "
                                "markdown did not fit it — posting the markdown instead of "
                                "suppressing it thread=%d (#686)",
                                self.thread_id,
                            )
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
                        await self._fold.reset()  # #747 (see turn_end)
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

                # #233: after the branch above on purpose. Claude narrates
                # ("下に貼ります") and *then* calls SendUserFile, so the prose
                # held in _pending_text has to reach the thread first —
                # otherwise the images land above the sentence introducing them.
                await self._deliver_user_files(event)

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
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._fold.reset()
            logger.info("TranscriptMirror stopped: thread=%d", self.thread_id)

    def _fold_poster(self, sink: Sink, fold_post: FoldPost | None) -> FoldPost:
        """How the #747 counter reaches the thread.

        A new message, like any other output, so the #539 filler steps aside
        for it first and never ends up sitting below it.
        """

        async def post(text: str) -> object | None:
            await self._progress.note_output()
            if fold_post is not None:
                return await fold_post(text)
            await sink(text)
            return None

        return post

    async def _report_usage_limit(self, limit: UsageLimit | None, *, reported: bool) -> None:
        """Say once, in Japanese, that Claude is waiting on a plan limit (#631).

        Says nothing at all when c-lord's own ⏳ notice already went out for this
        thread (AC8): that message is the richer one — it names the scope, the
        reset time and what the reader can actually do — and repeating it in
        different words reads as a second, separate thing having gone wrong.
        That is what the thread of 2026-09-04 showed, the English copy landing
        one second after the Japanese one.
        """
        if reported:
            logger.info(
                "TranscriptMirror: usage-limit banner already reported this turn thread=%d",
                self.thread_id,
            )
            return
        if usage_limit_notices.announced(self.thread_id):
            logger.info(
                "TranscriptMirror: suppressed usage-limit banner thread=%d "
                "(c-lord already posted its own notice)",
                self.thread_id,
            )
            return
        # Give the pane reader its head start (see USAGE_LIMIT_GRACE_SECONDS).
        # Inline rather than in a task: the tail keeps filling its queue while we
        # wait, so nothing is lost, and every later event still reaches Discord
        # behind this one.
        if self._usage_limit_grace > 0:
            await asyncio.sleep(self._usage_limit_grace)
            if usage_limit_notices.announced(self.thread_id):
                logger.info(
                    "TranscriptMirror: suppressed usage-limit banner thread=%d "
                    "(c-lord posted its own notice during the grace window)",
                    self.thread_id,
                )
                return
        logger.info(
            "TranscriptMirror: folded usage-limit banner thread=%d scope=%s",
            self.thread_id,
            limit.scope if limit is not None else "(unparsed)",
        )
        await self._try_sink(folded_notice(limit))

    async def _deliver_user_files(self, event: dict) -> None:
        """Hand every ``SendUserFile`` call in *event* to the sink (#233).

        Sidechain events are delivered too: a subagent that calls the tool means
        the same thing the main agent does — give this to the reader — and
        dropping it would be the very silence this fixes.
        """
        if self._user_file_sink is None:
            return
        for request in _user_file_requests(event):
            if request.tool_use_id is not None:
                if request.tool_use_id in self._delivered_user_files:
                    continue
                self._delivered_user_files.add(request.tool_use_id)
            logger.info(
                "TranscriptMirror: delivering %d SendUserFile attachment(s) thread=%d id=%s",
                len(request.paths),
                self.thread_id,
                request.tool_use_id,
            )
            await self._progress.note_output()
            try:
                await self._user_file_sink(request)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "TranscriptMirror user_file_sink failed for thread=%d id=%s",
                    self.thread_id,
                    request.tool_use_id,
                    exc_info=True,
                )

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
