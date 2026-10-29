"""Tell the outside world a turn is over (#917).

Someone who dispatches work to c-lord by webhook — an agent, CI, a dashboard —
has no way to learn that the turn finished except reading the thread and
guessing. Measured on 98 dispatches (2026-09-28..10-14): 18 minutes median, 62
at worst, and three finished turns the requester was never told about. c-lord
already knows (:mod:`c_lord.notify_policy` classifies every turn end); this
module is the outlet.

One JSON event per turn end goes to up to two places, **both off by default and
configured only on the server** — never from a prompt or a Discord message, so
nobody who can talk to the bot can aim it at an address (SSRF) or read the
events:

``CLORD_EVENT_LOG=<path>``
    One event per line appended to a JSONL file. ``tail -F`` it.
``CLORD_EVENT_WEBHOOK_URL=<url>`` + ``CLORD_EVENT_WEBHOOK_SECRET``
    POST the same JSON, with ``X-Clord-Signature: sha256=<hex HMAC of the body>``.
    Without a secret nothing is sent: an unsigned POST cannot be told from a
    forgery. Failures are logged and retried a few times; they never reach the
    turn or the thread.

Why not Claude Code's Stop hook: it fires before the last reply is posted to
Discord, so a receiver that goes to read the thread finds the turn "not over
yet". The event here is sent **after** the final answer has been posted.

Kinds are :data:`c_lord.notify_policy.Kind`: ``completion`` (the turn ended),
``blocked`` (it is parked on a menu someone has to answer — sent when the menu is
posted, the turn's ``completion`` follows later) and ``failure`` (it never ran,
crashed, timed out, was stopped, hit a plan limit…). ``reason`` says which, as a
short code — never error text, which can hold paths.

**Background sub-agents.** When Claude's turn ends while background agents are
still running, Claude Code starts another turn by itself as each one reports
back. Announcing ``completion`` at the first end would tell the requester "done"
minutes before the answer exists. The transcript mirror counts the agents
launched and not yet reported (:func:`note_turn_end`); when the turn that just
ended left some running, the ``completion`` is held until a later turn end finds
none. Limits: the count comes from the transcript, so a bot restart re-reads it
(the restored mirror reads the transcript back — #1012), a turn-end marker the
transcript does not carry (older Claude Code builds) cannot be read at all (the
completion goes out at once), and only agents are counted, not background shell
commands. An agent that never reports back cannot hold the event forever:
``CLORD_EVENT_DEFER_MAX_SECONDS`` (default 3600) releases it with
``reason: "background_agents_unresolved"``.

**Turns Claude starts by itself** (#961). When a background *shell command*
(CI, a long pytest) finishes, Claude Code starts a turn on its own, just as for
an agent — but nothing above counts those, and the runner that announces a turn
is long gone: it ended at Claude's first turn end ("waiting for CI"). That later
turn often carries the real final report. The mirror tells this module when a
``<task-notification>`` opens a turn (:func:`note_self_turn_started`) and whether
the turn that just ended was one (``note_turn_end(..., self_started=True)``);
its end goes out as a ``completion`` with ``started_by: {"type": "self"}`` and
``reason: "self_resumed_turn"``, one per such turn. It is not sent when a runner
turn is open (a person's message landed in it — the runner announces that end)
or when a held completion is released instead (the agent case above: that turn
*is* the held turn's end). guild / channel come from the thread's last runner
turn — ``null`` after a restart. Limit: a self-started turn that began before a
restart has no recorded start, so its end is not announced.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .notify_policy import Kind
from .utils.logger import log_ctx

if TYPE_CHECKING:
    import discord

logger = logging.getLogger(__name__)

# ``datetime.UTC`` is 3.11+ and c-lord supports 3.10.
_UTC = timezone.utc  # noqa: UP017

EVENT_VERSION = 1

ENV_LOG = "CLORD_EVENT_LOG"
ENV_URL = "CLORD_EVENT_WEBHOOK_URL"
ENV_SECRET = "CLORD_EVENT_WEBHOOK_SECRET"
ENV_DEFER_MAX = "CLORD_EVENT_DEFER_MAX_SECONDS"

SIGNATURE_HEADER = "X-Clord-Signature"
DEFAULT_DEFER_MAX_SECONDS = 3600.0

#: Pauses before each retry of a failed POST — so ``1 + len`` attempts in all.
_RETRY_DELAYS: tuple[float, ...] = (1.0, 5.0, 30.0)
_HTTP_TIMEOUT_SECONDS = 10.0
#: How often to ask the mirror whether the final answer is out yet, and how long
#: to wait for it. The idle flush is 8s by default; this outlasts it.
_SETTLE_POLL = 0.25
_SETTLE_MAX_SECONDS = 15.0
#: Bound on the per-thread snapshot map, as in :mod:`c_lord.turn_end_bus`.
_MAX_THREADS = 2048


@dataclass(frozen=True)
class StartedBy:
    """Who or what started a turn: ``user`` / ``webhook`` / ``scheduler`` / ``api``."""

    type: str
    id: str | None = None
    name: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"type": self.type, "id": self.id, "name": self.name}


@dataclass
class TurnInfo:
    """What is known about a turn from its start; the event is built from it."""

    thread_id: int
    guild_id: int | None
    channel_id: int | None
    started_by: StartedBy
    started_at: datetime
    started_mono: float
    #: id of the thread's last reply before this turn, so "the last reply" can
    #: be told from one an earlier turn posted without comparing clocks.
    baseline_reply_id: int


@dataclass
class _Held:
    """A ``completion`` waiting for background agents to report back."""

    info: TurnInfo
    timer: asyncio.TimerHandle | None = None
    #: the ``reason`` to send when the agents report back (#961).
    reason: str | None = None


_ACTIVE: dict[int, TurnInfo] = {}
#: thread -> (monotonic time of the last transcript turn end, agents still running)
_LAST_END: dict[int, tuple[float, int]] = {}
_HELD: dict[int, _Held] = {}
#: thread -> the last runner turn, whose guild / channel a self-started turn inherits.
_LAST_INFO: dict[int, TurnInfo] = {}
#: thread -> the open turn Claude started by itself on a notification (#961).
_SELF: dict[int, TurnInfo] = {}
_TASKS: set[asyncio.Task[None]] = set()


def reset() -> None:
    """Forget everything. For tests."""
    for held in _HELD.values():
        if held.timer is not None:
            held.timer.cancel()
    _ACTIVE.clear()
    _LAST_END.clear()
    _HELD.clear()
    _LAST_INFO.clear()
    _SELF.clear()


def _log_path() -> str | None:
    return os.getenv(ENV_LOG, "").strip() or None


def _http_target() -> tuple[str, bytes] | None:
    """``(url, secret)`` when the HTTP outlet is configured and usable."""
    url = os.getenv(ENV_URL, "").strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        logger.warning("%s must be an http(s) URL — the HTTP outlet is off", ENV_URL)
        return None
    secret = os.getenv(ENV_SECRET, "")
    if not secret:
        logger.warning(
            "CLORD_EVENT_WEBHOOK_URL is set but its signing secret is not — an unsigned "
            "event cannot be told from a forgery, so nothing is sent"
        )
        return None
    return url, secret.encode()


def enabled() -> bool:
    """Whether any outlet is configured. Everything below is a no-op when not."""
    return _log_path() is not None or bool(os.getenv(ENV_URL, "").strip())


def _defer_max_seconds() -> float:
    raw = os.getenv(ENV_DEFER_MAX, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DEFER_MAX_SECONDS
    return value if value > 0 else DEFAULT_DEFER_MAX_SECONDS


def started_by_from_message(
    message: discord.Message | Any,
    *,
    requester: object | None,
    bot_is_author: bool,
) -> StartedBy:
    """Classify the message that started a turn.

    A message c-lord posted itself with nobody behind it is a seed for a thread
    somebody asked for through the REST API (``/clord`` has an invoker, so it is
    a user's).
    """
    webhook_id = getattr(message, "webhook_id", None)
    author = getattr(message, "author", None)
    if webhook_id is not None:
        return StartedBy("webhook", str(webhook_id), getattr(author, "name", None))
    if requester is None and bot_is_author:
        return StartedBy("api")
    person = requester if requester is not None else author
    person_id = getattr(person, "id", None)
    return StartedBy("user", str(person_id) if person_id is not None else None)


def _last_reply_id(thread_id: int) -> int:
    from .skills.reply_tracker import get_last_reply_message

    message = get_last_reply_message(thread_id)
    return int(message.id) if message is not None else 0


def _info_for(thread: discord.Thread | Any, started_by: StartedBy) -> TurnInfo:
    guild = getattr(thread, "guild", None)
    return TurnInfo(
        thread_id=thread.id,
        guild_id=getattr(guild, "id", None),
        channel_id=getattr(thread, "parent_id", None),
        started_by=started_by,
        started_at=datetime.now(_UTC),
        started_mono=time.monotonic(),
        baseline_reply_id=_last_reply_id(thread.id),
    )


def begin_turn(thread: discord.Thread | Any, started_by: StartedBy) -> TurnInfo | None:
    """Note that a turn starts in *thread*; ``None`` when no outlet is configured."""
    if not enabled():
        return None
    held = _HELD.pop(thread.id, None)
    if held is not None:
        # A new turn takes the thread over; its own end is the one to announce.
        if held.timer is not None:
            held.timer.cancel()
        logger.info(
            "%s held completion dropped — a new turn started (#917)",
            log_ctx(thread_id=thread.id),
        )
    _SELF.pop(thread.id, None)
    info = _info_for(thread, started_by)
    _ACTIVE[thread.id] = info
    _remember(_LAST_INFO, thread.id, info)
    return info


def _remember(table: dict[int, Any], thread_id: int, value: object) -> None:
    table[thread_id] = value
    while len(table) > _MAX_THREADS:
        table.pop(next(iter(table)))  # oldest insertion first


def note_self_turn_started(thread_id: int, at: datetime | None = None) -> None:
    """Claude opened a turn in *thread_id* with nobody's message (#961/#1092).

    A ``<task-notification>`` (#961) or a scheduled wakeup (#1092) started it.

    Records where the thread's replies stood, so the event names the reply this
    turn posts. *at* is when the transcript says the turn began.
    """
    if not enabled():
        return
    try:
        base = _LAST_INFO.get(thread_id)
        _remember(
            _SELF,
            thread_id,
            TurnInfo(
                thread_id=thread_id,
                guild_id=base.guild_id if base is not None else None,
                channel_id=base.channel_id if base is not None else None,
                started_by=StartedBy("self"),
                started_at=at or datetime.now(_UTC),
                started_mono=time.monotonic(),
                baseline_reply_id=_last_reply_id(thread_id),
            ),
        )
    except Exception:
        # Runs inside the mirror's read loop: a bug here must not stop delivery.
        logger.warning(
            "%s could not note the self-started turn (#961)",
            log_ctx(thread_id=thread_id),
            exc_info=True,
        )


def end_turn(info: TurnInfo | None) -> None:
    """The run for *info* is over (whatever the outcome)."""
    if info is not None and _ACTIVE.get(info.thread_id) is info:
        del _ACTIVE[info.thread_id]


def note_turn_end(thread_id: int, outstanding: int, *, self_started: bool = False) -> None:
    """The transcript says a turn ended in *thread_id* with *outstanding* agents running.

    Called by the mirror after it has posted the turn's final answer and before it
    tells the runner. Records the snapshot :func:`finish_turn` reads, and releases
    a ``completion`` held for background agents once none are left.
    *self_started*: a ``<task-notification>`` (#961) or a scheduled wakeup (#1092)
    opened the turn — with no runner to announce it, its end is announced here.
    """
    if not enabled():
        return
    try:
        _remember(_LAST_END, thread_id, (time.monotonic(), outstanding))
        started = _SELF.pop(thread_id, None)
        own = started if self_started else None
        released = outstanding == 0 and _release(thread_id, reason=None)
        if own is None or released or thread_id in _ACTIVE or thread_id in _HELD:
            return
        if outstanding > 0:
            _hold(own, outstanding, reason="self_resumed_turn")
        else:
            _spawn(_deliver("completion", own, reason="self_resumed_turn"))
    except Exception:
        # Runs inside the mirror's read loop: a bug here must not stop delivery.
        logger.warning(
            "%s could not note the turn end (#917)", log_ctx(thread_id=thread_id), exc_info=True
        )


def finish_turn(info: TurnInfo | None, *, failure_reason: str | None = None) -> None:
    """The turn *info* ended: announce ``failure``, or ``completion`` unless held."""
    if info is None:
        return
    if failure_reason is not None:
        _spawn(_deliver("failure", info, reason=failure_reason))
        return
    snapshot = _LAST_END.get(info.thread_id)
    if snapshot is not None and snapshot[0] >= info.started_mono and snapshot[1] > 0:
        _hold(info, snapshot[1])
        return
    _spawn(_deliver("completion", info))


def announce_recovered_turn(
    thread: discord.Thread | Any,
    started_by: StartedBy,
    *,
    started_at: datetime | None,
    failure_reason: str | None,
) -> None:
    """Announce the end of a turn that crossed a bot restart (#978).

    Its run died with the old process (``outcome=cancelled``, which announces
    nothing), so this process, which saw the turn end in the transcript, sends
    the one event: ``failure`` with *failure_reason*, or ``completion`` with
    ``reason: "restart_crossed"``. *started_at* is when the turn's trigger was
    posted — the old process's own record of the start is gone. Any reply this
    process has posted in the thread is the turn's (the catch-up included), so
    ``last_message_id`` is the latest of them. Background agents still out hold
    the ``completion`` as for any turn (#917/#1012).
    """
    if not enabled():
        return
    info = _info_for(thread, started_by)
    info.baseline_reply_id = 0
    if started_at is not None:
        info.started_at = _as_utc(started_at)
    if failure_reason is not None:
        _spawn(_deliver("failure", info, reason=failure_reason))
        return
    # #1012: as for any turn end (#917), not while background agents are out.
    # The mirror read back the ones launched before the restart.
    from .transcript.mirror import background_agents_running

    outstanding = background_agents_running(thread.id)
    if outstanding > 0:
        _hold(info, outstanding, reason="restart_crossed")
    else:
        _spawn(_deliver("completion", info, reason="restart_crossed"))


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=_UTC) if moment.tzinfo is None else moment.astimezone(_UTC)


def emit_blocked(thread: discord.Thread | Any, *, message_id: int | None) -> None:
    """A menu that parks the turn was posted as *message_id*."""
    if not enabled():
        return
    try:
        info = _ACTIVE.get(thread.id) or _info_for(thread, StartedBy("user"))
        _spawn(_deliver("blocked", info, message_id=message_id))
    except Exception:
        logger.warning(
            "%s could not queue the blocked event (#917)",
            log_ctx(thread_id=thread.id),
            exc_info=True,
        )


def _hold(info: TurnInfo, outstanding: int, *, reason: str | None = None) -> None:
    previous = _HELD.pop(info.thread_id, None)
    if previous is not None and previous.timer is not None:
        previous.timer.cancel()
    held = _Held(info, reason=reason)
    with contextlib.suppress(RuntimeError):
        loop = asyncio.get_running_loop()
        held.timer = loop.call_later(
            _defer_max_seconds(), _release, info.thread_id, "background_agents_unresolved"
        )
    _HELD[info.thread_id] = held
    logger.info(
        "%s completion held — %d background agent(s) still running (#917)",
        log_ctx(thread_id=info.thread_id),
        outstanding,
    )


def _release(thread_id: int, reason: str | None) -> bool:
    """Send the completion held for *thread_id*; whether there was one."""
    held = _HELD.pop(thread_id, None)
    if held is None:
        return False
    if held.timer is not None:
        held.timer.cancel()
    _spawn(_deliver("completion", held.info, reason=reason or held.reason))
    return True


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in the background; the turn never waits for an outlet."""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        logger.warning("turn event dropped — no running event loop (#917)")
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def drain() -> None:
    """Wait for every pending delivery. For tests and orderly shutdown."""
    while _TASKS:
        await asyncio.gather(*list(_TASKS), return_exceptions=True)


async def _settle(thread_id: int) -> None:
    """Wait until the mirror has no final answer still held back.

    The mirror posts the answer at the transcript's turn-end marker, or — for a
    build that writes none — after an idle window. The runner can finish before
    that, and an event sent then would name the previous reply.
    """
    from .transcript.mirror import has_pending_final

    deadline = time.monotonic() + _SETTLE_MAX_SECONDS
    while has_pending_final(thread_id) and time.monotonic() < deadline:
        await asyncio.sleep(_SETTLE_POLL)


def _build_event(
    kind: Kind,
    info: TurnInfo,
    *,
    last_message_id: int | None,
    ended_at: datetime,
    reason: str | None,
) -> dict[str, Any]:
    def text(value: int | None) -> str | None:
        return str(value) if value is not None else None

    event: dict[str, Any] = {
        "v": EVENT_VERSION,
        "event_id": uuid.uuid4().hex,
        "kind": kind,
        "guild_id": text(info.guild_id),
        "channel_id": text(info.channel_id),
        "thread_id": str(info.thread_id),
        "last_message_id": text(last_message_id),
        "started_by": info.started_by.as_dict(),
        "turn_started_at": info.started_at.isoformat().replace("+00:00", "Z"),
        "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
    }
    if reason is not None:
        event["reason"] = reason
    return event


async def _deliver(
    kind: Kind,
    info: TurnInfo,
    *,
    reason: str | None = None,
    message_id: int | None = None,
) -> None:
    """Build one event and send it everywhere it is configured. Never raises."""
    try:
        ended_at = datetime.now(_UTC)
        last_message_id = message_id
        if kind != "blocked":
            if kind == "completion":
                await _settle(info.thread_id)
            reply_id = _last_reply_id(info.thread_id)
            last_message_id = reply_id if reply_id > info.baseline_reply_id else None
        event = _build_event(
            kind, info, last_message_id=last_message_id, ended_at=ended_at, reason=reason
        )
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
        ctx = log_ctx(thread_id=info.thread_id)
        logger.info("%s turn event: %s (#917)", ctx, kind)
        path = _log_path()
        if path is not None:
            await _append_line(path, body, ctx)
        target = _http_target()
        if target is not None:
            await _post(target[0], target[1], body, kind, ctx)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "%s could not send the turn event (#917)",
            log_ctx(thread_id=info.thread_id),
            exc_info=True,
        )


async def _append_line(path: str, body: bytes, ctx: str) -> None:
    def write() -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # One write() in O_APPEND mode: concurrent turns cannot interleave a line.
        with open(path, "ab") as handle:
            handle.write(body + b"\n")

    try:
        await asyncio.to_thread(write)
    except OSError:
        logger.warning("%s could not append the turn event to %s (#917)", ctx, path, exc_info=True)


async def _post(url: str, secret: bytes, body: bytes, kind: str, ctx: str) -> None:
    import aiohttp

    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature,
        "X-Clord-Event": kind,
        "User-Agent": "c-lord-events/1",
    }
    attempts = 1 + len(_RETRY_DELAYS)
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(_RETRY_DELAYS[attempt - 1])
        status: int | None = None
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:  # noqa: SIM117
                async with session.post(
                    url, data=body, headers=headers, allow_redirects=False
                ) as r:
                    status = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:  # noqa: UP041
            logger.warning(
                "%s turn event POST failed (attempt %d/%d): %s", ctx, attempt + 1, attempts, exc
            )
            continue
        if status < 300:
            return
        if status < 500 and status not in (408, 429):
            logger.warning("%s turn event POST refused with HTTP %d — not retrying", ctx, status)
            return
        logger.warning(
            "%s turn event POST got HTTP %d (attempt %d/%d)", ctx, status, attempt + 1, attempts
        )
    logger.warning("%s turn event POST given up after %d attempts (#917)", ctx, attempts)
