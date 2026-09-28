"""Pick up the messages that arrived while the Discord gateway was down (#745).

Discord delivers messages over one websocket. When that socket is gone — the
host lost DNS for 6h35m on 2026-09-16 — Discord keeps accepting messages, but
nothing reaches ``on_message``. A reconnect that manages a RESUME gets the
missed events replayed; one that has to IDENTIFY again (the session expired)
gets **nothing**: the messages are simply not delivered, ever. Before #745
nothing went back for them, so a message posted in that window got no
reaction, no reply and no log line, and its sender re-posted it word for word
four and a half hours later.

This module is the bookkeeping for going back. It does not talk to Discord —
:class:`~c_lord.cogs.claude_chat.ClaudeChatCog` does, and uses these pieces:

* :class:`GatewayWatch` — **where to start reading.** It remembers the last
  moment the gateway was known to deliver (the newest message seen) and turns a
  disconnect → reconnect into an :class:`Outage` with a ``since`` to read from.
  It deliberately does *not* read from the moment the disconnect was noticed:
  a suspended host or a frozen process notices long after the gateway stopped
  delivering, and everything posted in between would be skipped.
* :class:`SeenMessages` — **what already ran.** Every thread message the
  gateway delivers is recorded; reading history then skips those, and a message
  picked up from history is recorded too, so a late gateway delivery of the
  same message is not run a second time. This is what makes the generous
  ``since`` above safe.
* :class:`Startup` / :func:`last_mark_before` — **the restart gap (#776).** A
  new process IDENTIFYs, so what was posted while no process was running is
  never delivered either — and ``SeenMessages`` starts empty, so it cannot
  tell what the previous process already ran. That is read off Discord
  instead: every turn leaves a reaction on its trigger message and every
  other way of handling a message posts in the thread, so a request newer
  than the previous process's last such mark is one it never saw. No DB
  state is kept for it.
* :func:`missed_notice` / :func:`merge_missed_prompt` — **what to say.** One
  line in the thread explaining the delay, and — when several messages were
  missed in one thread — one prompt carrying all of them, because replaying
  them one by one would make each interrupt the one before it.

The behaviour is written down in ``docs/specs/gateway-backfill.md``.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Literal

import discord

# How far before the last sign of life to start reading. Messages are delivered
# in sequence order, not creation order, and the two can disagree by a little;
# anything this reaches that did arrive is recognised by ``SeenMessages``.
MARGIN = timedelta(minutes=2)

# How many delivered thread-message ids to remember. The lookback above spans
# minutes to hours; ten thousand thread messages in that span is far beyond any
# c-lord deployment, and the ids cost a few hundred KB.
SEEN_CAPACITY = 10_000

# Messages read back per thread on one reconnect. A thread that collected more
# than this while we were away is logged as such rather than paged through.
HISTORY_LIMIT = 100

# How far back the first connect of a process reads (#776). A restart leaves
# a gap of seconds, a crash waiting on its supervisor minutes. Past an hour the
# message is no longer "a moment ago" — and a thread whose tail is an unmarked
# request that old is more likely one c-lord deliberately left alone.
STARTUP_LOOKBACK = timedelta(hours=1)

Outcome = Literal["run", "held", "superseded"]
Cause = Literal["gateway", "restart"]


class SeenMessages:
    """Message ids the gateway (or a previous pick-up) already handed us.

    Bounded FIFO: the oldest id is forgotten first. Not thread-safe, and does not
    need to be — it is only touched from the event loop.
    """

    def __init__(self, capacity: int = SEEN_CAPACITY) -> None:
        self._capacity = capacity
        self._ids: OrderedDict[int, None] = OrderedDict()

    def add(self, message_id: int) -> bool:
        """Record *message_id*. True when it was not already recorded."""
        if message_id in self._ids:
            return False
        self._ids[message_id] = None
        while len(self._ids) > self._capacity:
            self._ids.popitem(last=False)
        return True

    def __contains__(self, message_id: object) -> bool:
        return message_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)


@dataclass(frozen=True)
class Outage:
    """One stretch without a gateway, as seen from the moment it ended."""

    down_at: datetime
    """When the disconnect was first noticed (the first of the retry streak)."""

    last_alive_at: datetime
    """The newest moment the gateway was known to deliver before ``down_at``."""

    back_at: datetime
    """When ``on_ready`` / ``on_resumed`` fired."""

    since: datetime
    """Where to start reading history: a margin before the last sign of life,
    never before this process first came up."""

    @property
    def duration(self) -> timedelta:
        return self.back_at - self.down_at


@dataclass
class GatewayWatch:
    """Turns ``disconnect`` / ``ready`` / ``resumed`` into :class:`Outage` records.

    ``clock`` returns an aware UTC ``datetime``; tests replace it.
    """

    clock: Callable[[], datetime] = field(default=discord.utils.utcnow)
    margin: timedelta = MARGIN
    started_at: datetime | None = None
    """When this process came up (#776). Anything the bot posted before it was
    posted by the previous process. Defaults to the moment of construction."""
    _first_up: datetime | None = None
    _last_alive: datetime | None = None
    _down_at: datetime | None = None
    _alive_at_down: datetime | None = None

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()

    def first_connect(self, now: datetime | None = None) -> Startup | None:
        """Record ``ready`` if it is this process's first (#776).

        The gap to read back after a restart — or ``None`` when the process
        was already up, which is what :meth:`connected` is for.
        """
        if self._first_up is not None:
            return None
        now = now or self.clock()
        self.connected(now)
        started_at = self.started_at or now
        return Startup(started_at=started_at, up_at=now, since=started_at - STARTUP_LOOKBACK)

    def seen(self, at: datetime) -> None:
        """The gateway delivered something created at *at*."""
        if self._last_alive is None or at > self._last_alive:
            self._last_alive = at

    def disconnected(self, now: datetime | None = None) -> bool:
        """Record a ``disconnect``. True for the first one of a streak.

        discord.py dispatches ``disconnect`` on every failed reconnect attempt
        (40 of them in the incident); the outage began at the first. A
        disconnect before the first connect is not an outage — nothing was
        being delivered to miss.
        """
        if self._first_up is None or self._down_at is not None:
            return False
        self._down_at = now or self.clock()
        self._alive_at_down = self._last_alive
        return True

    def connected(self, now: datetime | None = None) -> Outage | None:
        """Record ``ready`` / ``resumed``. The outage that just ended, if any."""
        now = now or self.clock()
        if self._first_up is None:
            self._first_up = now
            self._last_alive = now
            return None
        if self._down_at is None:
            return None
        down_at = self._down_at
        # Frozen at the moment of the disconnect: a RESUME replays the missed
        # messages *before* ``resumed`` fires, and they would otherwise move the
        # last sign of life to the middle of the outage.
        last_alive = min(self._alive_at_down or down_at, down_at)
        since = max(self._first_up, last_alive - self.margin)
        self._down_at = None
        self._alive_at_down = None
        self._last_alive = now
        return Outage(down_at=down_at, last_alive_at=last_alive, back_at=now, since=since)


@dataclass(frozen=True)
class Startup:
    """The first connect of a process: what it may have missed (#776)."""

    started_at: datetime
    """When this process came up. The bot's marks from before it are the
    previous process's; later ones are this process's own."""

    up_at: datetime
    """When the first ``on_ready`` fired. Later messages are delivered live."""

    since: datetime
    """Where to start reading: :data:`STARTUP_LOOKBACK` before ``started_at``."""


def last_mark_before(
    history: Sequence[discord.Message], *, bot_id: int | None, before: datetime
) -> int | None:
    """Id of the newest message showing the previous process was here (#776).

    A mark is a message the bot posted, or a message carrying the bot's own
    reaction — the per-turn lamp (#246) sits on every trigger message, and a
    message handled without a turn (menu answer, closed notice, slot wait, ⚡)
    is followed by a post. Only marks from before *before* (this process's
    start) count: this process's transcript mirror may already have posted in
    the thread by the time it is read.
    """
    newest: int | None = None
    for m in history:
        if m.created_at >= before:
            continue
        author = getattr(m, "author", None)
        own_post = bot_id is not None and getattr(author, "id", None) == bot_id
        own_reaction = any(getattr(r, "me", False) for r in getattr(m, "reactions", None) or ())
        if (own_post or own_reaction) and (newest is None or m.id > newest):
            newest = m.id
    return newest


@dataclass(frozen=True)
class MissedEntry:
    """One missed message, as it goes into a merged prompt."""

    created_at: datetime
    author: str
    text: str
    attachments: tuple[str, ...] = ()


def _clock_text(moment: datetime, *, tz: tzinfo | None, with_date: bool) -> str:
    """``HH:MM`` in *tz* (the host's zone when ``None``), or ``M/D HH:MM``."""
    local = moment.astimezone(tz)
    hm = f"{local:%H:%M}"
    return f"{local.month}/{local.day} {hm}" if with_date else hm


def missed_notice(
    *,
    down_at: datetime,
    back_at: datetime,
    first_missed_at: datetime,
    count: int,
    outcome: Outcome,
    cause: Cause = "gateway",
    tz: tzinfo | None = None,
) -> str:
    """The one line posted in a thread whose messages were picked up late.

    The start shown is never later than the first missed message: a frozen
    process notices the disconnect only after it thaws, and the line must not
    claim the connection was fine when the message was posted.

    ``outcome``:

    * ``"run"`` — the message(s) go through the ordinary reply path now.
    * ``"held"`` — the thread was closed on purpose (#512); the reply path will
      answer with its closed notice, so this line promises nothing.
    * ``"superseded"`` — the sender already posted again after the reconnect;
      running the old one now would interrupt the new one.

    ``cause`` says why it was missed: the gateway was down (#745), or c-lord
    itself was restarting (#776).
    """
    start = min(down_at, first_missed_at)
    with_date = start.astimezone(tz).date() != back_at.astimezone(tz).date()
    span = (
        f"{_clock_text(start, tz=tz, with_date=with_date)}"
        f"〜{_clock_text(back_at, tz=tz, with_date=with_date)}"
    )
    what = "この依頼" if count == 1 else f"この間に届いた {count} 件の依頼"
    why = "c-lord が再起動しており" if cause == "restart" else "Discord と接続できておらず"
    head = f"-# 🔌 {span} の間 {why}、{what}を受け取れていませんでした。"
    if outcome == "run":
        return head + ("いまから処理します。" if count == 1 else "まとめていまから処理します。")
    if outcome == "superseded":
        return head + (
            "そのあとに届いた依頼を先に処理しているので、こちらは実行していません。"
            "必要ならもう一度送ってください。"
        )
    return head


def merge_missed_prompt(entries: Sequence[MissedEntry], *, tz: tzinfo | None = None) -> str:
    """One prompt carrying every missed message of a thread, oldest first.

    Only used when more than one was missed. The last entry's attachments are
    staged by the ordinary reply path; the earlier ones are listed by name and
    URL here, since that path stages the trigger message's files only.
    """
    n = len(entries)
    last_day = entries[-1].created_at.astimezone(tz).date() if entries else None
    parts = [
        f"（c-lord がメッセージを受け取れていなかった間に、このスレッドへ {n} 件のメッセージが"
        "届いていました。古い順に並べます。）"
    ]
    for i, entry in enumerate(entries, 1):
        with_date = entry.created_at.astimezone(tz).date() != last_day
        stamp = _clock_text(entry.created_at, tz=tz, with_date=with_date)
        block = f"--- {i}/{n} {stamp} {entry.author} ---\n{entry.text}"
        if entry.attachments:
            block += "\n(添付: " + ", ".join(entry.attachments) + ")"
        parts.append(block)
    return "\n\n".join(parts)
