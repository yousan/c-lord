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
3. If the state changed (or the volatile window-index moved, or the
   topic is set), build the new thread name with
   :func:`thread_name.build_name` and rename the Discord thread
   when it differs from the current name. Minimises API calls.

The loop deliberately never touches the ``topic`` body — that is
the user-visible stable identity. Only the leading status emoji and
the trailing ``W<N> │`` window-index hint are kept fresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from typing import TYPE_CHECKING

import discord

from .thread_name import build_name

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .database.repository import SessionRepository

logger = logging.getLogger(__name__)

# Format: <session_name>|<window_id>|<window_index>|<@thread_id>|<window_name>
_LIST_WINDOWS_FORMAT = "#{session_name}|#{window_id}|#{window_index}|#{@thread_id}|#{window_name}"

_DEFAULT_INTERVAL_SECONDS = 60.0

# How many bottom lines of the pane to check for prompt/error indicators.
_PANE_PROBE_LINES = 6

# Characters that indicate Claude Code's input prompt (waiting for user).
_WAITING_PROMPTS = frozenset({"❯", ">"})

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
    Error takes highest priority; waiting is detected from the bare
    ``❯``/``>`` prompt; otherwise the state is ``'running'``.
    """
    if not pane_text:
        return "running"
    lines = pane_text.rstrip().splitlines()
    tail = lines[-_PANE_PROBE_LINES:]

    for line in tail:
        for indicator in _ERROR_INDICATORS:
            if indicator in line:
                return "error"

    for line in tail:
        if line.strip() in _WAITING_PROMPTS:
            return "waiting"

    return "running"


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
            idx_str = window_info.get("window_index") or ""
            window_index: int | None = int(idx_str) if idx_str.isdigit() else None

            # Detect fine-grained lamp state from pane content (#120).
            session_name = window_info.get("session_name", "")
            window_name = window_info.get("window_name", "")
            pane_text = await asyncio.to_thread(_capture_pane_text, session_name, window_name)
            new_state = _pane_lamp_state(pane_text)
        else:
            new_state = "dead"
            window_id = record.tmux_window_id
            window_index = None

        # Persist state and window-id changes.
        if record.state != new_state:
            await self._repo.set_state(thread_id, new_state)
        if window_id and record.tmux_window_id != window_id:
            await self._repo.set_tmux_window_id(thread_id, window_id)

        # Without a topic we can't construct a sensible name yet — skip.
        if not record.topic:
            return

        new_name = build_name(record.topic, new_state, window_index)

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

        try:
            await asyncio.wait_for(channel.edit(name=new_name), timeout=5.0)
            logger.info(
                "state-sync: renamed thread %d → %r (state=%s)",
                thread_id,
                new_name,
                new_state,
            )
        except (discord.HTTPException, TimeoutError) as exc:
            logger.debug("state-sync: rename failed for thread %d: %s", thread_id, exc)
