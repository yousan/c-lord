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
3. If the state changed (or the w{N} window number changed, or the
   topic is set), build the new thread name with
   :func:`thread_name.build_name` and rename the Discord thread
   when it differs from the current name. Minimises API calls.

The loop deliberately never touches the ``topic`` body — that is
the user-visible stable identity. Only the leading status emoji and
the leading ``W<N> │`` work-number hint are kept fresh.  A session closed with
``/close-workspace`` keeps its ``[終了]`` marker here (#512): the loop sees it as
``dead`` like any window-less thread, so without the flag the repaint would strip
the marker off.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import re
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

from .notify_policy import owner_notify_id
from .session_close import is_closed
from .thread_name import build_name
from .tmux import parse_work_number

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .database.repository import SessionRepository

logger = logging.getLogger(__name__)

# Wall-clock format used to persist the rename backoff deadline (#281). Matches
# the DB's datetime('now','localtime') text so values are directly comparable.
_BACKOFF_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now() -> datetime.datetime:
    """Current local wall-clock time. Indirected for deterministic testing."""
    return datetime.datetime.now()


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

# How many bottom lines of the pane to check for error indicators.
_PANE_PROBE_LINES = 6

# How many bottom lines to scan for the live working spinner. The spinner
# renders just above the input box, but the box + status footer (~8 lines) and
# any in-progress tool-result preview push it 10–20 lines off the bottom — so a
# narrow window misses it. Verified against live captures (#190).
_RUNNING_PROBE_LINES = 30

# The live working spinner shows a "(<elapsed> · …)" timer that only exists
# while Claude is actively generating/executing, e.g.
#   ✢ Swirling… (2m 29s · ↓ 9.3k tokens)
#   ✶ Creating PR… (11m 57s · ↑ 36.5k tokens)
#   ✻ Running… (12s · esc to interrupt)
# A completed turn collapses to "<char> <Word> for <N>s" (no parenthetical), so
# matching the timer — not the spinner glyph — avoids false-positives on stale
# completed spinners left in scrollback (#190). It is also independent of which
# glyph the spinner is cycling through (✢ ✻ ✶ · …), which the old glyph set missed.
_RUNNING_SPINNER_RE = re.compile(r"\((?:\d+h\s*)?(?:\d+m\s*)?\d+s\s*·")

# Spinner glyphs that, at the very bottom of the pane, still signal active work.
# Kept as a narrow fallback for panes where the spinner has no timer line yet.
_RUNNING_CHARS = frozenset({"✢", "✻", "✶"})

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

    Running is detected by the live working spinner's ``(<elapsed> · …)`` timer
    (see :data:`_RUNNING_SPINNER_RE`), scanned across a wide bottom window since
    the input box + footer + tool-result preview push it well off the bottom
    (#190). A narrow bottom-glyph check remains as a fallback.
    """
    if not pane_text:
        return "waiting"
    lines = pane_text.rstrip().splitlines()
    tail = lines[-_PANE_PROBE_LINES:]

    for line in tail:
        for indicator in _ERROR_INDICATORS:
            if indicator in line:
                return "error"

    for line in lines[-_RUNNING_PROBE_LINES:]:
        if _RUNNING_SPINNER_RE.search(line):
            return "running"

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


def _pane_foreground_command(session_name: str, window_name: str) -> str | None:
    """Foreground command of ``session:window``'s pane, or None when unreadable (#510).

    The sweep already knows the session/window it captured from, so it asks tmux
    directly rather than routing through a :class:`TmuxSessionManager` (same
    reason :func:`_capture_pane_text` exists). None means "could not tell",
    never "dead" — see :func:`c_lord.tmux.pane_command_is_dead`.
    """
    if not session_name or not window_name:
        return None
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-t",
                f"{session_name}:{window_name}",
                "-F",
                "#{pane_current_command}",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


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
        is_processing: Callable[[int], bool] | None = None,
    ) -> None:
        self._bot = bot
        self._repo = session_repo
        self._interval = interval_seconds
        # Optional callback: True while a Claude turn is actively running for the
        # thread. Lets the poll keep a thread 🟢 running even when it lands in a
        # brief no-spinner window (session startup, tool gap) instead of rolling
        # the event-driven lamp back to 🟡 waiting (#236). Default no-op keeps
        # the loop self-contained for consumers that don't wire it up.
        self._is_processing: Callable[[int], bool] = is_processing or (lambda _tid: False)
        self._task: asyncio.Task[None] | None = None
        # Per-thread rate-limit backoff: thread_id → monotonic time until next rename is allowed.
        self._rename_backoff: dict[int, float] = {}
        # #277: the first tick after startup must only sync DB state — never
        # rename — so a restart can't burst-PATCH every diverging thread at once
        # and saturate Discord's per-channel rename rate-limit (429). Set False
        # once the first full tick completes; subsequent ticks rename normally.
        self._initial_pass: bool = True

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
            await self._sync_one(record, by_tid, allow_rename=not self._initial_pass)

        # After the first full pass, allow renames on subsequent ticks (#277).
        if self._initial_pass:
            logger.info(
                "thread-state sync: initial pass complete (%d session(s) state-synced, "
                "no renames sent); renames enabled from next tick",
                len(sessions),
            )
            self._initial_pass = False

    async def _sync_one(
        self,
        record,  # SessionRecord
        by_tid: dict[int, dict[str, str]],
        *,
        allow_rename: bool = True,
    ) -> None:
        """Reconcile a single session row with live tmux state.

        When ``allow_rename`` is False, only the DB ``state`` / ``tmux_window_id``
        are synced and no Discord rename is sent — used for the first tick after
        startup to avoid the rename burst that saturates the rate-limit (#277).
        """
        thread_id = record.thread_id
        window_info = by_tid.get(thread_id)

        if window_info is not None:
            window_id = window_info["window_id"] or None

            # Detect fine-grained lamp state from pane content (#120).
            session_name = window_info.get("session_name", "")
            window_name = window_info.get("window_name", "")
            # W<N> follows the stable w{N} name, not the volatile window_index.
            window_number: int | None = parse_work_number(window_name)
            pane_text = await asyncio.to_thread(_capture_pane_text, session_name, window_name)
            new_state = _pane_lamp_state(pane_text)
            # Don't roll an actively-processing thread back to 🟡 just because the
            # poll landed in a brief no-spinner window (startup / tool gap). Only
            # promote waiting→running; error stays error (#236).
            if new_state == "waiting" and self._is_processing(thread_id):
                new_state = "running"
        else:
            new_state = "dead"
            window_id = record.tmux_window_id
            window_number = None

        # Persist state and window-id changes.
        if record.state != new_state:
            await self._repo.set_state(thread_id, new_state)
        if window_id and record.tmux_window_id != window_id:
            await self._repo.set_tmux_window_id(thread_id, window_id)

        # #277: on the first tick after startup, sync DB state only — never
        # rename. A restart resets every in-memory guard, so renaming here would
        # PATCH every diverging thread at once and saturate Discord's rename
        # rate-limit (429 within seconds). The next tick renames diverging
        # threads normally, using the state we just persisted as the baseline.
        if not allow_rename:
            return

        # Without a topic we can't construct a sensible name yet — skip.
        if not record.topic:
            return

        # #414: keep the Issue/PR number in the lamp-sync rename too, otherwise
        # the slow sidebar repaint would drop it from the name.
        # #512: likewise the ``[終了]`` marker. A closed session has no tmux window,
        # so this loop computes state="dead" for it every tick — without the flag
        # it would rebuild the plain name and quietly undo the marker that
        # /close-workspace just applied.
        new_name = build_name(
            record.topic,
            new_state,
            window_number,
            issue_ref=record.issue_ref,
            closed=is_closed(record),
        )

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

        # Persisted (cross-restart) rate-limit backoff (#281): the in-memory
        # _rename_backoff resets on restart, so honour the DB-persisted deadline
        # too. A fresh process forgets it just renamed this channel and would
        # re-PATCH within Discord's ~10-min window (429); the persisted wall-clock
        # deadline survives the restart.
        persisted = getattr(record, "rename_backoff_until", None)
        if persisted:
            with contextlib.suppress(ValueError, TypeError):
                deadline = datetime.datetime.strptime(persisted, _BACKOFF_TS_FORMAT)
                if _now() < deadline:
                    logger.debug(
                        "state-sync: rename skipped thread %d (persisted backoff until %s)",
                        thread_id,
                        persisted,
                    )
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
            await self._persist_backoff(thread_id, None)  # clear stale deadline (#281)
        except discord.HTTPException as exc:
            if exc.status == 429:
                retry_after = float(
                    getattr(exc, "retry_after", _DEFAULT_RENAME_BACKOFF_SECONDS)
                    or _DEFAULT_RENAME_BACKOFF_SECONDS
                )
                self._rename_backoff[thread_id] = asyncio.get_event_loop().time() + retry_after
                await self._persist_backoff(thread_id, retry_after)  # survive restart (#281)
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
            await self._persist_backoff(thread_id, _DEFAULT_RENAME_BACKOFF_SECONDS)  # (#281)
            logger.warning(
                "state-sync: rename timed out for thread %d"
                " (suspected rate-limit), backing off %.0fs",
                thread_id,
                _DEFAULT_RENAME_BACKOFF_SECONDS,
            )

    async def _persist_backoff(self, thread_id: int, retry_after_seconds: float | None) -> None:
        """Persist (or clear) the rename backoff deadline to the DB (#281).

        ``retry_after_seconds`` None clears the deadline; otherwise the deadline
        is ``_now() + retry_after_seconds`` as a wall-clock string. Best-effort:
        a repo without ``set_rename_backoff_until`` (older consumers) is skipped.
        """
        setter = getattr(self._repo, "set_rename_backoff_until", None)
        if setter is None:
            return
        deadline: str | None = None
        if retry_after_seconds is not None:
            deadline = (_now() + datetime.timedelta(seconds=retry_after_seconds)).strftime(
                _BACKOFF_TS_FORMAT
            )
        with contextlib.suppress(Exception):
            await setter(thread_id, deadline)


class MenuWatchdogLoop:
    """Always-on 60s sweep that bridges unresolved TUI menus to Discord (#359).

    Independent of the thread-name lamp (which is off by default, #329): the
    user's ability to see and answer AskUserQuestion/plan menus must not depend
    on an opt-in cosmetic feature.  Iterates every tmux window that carries an
    ``@thread_id`` so it also recovers menus opened before the bot (re)started.

    Ownership filter (#438): ``tmux list-windows -a`` returns every session on
    the (shared) tmux server, including OTHER bots' sessions on the same host.
    Bridging a foreign window would post another bot's menu into a thread we do
    not own.  Each sweep is therefore restricted to windows this bot owns:
      * AC2 — ``@thread_id`` must be a session in our own ``sessions.db``
        (each bot has its own DB, so a foreign thread_id is absent), and
      * AC1 — the window must live in a tmux session this bot manages
        (binding-derived names + the global default).
    ``repo`` defaults to ``None`` for backward compatibility (consumers that
    do not wire it keep the old, unfiltered behaviour); ``setup.py`` always
    passes it so the filter is on by default (zero-config).
    """

    def __init__(
        self,
        bot: Bot,
        *,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        is_processing: Callable[[int], bool] | None = None,
        repo: SessionRepository | None = None,
    ) -> None:
        self._bot = bot
        self._interval = interval_seconds
        self._is_processing: Callable[[int], bool] = is_processing or (lambda _tid: False)
        self._repo = repo
        self._task: asyncio.Task[None] | None = None
        # Per-thread background bridge task — one at a time per thread.
        self._ask_bridges: dict[int, asyncio.Task[None]] = {}

    def start(self) -> None:
        """Spawn the loop task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="menu_watchdog")
        logger.info("Started menu watchdog loop (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        for t in self._ask_bridges.values():
            t.cancel()
        self._ask_bridges.clear()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(min(5.0, self._interval))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("menu watchdog tick raised")
            await asyncio.sleep(self._interval)

    async def tick(self) -> None:
        """One sweep over every tmux window bound to a thread. Public for tests."""
        windows = await asyncio.to_thread(_list_all_windows)
        managed = await self._managed_session_names()
        for w in windows:
            tid = w.get("thread_id") or ""
            if not tid.isdigit():
                continue
            thread_id = int(tid)
            session_name = w.get("session_name", "")
            # #438: skip windows this bot does not own BEFORE capturing the pane
            # (a foreign bot's window on the shared tmux server). This is the
            # whole guard against cross-bot menu bridging.
            if not await self._owns_window(thread_id, session_name, managed):
                continue
            window_name = w.get("window_name", "")
            pane_text = await asyncio.to_thread(_capture_pane_text, session_name, window_name)
            try:
                await self._maybe_bridge_open_menu(thread_id, session_name, window_name, pane_text)
            except Exception:
                logger.exception("menu watchdog failed for thread=%s", tid)

    async def _managed_session_names(self) -> set[str] | None:
        """tmux session names this bot manages, for the #438 ownership filter.

        Returns ``None`` when undeterminable (no ChannelRepoCog) — in that case
        the session-name guard (AC1) is skipped and ownership rests on the DB
        guard (AC2) alone.
        """
        from .cogs.channel_repo import ChannelRepoCog

        cog = self._bot.get_cog("ChannelRepoCog")
        if not isinstance(cog, ChannelRepoCog):
            return None
        with contextlib.suppress(Exception):
            return await cog.managed_session_names()
        return None

    async def _owns_window(
        self, thread_id: int, session_name: str, managed: set[str] | None
    ) -> bool:
        """#438: does this tmux window belong to THIS bot? See class docstring.

        AC2 (DB) is authoritative; AC1 (managed session set) is the second guard.
        Both must hold. A window in a session we do not manage, or for a thread
        absent from our ``sessions.db``, is another bot's — never bridge it.
        """
        # AC2 — the thread must be one of our own sessions.
        if self._repo is not None and await self._repo.get(thread_id) is None:
            return False
        # AC1 — the window must live in a tmux session we manage. ``managed``
        # includes the default session, so unbound-channel windows (#420) for an
        # owned thread still pass. Skip the guard when undeterminable / no name.
        if managed is None or not session_name:
            return True
        return session_name in managed

    async def _maybe_bridge_open_menu(
        self, thread_id: int, session_name: str, window_name: str, pane_text: str
    ) -> None:
        """#359 menu watchdog: bridge an unresolved TUI menu no turn is watching.

        A menu that renders after ``run_claude`` finalized (quiet tool run /
        long thinking trips the stable fallback) has no live bridge: the
        post-turn peek is one-shot and the transcript mirror cannot read the
        ``tool_use`` line until the menu resolves. This sweep-time check is the
        retrying backstop: every tick, an open AskUserQuestion/plan menu with no
        active waiter gets bridged to Discord buttons. ``bridge_pane_ask``'s
        pane watch (#369) ends the bridge when the menu resolves elsewhere, so
        a TUI answer cannot strand the watchdog task.
        """
        # Cheap signature test on the small lamp capture before paying for a
        # full capture + parse.
        from .claude.tmux_runner import _ASK_SIGNATURE, _PLAN_SIGNATURE

        if _ASK_SIGNATURE not in pane_text and _PLAN_SIGNATURE not in pane_text:
            return
        # #510: the signature can outlive claude. tmux-resurrect restores a dead
        # pane's saved screen (``cat <dump>; exec zsh``) after a reboot, so the
        # menu of a session that ended weeks ago is still on display — and
        # bridging it pinged the owner once every 24h forever (the bridge's Esc
        # lands in zsh, so the "menu" never resolves). Ask the process table.
        from .tmux import pane_command_is_dead

        command = await asyncio.to_thread(_pane_foreground_command, session_name, window_name)
        if pane_command_is_dead(command):
            logger.debug(
                "menu watchdog: ignoring menu in dead pane (thread=%d %s:%s cmd=%s)",
                thread_id,
                session_name,
                window_name,
                command,
            )
            return
        # A live turn's poll loop owns menus while it is processing (#166).
        if self._is_processing(thread_id):
            return
        existing = self._ask_bridges.get(thread_id)
        if existing is not None and not existing.done():
            return
        from .discord_ui.ask_bus import ask_bus

        if ask_bus.is_active(thread_id):
            return

        from .claude.tmux_runner import (
            TmuxClaudeRunner,
            _normalize_capture,
            _parse_ask_from_pane,
            _parse_plan_from_pane,
        )

        full = await asyncio.to_thread(_capture_pane_text, session_name, window_name, 120)
        norm = _normalize_capture(full)
        question = _parse_ask_from_pane(norm) or _parse_plan_from_pane(norm)
        if question is None:
            return

        channel = self._bot.get_channel(thread_id)
        if channel is None:
            with contextlib.suppress(Exception):
                channel = await self._bot.fetch_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            return

        from .cogs.channel_repo import ChannelRepoCog

        parent_id = getattr(channel, "parent_id", None) or thread_id
        tmux_manager = None
        channel_cog = self._bot.get_cog("ChannelRepoCog")
        if isinstance(channel_cog, ChannelRepoCog):
            tmux_manager = await channel_cog.resolve_tmux_manager(parent_id)
        if tmux_manager is None:
            tmux_manager = getattr(self._bot, "tmux_manager", None)
        if tmux_manager is None and session_name:
            # #420: A channel without a /clord-init binding resolves no manager,
            # and bot.tmux_manager is unwired (main.py never passes one). But the
            # sweep already located the live tmux session this menu's window lives
            # in (we captured the pane from it), so target that session directly —
            # same construction resolve_tmux_manager itself uses. Without this the
            # watchdog gives up every tick and the tmux→Discord mirror "cuts off":
            # the stranded menu never reaches Discord buttons.
            from .tmux import TmuxSessionManager

            tmux_manager = TmuxSessionManager(session_name=session_name)
        if tmux_manager is None:
            logger.warning("menu watchdog: no tmux manager for thread=%d", thread_id)
            return
        runner = TmuxClaudeRunner(tmux_manager=tmux_manager, thread_id=thread_id)

        # #549: long pre-menu prose (経緯・推し) scrolls off the alternate screen,
        # which keeps no scrollback, so the capture above returns the menu with
        # ``context=""`` — the question then reaches Discord with no decision
        # context, and the prose only appears after the answer, out of order.
        # The poll loop has recovered this since #468 by transiently growing the
        # window (Claude redraws its conversation on SIGWINCH); the watchdog
        # never did, which is exactly the reported #549 case. Same conditions as
        # the poll loop: only when the first read came up empty, so an ordinary
        # menu never pays the resize round-trip.
        if not question.context and hasattr(tmux_manager, "capture_pane_tall"):
            tall = await asyncio.to_thread(tmux_manager.capture_pane_tall, thread_id)
            if isinstance(tall, str) and tall:
                recovered = _parse_ask_from_pane(_normalize_capture(tall)) or _parse_plan_from_pane(
                    _normalize_capture(tall)
                )
                if recovered is not None and recovered.context:
                    question = recovered
                    logger.info(
                        "menu watchdog: recovered pre-menu context via tall capture "
                        "(thread=%d, context_chars=%d)",
                        thread_id,
                        len(recovered.context),
                    )

        from .discord_ui.ask_handler import bridge_pane_ask

        logger.info(
            "menu watchdog: bridging unwatched TUI menu "
            "(thread=%d header=%r context_chars=%d)",
            thread_id,
            question.header,
            len(question.context),
        )
        self._ask_bridges[thread_id] = asyncio.create_task(
            bridge_pane_ask(
                channel,
                question,
                runner,
                ask_repo=getattr(self._bot, "ask_repo", None),
                # #480: watchdog bridges a menu no Discord turn is watching
                # (terminal-driven), so ping the bot owner as the fallback
                # (#525: unless this deployment turned that fallback off).
                notify_user_id=owner_notify_id(self._bot, kind="blocked"),
            ),
            name=f"menu-watchdog-{thread_id}",
        )
