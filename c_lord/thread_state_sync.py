"""Periodic state-sync loop for Discord thread names (Issue #95, #120).

Every ``poll_interval`` seconds:

1. Snapshot the live tmux state with ``tmux list-windows -a`` —
   gives us each window's ``window_id`` (immutable), ``window_index``
   (volatile hint), ``window_name``, ``session_name``, and the
   ``@thread_id`` window option.
2. For each session row in the DB, compute the new ``state``:
   * ``running``  → tmux window exists and Claude is actively executing
   * ``waiting``  → tmux window exists and the ❯ input prompt is visible
   * ``error``    → tmux window exists and error indicators found in pane
   * ``dead``     → no tmux window exists
   * ``pending`` is reserved for explicit external setters and is
     never produced by this loop.
3. If the state changed (or the work{N} window number changed, or the
   topic is set), build the new thread name with
   :func:`thread_name.build_name` and rename the Discord thread
   when it differs from the current name. Minimises API calls.

The loop deliberately never touches the ``topic`` body — that is
the user-visible stable identity. Only the leading status emoji and
the leading ``W<N> │`` work-number hint are kept fresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from typing import TYPE_CHECKING

import discord

from .thread_name import build_name
from .tmux import parse_work_number

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .database.repository import SessionRepository

logger = logging.getLogger(__name__)

# Format: <session_name>|<window_id>|<window_index>|<@thread_id>|<window_name>
_LIST_WINDOWS_FORMAT = "#{session_name}|#{window_id}|#{window_index}|#{@thread_id}|#{window_name}"

_DEFAULT_INTERVAL_SECONDS = 60.0

# Timeout for a single rename HTTP call.  Long enough for normal API response;
# short enough not to block the tick when discord.py's rate-limit sleep fires.
_RENAME_TIMEOUT_SECONDS = 30.0

# Conservative backoff applied when a rename times out (likely discord.py's
# rate-limit retry-sleep being cancelled by wait_for) — matches Discord's
# ~10-minute rename window per channel.
_DEFAULT_RENAME_BACKOFF_SECONDS = 600.0

# How many bottom lines of the pane to check for prompt/error indicators.
_PANE_PROBE_LINES = 6

# Characters that signal active Claude Code execution in the pane content area.
# ✢ (U+2722) appears during response generation (Actualizing/Synthesizing/…).
# ✻ (U+273B) appears during tool execution (Running bash / Reading file / …).
# Both are specific to the Claude Code TUI and absent from idle panes.
_RUNNING_CHARS = frozenset({"✢", "✻"})

# Substrings that indicate an error state (checked in the bottom pane lines).
_ERROR_INDICATORS = (
    "APIError",
    "Error:",
    "error:",
    "Fatal error",
    "Traceback (most recent",
)

# Number of pane lines to capture for state detection (kept small for speed).
_PANE_CAPTURE_LINES = 20


def _pane_lamp_state(pane_text: str) -> str:
    """Determine lamp state from captured pane content.

    Returns one of ``'running'``, ``'waiting'``, or ``'error'``.
    Priority: error > running (positive signal) > waiting (default).

    Default is ``'waiting'`` because the idle state (user prompt visible,
    draft text in input, ``-- INSERT --`` etc.) is far more common than
    active execution.

    Running is detected by the Claude Code TUI characters that appear only
    during active work: ``✢`` (response generation) and ``✻`` (tool execution).
    These are absent from idle panes — verified against live pane captures.
    """
    if not pane_text:
        return "waiting"
    lines = pane_text.rstrip().splitlines()
    tail = lines[-_PANE_PROBE_LINES:]

    for line in tail:
        for indicator in _ERROR_INDICATORS:
            if indicator in line:
                return "error"

    for line in tail:
        if any(ch in line for ch in _RUNNING_CHARS):
            return "running"

    return "waiting"


def _capture_pane_text(
    session_name: str, window_name: str, lines: int = _PANE_CAPTURE_LINES
) -> str:
    """Run ``tmux capture-pane`` for the given session:window and return raw text."""
    if not session_name or not window_name:
        return ""
    try:
        result = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-p",
                "-J",
                "-t",
                f"{session_name}:{window_name}",
                "-S",
                f"-{lines}",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _list_all_windows() -> list[dict[str, str]]:
    """Run ``tmux list-windows -a`` and parse the result.

    Returns an empty list if tmux is unavailable / no server / etc.
    Never raises.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", _LIST_WINDOWS_FORMAT],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    out: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        out.append(
            {
                "session_name": parts[0],
                "window_id": parts[1],
                "window_index": parts[2],
                "thread_id": parts[3] if len(parts) > 3 else "",
                "window_name": parts[4] if len(parts) > 4 else "",
            }
        )
    return out


def _index_by_thread_id(
    windows: list[dict[str, str]],
) -> dict[int, dict[str, str]]:
    """Build a thread_id → window-info mapping from the snapshot."""
    by_tid: dict[int, dict[str, str]] = {}
    for w in windows:
        tid = w.get("thread_id") or ""
        if tid.isdigit():
            by_tid[int(tid)] = w
    return by_tid


class ThreadStateSyncLoop:
    """Background task that keeps Discord thread names in sync with tmux."""

    def __init__(
        self,
        bot: Bot,
        session_repo: SessionRepository,
        *,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._bot = bot
        self._repo = session_repo
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        # Per-thread rate-limit backoff: thread_id → monotonic time until next rename is allowed.
        self._rename_backoff: dict[int, float] = {}

    def start(self) -> None:
        """Spawn the loop task. Idempotent — second call is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="thread_state_sync")
        logger.info("Started thread-state sync loop (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        """Cancel the loop. Safe to call multiple times."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    async def _run(self) -> None:
        # First pass after a short delay so the bot is connected.
        await asyncio.sleep(min(5.0, self._interval))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("thread-state sync tick raised")
            await asyncio.sleep(self._interval)

    async def tick(self) -> None:
        """One synchronisation pass. Public for testing."""
        windows = await asyncio.to_thread(_list_all_windows)
        by_tid = _index_by_thread_id(windows)

        sessions = await self._repo.list_all(limit=500)
        for record in sessions:
            await self._sync_one(record, by_tid)

    async def _sync_one(
        self,
        record,  # SessionRecord
        by_tid: dict[int, dict[str, str]],
    ) -> None:
        """Reconcile a single session row with live tmux state."""
        thread_id = record.thread_id
        window_info = by_tid.get(thread_id)

        if window_info is not None:
            window_id = window_info["window_id"] or None

            # Detect fine-grained lamp state from pane content (#120).
            session_name = window_info.get("session_name", "")
            window_name = window_info.get("window_name", "")
            # W<N> follows the stable work{N} name, not the volatile window_index.
            window_number: int | None = parse_work_number(window_name)
            pane_text = await asyncio.to_thread(_capture_pane_text, session_name, window_name)
            new_state = _pane_lamp_state(pane_text)
        else:
            new_state = "dead"
            window_id = record.tmux_window_id
            window_number = None

        # Persist state and window-id changes.
        if record.state != new_state:
            await self._repo.set_state(thread_id, new_state)
        if window_id and record.tmux_window_id != window_id:
            await self._repo.set_tmux_window_id(thread_id, window_id)

        # Without a topic we can't construct a sensible name yet — skip.
        if not record.topic:
            return

        new_name = build_name(record.topic, new_state, window_number)

        # Fetch the Discord thread and rename if different.
        try:
            channel = self._bot.get_channel(thread_id)
            if channel is None:
                # Don't fetch_channel on every tick — too costly for dead threads.
                # We just persist state; next user interaction will rename.
                return
        except Exception:
            return

        if not isinstance(channel, discord.Thread):
            return
        if (channel.name or "") == new_name:
            return

        # Per-thread rate-limit backoff: skip rename if still within back-off window.
        now = asyncio.get_event_loop().time()
        backoff_until = self._rename_backoff.get(thread_id, 0.0)
        if now < backoff_until:
            logger.debug(
                "state-sync: rename skipped thread %d (rate-limited, retry in %.0fs)",
                thread_id,
                backoff_until - now,
            )
            return

        try:
            await asyncio.wait_for(channel.edit(name=new_name), timeout=_RENAME_TIMEOUT_SECONDS)
            logger.info(
                "state-sync: renamed thread %d → %r (state=%s)",
                thread_id,
                new_name,
                new_state,
            )
            self._rename_backoff.pop(thread_id, None)
        except discord.HTTPException as exc:
            if exc.status == 429:
                retry_after = float(
                    getattr(exc, "retry_after", _DEFAULT_RENAME_BACKOFF_SECONDS)
                    or _DEFAULT_RENAME_BACKOFF_SECONDS
                )
                self._rename_backoff[thread_id] = asyncio.get_event_loop().time() + retry_after
                logger.warning(
                    "state-sync: rename rate-limited thread %d, backing off %.0fs",
                    thread_id,
                    retry_after,
                )
            else:
                logger.debug("state-sync: rename failed for thread %d: %s", thread_id, exc)
        except asyncio.TimeoutError:  # noqa: UP041 — asyncio.TimeoutError != TimeoutError on Py3.10
            # discord.py's rate-limit sleep (retry_after) is cancelled by wait_for's timeout.
            # Apply conservative backoff to prevent rapid PATCH retries.
            self._rename_backoff[thread_id] = (
                asyncio.get_event_loop().time() + _DEFAULT_RENAME_BACKOFF_SECONDS
            )
            logger.warning(
                "state-sync: rename timed out for thread %d"
                " (suspected rate-limit), backing off %.0fs",
                thread_id,
                _DEFAULT_RENAME_BACKOFF_SECONDS,
            )
