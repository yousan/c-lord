"""One self-updating line that fills long silences during a turn (#539).

The pain this solves: "最初のメッセージを投げても応答がない。進んでいるのか
どうかの途中経過がわかりにくい" — a turn can run for ten minutes with nothing
in the thread, so a live session is indistinguishable from a dead one.

**What it deliberately does not do is narrate constantly.** Measured on
production (17 threads / 25 turns / 147 gaps, 2026-08-26), visible output
already arrives every ~39s (median); the first output of a turn lands within
23s (median) / 64s (p90). A periodic progress line would pile noise on top of
output that is already adequate, and c-lord has been burned by exactly that
before — the per-turn thread-name lamp saturated Discord's rename limit (#241).

What actually hurts is the *tail*: 15% of gaps exceed 2 minutes and 7% exceed
5 minutes. So this component stays quiet until the thread has genuinely gone
silent, then shows a single subtext line, updates it **in place**, and deletes
it the moment real output returns. The thread therefore grows by at most one
message at a time, and ends the turn back at zero.

Distinguishing "作業中" from "待機中" (#539 AC3) uses tool activity rather than
elapsed time: while Claude grinds through tools the mirror keeps seeing jsonl
events even though none of them are posted, so "tools are moving" is a real
signal that the session is alive. When even that stops, the line says so
instead of claiming progress it cannot see.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

logger = logging.getLogger(__name__)

# How long the thread must be silent before the line appears at all.
# 60s would fire on 34% of measured gaps and compete with Claude's own
# narration; 90s targets the ~15-20% tail that is actually painful.
DEFAULT_QUIET_SECONDS = 90.0

# How often the posted line is refreshed. Message edits use a different Discord
# rate-limit bucket than thread renames (#246), and one edit per 15s per thread
# is far below it.
DEFAULT_UPDATE_SECONDS = 15.0

# If no tool event has been seen for this long, stop claiming "作業中".
DEFAULT_STALLED_SECONDS = 60.0

# Tool labels can be long (a full Bash command); keep the line to one row.
_MAX_LABEL_CHARS = 60


class _Post(Protocol):
    def __call__(self, text: str) -> Awaitable[object | None]: ...


class _Edit(Protocol):
    def __call__(self, handle: object, text: str) -> Awaitable[None]: ...


class _Delete(Protocol):
    def __call__(self, handle: object) -> Awaitable[None]: ...


def _mmss(seconds: float) -> str:
    total = int(max(0.0, seconds))
    return f"{total // 60}:{total % 60:02d}"


def _shorten(label: str) -> str:
    label = " ".join(label.split())
    if len(label) <= _MAX_LABEL_CHARS:
        return label
    return label[: _MAX_LABEL_CHARS - 1] + "…"


class TurnProgress:
    """Shows a single subtext line while a turn is quiet, and only then.

    Wiring is three async callables so the Discord specifics stay in the Cog and
    this stays unit-testable with a fake clock:

    - ``post(text) -> handle`` — send the line, return something to edit later
    - ``edit(handle, text)`` — refresh it in place
    - ``delete(handle)`` — take it away

    Every call is best-effort: a progress line must never be able to break a
    turn, so failures are logged and swallowed. A failed ``post`` simply means
    the line does not appear; a failed ``delete`` still drops the handle so the
    next gap starts from a clean state rather than editing a message that may
    no longer exist.
    """

    def __init__(
        self,
        *,
        post: _Post,
        edit: _Edit,
        delete: _Delete,
        quiet_seconds: float = DEFAULT_QUIET_SECONDS,
        update_seconds: float = DEFAULT_UPDATE_SECONDS,
        stalled_seconds: float = DEFAULT_STALLED_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._post = post
        self._edit = edit
        self._delete = delete
        self._quiet_seconds = quiet_seconds
        self._update_seconds = update_seconds
        self._stalled_seconds = stalled_seconds
        self._clock = clock

        self._armed = False
        self._handle: object | None = None
        self._turn_start = 0.0
        self._last_output = 0.0
        self._last_activity = 0.0
        self._last_edit = 0.0
        self._tool_label: str | None = None
        self._tool_count = 0

    # -- lifecycle ---------------------------------------------------------

    def begin_turn(self) -> None:
        """Arm for a new turn. Idempotent within the same turn.

        Arming matters: an idle thread produces no jsonl events at all, which
        from the transcript alone looks the same as Claude thinking silently.
        Only a turn we know started can put a line in the thread, so an idle
        thread can never sprout a stale "待機中".
        """
        now = self._clock()
        if self._armed:
            return
        self._armed = True
        self._turn_start = now
        self._last_output = now
        self._last_activity = now
        self._tool_label = None
        self._tool_count = 0

    async def end_turn(self) -> None:
        """Disarm and take the line away. Safe to call when nothing is shown."""
        self._armed = False
        await self._remove()

    # -- signals -----------------------------------------------------------

    async def note_output(self) -> None:
        """Something a reader can see reached the thread.

        Resets the silence window and removes the line immediately — waiting for
        the next tick would leave the filler sitting *below* the real output it
        was standing in for.
        """
        self._last_output = self._clock()
        await self._remove()

    def note_activity(self, label: str | None = None) -> None:
        """A tool event was seen — evidence the session is alive but busy."""
        self._last_activity = self._clock()
        if label:
            self._tool_label = _shorten(label)
            self._tool_count += 1

    # -- driving -----------------------------------------------------------

    async def tick(self) -> None:
        """Post / refresh / withdraw the line as the silence dictates.

        Called from the mirror's own loop rather than a private timer task: that
        loop already wakes at least every ``idle_flush_seconds`` (8s default),
        which is finer than the 15s refresh, and it avoids a second task whose
        lifetime would have to be kept in sync with the mirror's.
        """
        if not self._armed:
            await self._remove()
            return

        now = self._clock()
        if now - self._last_output < self._quiet_seconds:
            return

        body = self._render(now)
        if self._handle is None:
            handle = await self._safely(self._post(body), "post")
            if handle is not None:
                self._handle = handle
                self._last_edit = now
            return

        if now - self._last_edit >= self._update_seconds:
            await self._safely(self._edit(self._handle, body), "edit")
            self._last_edit = now

    # -- internals ---------------------------------------------------------

    def _render(self, now: float) -> str:
        elapsed = _mmss(now - self._turn_start)
        idle = now - self._last_activity
        if self._tool_label is not None and idle < self._stalled_seconds:
            return f"-# ⚙️ 作業中 {elapsed} · 🔧 {self._tool_label} · ツール {self._tool_count} 件"
        return (
            f"-# ⏳ 待機中 {elapsed} · 直近の動きから {int(idle)} 秒"
            " · 長考かコンテキスト圧縮の可能性"
        )

    async def _remove(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        await self._safely(self._delete(handle), "delete")

    async def _safely(self, awaitable: Awaitable[object | None], what: str) -> object | None:
        """Await *awaitable*, swallowing anything that goes wrong.

        A progress line is decoration; it must never propagate an exception into
        the mirror loop and take the actual answer down with it.
        """
        try:
            return await awaitable
        except Exception:
            logger.debug("TurnProgress %s failed", what, exc_info=True)
            with contextlib.suppress(Exception):
                pass
            return None
