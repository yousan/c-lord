"""Tmux session management for Claude Code sessions.

Uses a single tmux session ``clord`` with per-thread windows (``w1``,
``w2``, ...).  The mapping between thread IDs and window names is
stored in tmux window options (``@thread_id``) so it survives bot restarts.
(Windows created before the prefix was shortened are named ``work{N}`` and
are still recognized — see :func:`parse_work_number`.)

All operations use ``asyncio.to_thread`` for non-blocking execution.
When tmux is not installed, operations degrade gracefully (log warning, skip).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# Discord snowflake IDs are 17–19 digits. Require ≥10 to avoid matching
# unrelated trailing-numeric path components (PIDs, ports, etc.).
_THREAD_ID_FROM_PATH_RE = re.compile(r"(\d{10,})/*$")

logger = logging.getLogger(__name__)

SESSION_NAME = "clord"
# Short window prefix (#356): windows are named ``w1``, ``w2``, … so the tmux
# window list / status bar stays compact (``w73`` instead of ``work73``).
WINDOW_PREFIX = "w"
# Windows created before the prefix was shortened are named ``work{N}``.  We
# keep recognizing that form so already-running windows survive the transition
# (their W<N> Discord label and thread mapping keep working until they are
# naturally recreated). New windows always use ``WINDOW_PREFIX``.
_LEGACY_WINDOW_PREFIX = "work"

# #374: temporary index base used while re-sorting windows. Windows are first
# moved into this (free) high range in the desired order, then ``move-window -r``
# compacts them back from ``base-index``. Far above any realistic window count.
_SORT_TMP_BASE = 9000

# #403: bot sessions are pinned to ``window-size manual`` so the human's SSH
# terminal changing size does not — via the tmux default ``window-size latest``
# — resize every window and SIGWINCH-storm each idle Claude TUI into redrawing
# its bottom status line. That redraw is frequently incomplete for an inactive
# pane, leaving the status block ghosted/duplicated until a full redraw (a
# manual resize / Ctrl-L). New windows are fitted to the attached client (or
# this default when none is attached) at creation, so they still look right and
# then stay fixed. Chosen large enough for a usable Claude TUI.
DEFAULT_MANAGED_WINDOW_SIZE = (160, 40)

# #471: /tmux-screenshot height. capture_screen() only ever sees the *visible*
# window (DEFAULT_MANAGED_WINDOW_SIZE → 40 rows), so a screenshot cut off the
# conversation history. Claude Code runs as a full-screen TUI whose alternate
# screen keeps no scrollback, so the only way to reveal more history is to
# transiently grow the window before capture (SIGWINCH → Claude redraws more of
# the conversation), then restore the exact original size — the same trick as
# capture_pane_tall (#468). This is the default target height in rows; override
# with CLORD_TMUX_SCREENSHOT_ROWS, or set it to 0 to disable the growth and
# capture the current window as-is.
DEFAULT_SCREENSHOT_ROWS = 100
_SCREENSHOT_ROWS_ENV = "CLORD_TMUX_SCREENSHOT_ROWS"

# Effort levels the ``claude --effort`` flag accepts (commander-validated; it
# hard-errors on anything else).  ``ultracode``/``auto`` are real effort levels
# but only settable via ``CLAUDE_CODE_EFFORT_LEVEL`` or the ``/effort`` command,
# not this flag — so they are intentionally excluded here.
_VALID_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

# #147: Claude Code runs with ``editorMode: vim``.  Its input box therefore has
# a vim NORMAL mode in which literal characters (``send-keys -l``) are
# interpreted as editor commands, corrupting the message.  The TUI status bar
# shows ``-- INSERT`` only while in INSERT mode; NORMAL mode simply omits the
# prefix (current Claude Code, v2.1.150, renders NO ``-- NORMAL`` marker).  So
# we detect mode by the presence of ``-- INSERT`` and anchor the NORMAL case on
# the always-present permission/plan status indicators.
_INSERT_MARKER = "-- INSERT"
# Status-bar anchors that prove the pane is sitting at the input prompt (and so
# its vim mode is meaningful).  ``⏵⏵``/``⏸⏸`` are the bypass/plan indicators;
# ``-- NORMAL`` is included for forward-compat in case a future build restores
# the explicit NORMAL marker.
_STATUS_BAR_ANCHORS = ("⏵⏵", "⏸⏸", "-- NORMAL")
# Pause after pressing ``i`` so the TUI commits the INSERT-mode switch before the
# literal text arrives (verified on staging: the switch is near-instant).
_INSERT_SETTLE = 0.15
# #485: pause after Esc-dismissing a stuck menu so the TUI closes it before the
# message is typed (otherwise the text could still land on the closing menu).
_MENU_DISMISS_SETTLE = 0.3

# #503: c-lord is usually the first process on the host to touch tmux, so the
# server it starts inherits c-lord's *own* cgroup. systemd kills a unit's whole
# cgroup on stop, so a plain ``systemctl --user restart c-lord.service`` took
# every tmux session down with it — including the human's unrelated work
# sessions (2026-08-07: 16 sessions lost, twice). Routing the server start
# through ``systemd-run --user`` puts it in its own transient unit instead.
#
# Both properties are required. ``tmux new-session -d`` exits as soon as the
# server has forked, so with systemd's defaults the unit would count as finished
# and the freshly started server would be reaped along with the cgroup.
_SYSTEMD_RUN_ARGS = (
    "systemd-run",
    "--user",
    "--quiet",
    "--description=tmux server (started by c-lord, kept out of its cgroup)",
    "--property=KillMode=process",
    "--property=RemainAfterExit=yes",
    # Unload the unit if it fails, so a bad start does not leave a "failed"
    # entry behind forever. A successful start stays active (RemainAfterExit)
    # and is therefore kept.
    "--collect",
)
# systemd-run does NOT inherit our environment — the transient unit is started
# by the systemd user manager and gets *its* environment. Anything that decides
# which tmux server we end up talking to has to be forwarded explicitly, or the
# unit quietly starts a server somewhere else and our readiness check times out.
# Caught on staging: with TMUX_TMPDIR dropped, the unit hit the default socket
# and died with "duplicate session" while we waited on the isolated one.
_SYSTEMD_RUN_FORWARDED_ENV = ("TMUX_TMPDIR", "PATH")
# systemd-run returns once the transient unit has *started*; the tmux server
# inside it needs a moment more before the session answers. Poll instead of
# sleeping a fixed worst case.
_SERVER_START_TIMEOUT = 5.0
_SERVER_START_POLL = 0.1


def parse_work_number(window_name: str) -> int | None:
    """Extract the ``N`` from a ``w{N}`` (or legacy ``work{N}``) window name.

    Returns ``None`` for windows that don't follow the convention (e.g. the
    session's initial shell window, or a window adopted by dir-match).

    This number is the *stable* window identifier shown as the ``W<N>`` thread
    name prefix. It deliberately is NOT tmux's ``#{window_index}``, which is
    volatile — the index shifts when other windows are killed/renumbered and is
    offset by ``base-index``, so using it would make the Discord ``W<N>`` label
    disagree with the window's own ``w{N}`` name.

    Both the current short prefix (``w{N}``) and the legacy long prefix
    (``work{N}``) are accepted so windows created before the rename keep their
    label across the transition. ``WINDOW_PREFIX`` is checked first; because it
    is a prefix of ``_LEGACY_WINDOW_PREFIX`` (``"w"`` ⊂ ``"work"``), a name like
    ``work73`` falls through to the legacy branch (its ``"ork73"`` suffix isn't
    numeric) and is parsed correctly.
    """
    for prefix in (WINDOW_PREFIX, _LEGACY_WINDOW_PREFIX):
        if window_name.startswith(prefix):
            suffix = window_name[len(prefix) :]
            if suffix.isdigit():
                return int(suffix)
    return None


def _screenshot_rows_from_env() -> int:
    """Read the configured tmux-screenshot height (rows) from the environment.

    ``CLORD_TMUX_SCREENSHOT_ROWS`` overrides :data:`DEFAULT_SCREENSHOT_ROWS`. A
    non-integer or negative value is ignored (falls back to the default) with a
    warning, so a typo can't silently break the taller screenshot. ``0`` is a
    valid, explicit "don't grow the window — capture it as-is".
    """
    raw = os.getenv(_SCREENSHOT_ROWS_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_SCREENSHOT_ROWS
    try:
        rows = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r (not an integer); using default %d",
            _SCREENSHOT_ROWS_ENV,
            raw,
            DEFAULT_SCREENSHOT_ROWS,
        )
        return DEFAULT_SCREENSHOT_ROWS
    if rows < 0:
        logger.warning(
            "Negative %s=%r; using default %d",
            _SCREENSHOT_ROWS_ENV,
            raw,
            DEFAULT_SCREENSHOT_ROWS,
        )
        return DEFAULT_SCREENSHOT_ROWS
    return rows


# #527: tmux hands each command to its server over an imsg capped at
# ``MAX_IMSGSIZE`` (16384 bytes), so a single ``send-keys`` carrying a whole
# prompt is refused with ``command too long`` once it passes ~16KB — measured
# ceiling on tmux 3.4 is 16,335 bytes for a short target name, and the target
# name eats into it.  A 19,852-byte markdown attachment hit exactly this and the
# message never reached Claude.  Chunk far enough below the cap that argv
# overhead (flags, ``session:window``) can never close the gap.
_SEND_KEYS_CHUNK_BYTES = 3000


def _chunk_for_send_keys(text: str, limit: int = _SEND_KEYS_CHUNK_BYTES) -> list[str]:
    """Split *text* into pieces of at most *limit* UTF-8 bytes each.

    Splits only on character boundaries: ``send-keys -l`` writes the bytes
    straight to the pane, so a multi-byte character cut in half would arrive as
    mojibake.  Lossless — ``"".join(_chunk_for_send_keys(t)) == t``.
    """
    if not text:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > limit and current:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(char)
        size += width
    if current:
        chunks.append("".join(current))
    return chunks


# #529: the cold-start prompt used to be typed at the pane's shell prompt as
# part of the command line. oh-my-zsh binds ``url-quote-magic`` to
# ``self-insert``, so zsh backslash-escaped ``?``/``=``/``&`` inside any URL as
# it was typed and Claude received a URL that 404s. Handing the prompt over in a
# file keeps it out of the line editor entirely — and keeps the command short
# no matter how long the prompt is.
_PROMPT_FILE_PREFIX = "clord-prompt-"
_PROMPT_FILE_MAX_AGE = 3600.0  # seconds; a file older than this was abandoned


def _prompt_file_dir() -> Path:
    """Directory holding hand-off prompt files (created 0700 on first use)."""
    directory = Path(tempfile.gettempdir()) / "clord-prompts"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def _sweep_stale_prompt_files(directory: Path) -> None:
    """Delete abandoned prompt files — they hold whatever the user typed.

    A file is only abandoned if its turn never reached the ``rm`` in the
    command (pane died between write and run). Best effort; never raises.
    """
    cutoff = time.time() - _PROMPT_FILE_MAX_AGE
    try:
        for path in directory.glob(f"{_PROMPT_FILE_PREFIX}*"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        return


def _write_prompt_file(prompt: str) -> Path:
    """Write *prompt* to an owner-only file and return its path.

    Raises OSError; the caller falls back to the inline command line, because
    a mangled URL beats losing the turn.
    """
    directory = _prompt_file_dir()
    _sweep_stale_prompt_files(directory)
    fd, name = tempfile.mkstemp(prefix=_PROMPT_FILE_PREFIX, suffix=".txt", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    path = Path(name)
    path.chmod(0o600)
    return path


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return the result (never raises on non-zero exit)."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
    )


def _pane_in_insert_mode(pane_text: str) -> bool | None:
    """Return True if the input box is in vim INSERT mode, False for NORMAL.

    Returns ``None`` when the mode cannot be determined (no status bar in the
    captured frame — e.g. mid-redraw or while Claude is generating).  Callers
    should treat ``None`` as "leave input untouched" to avoid the double-``i``
    regression (#147 AC2).

    Only the bottom status zone is inspected, so a stale ``-- INSERT`` lingering
    in scrollback above a current NORMAL status bar does not win.
    """
    lines = [ln for ln in pane_text.splitlines() if ln.strip()]
    if not lines:
        return None
    zone = "\n".join(lines[-8:])
    if _INSERT_MARKER in zone:
        return True
    if any(anchor in zone for anchor in _STATUS_BAR_ANCHORS):
        return False
    return None


def _pane_has_open_menu(pane_text: str) -> bool:
    """True if the pane shows an open AskUserQuestion / plan-approval menu (#485).

    Used by :meth:`TmuxSessionManager.send_input` to dismiss a stuck menu before
    typing, so a plain reply can never select the highlighted option. The pane
    parsers live in ``claude.tmux_runner`` which imports this module, so they are
    imported lazily here to avoid a circular import.
    """
    try:
        from .claude.tmux_runner import (
            _normalize_capture,
            _parse_ask_from_pane,
            _parse_plan_from_pane,
        )
    except ImportError:  # pragma: no cover - defensive
        return False
    norm = _normalize_capture(pane_text)
    return _parse_ask_from_pane(norm) is not None or _parse_plan_from_pane(norm) is not None


def pane_command_is_dead(command: object) -> bool:
    """True only when *command* positively shows the pane is NOT running claude (#510).

    A pane whose foreground process is a shell can still *show* an open menu:
    tmux-resurrect restores the saved screen contents (``cat <dump>; exec zsh``)
    without restoring claude, so a reboot leaves a corpse that parses exactly
    like a live AskUserQuestion. Detecting that needs the process, not the text.

    Anything unreadable (None, empty, a non-str from a mock/stub) is treated as
    UNKNOWN → False, never "dead": a tmux hiccup must not silence a real
    question. Same asymmetry as the empty-capture rule in #485.
    """
    return isinstance(command, str) and bool(command.strip()) and "claude" not in command.lower()


def _tmux_available() -> bool:
    """Return True if tmux is installed and accessible."""
    result = _run(["tmux", "-V"])
    return result.returncode == 0


class TmuxSessionManager:
    """Manages tmux windows for Claude Code Discord threads.

    One global tmux session ``clord`` holds all threads.  Each thread gets
    a window named ``w{N}`` with the thread ID stored in the ``@thread_id``
    window option.
    """

    def __init__(
        self,
        session_name: str | None = None,
        mapping_path: str | None = None,
        screenshot_rows: int | None = None,
    ) -> None:
        self.session_name: str = session_name or SESSION_NAME
        # #471: target height (rows) for /tmux-screenshot. Defaults to the env
        # (CLORD_TMUX_SCREENSHOT_ROWS) / DEFAULT_SCREENSHOT_ROWS so every manager
        # picks up the config with no wiring; an explicit arg wins (tests / API).
        self.screenshot_rows: int = (
            screenshot_rows if screenshot_rows is not None else _screenshot_rows_from_env()
        )
        self._available: bool | None = None
        self._next_work_id: int = 1
        self._thread_to_window: dict[int, str] = {}
        # Serializes window creation + the post-create sort (#374) so concurrent
        # create_session() calls (one per Discord thread, run in the asyncio
        # thread pool) don't interleave their move-window operations.
        self._lock = threading.Lock()
        # Persistent thread→window mapping file. Survives tmux restarts; used
        # as fallback in _rebuild_mapping when pane has cd'd away (issue #113).
        if mapping_path is not None:
            self._mapping_path: str = mapping_path
        else:
            cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "c-lord")
            os.makedirs(cache_dir, exist_ok=True)
            self._mapping_path = os.path.join(cache_dir, f"{self.session_name}-window-map.json")

    def _check_available(self) -> bool:
        """Check and cache tmux availability."""
        if self._available is None:
            self._available = _tmux_available()
            if not self._available:
                logger.warning("tmux is not installed — tmux features disabled")
        return self._available

    # ── Helpers ────────────────────────────────────────────────────────

    def _ensure_window_size_manual(self) -> None:
        """Pin the session to ``window-size manual`` (#403).

        Under the tmux default ``latest``, the human's terminal changing size
        resizes every window and ghosts each idle Claude TUI's status line.
        ``manual`` makes the windows immune to client-size changes. Idempotent.
        """
        if not self._check_available():
            return
        _run(["tmux", "set-option", "-t", self.session_name, "window-size", "manual"])

    def _current_client_size(self) -> tuple[int, int] | None:
        """Return ``(width, height)`` of an attached client, or ``None``.

        ``None`` when no client is attached or tmux is unavailable.
        """
        if not self._check_available():
            return None
        result = _run(
            [
                "tmux",
                "list-clients",
                "-t",
                self.session_name,
                "-F",
                "#{client_width} #{client_height}",
            ]
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    return int(parts[0]), int(parts[1])
                except ValueError:
                    continue
        return None

    def _fit_window_to_client(self, window_name: str) -> None:
        """Size a (manual) window to the attached client, or a default (#403).

        Called right after a window is created — while it is empty, so the
        resize cannot ghost a running TUI — so the window looks right when first
        viewed and then stays fixed (immune to later terminal-size changes).
        """
        if not self._check_available():
            return
        width, height = self._current_client_size() or DEFAULT_MANAGED_WINDOW_SIZE
        _run(
            [
                "tmux",
                "resize-window",
                "-t",
                f"{self.session_name}:{window_name}",
                "-x",
                str(width),
                "-y",
                str(height),
            ]
        )

    def _ensure_session(self) -> bool:
        """Ensure the global ``clord`` tmux session exists.

        Returns True if the session is available (already existed or was
        created), False on failure.
        """
        result = _run(["tmux", "has-session", "-t", self.session_name])
        if result.returncode == 0:
            self._ensure_window_size_manual()  # #403
            return True

        if not self._create_global_session():
            return False

        logger.info("Created global tmux session: %s", self.session_name)
        self._ensure_window_size_manual()  # #403
        return True

    def _create_global_session(self) -> bool:
        """Create the global session with a temporary first window.

        When no tmux server is running yet, *we* are about to start one — and a
        server started as our child inherits our cgroup, so stopping c-lord's
        systemd unit would kill every tmux session on the host. Launch it in its
        own transient unit instead (#503). Once a server exists this does not
        apply: the new session is created by that server, in *its* cgroup.
        """
        new_session = ["tmux", "new-session", "-d", "-s", self.session_name]

        # Short-circuits to the plain path when a server is already up. If the
        # detached start fails (no systemd — container / CI — or the unit did not
        # come up) we fall through deliberately: a session in the wrong cgroup
        # still beats no session, since the thread would otherwise stall with no
        # reply at all.
        if not self._tmux_server_running() and self._start_server_detached(new_session):
            return True

        result = _run(new_session)
        if result.returncode != 0:
            logger.warning(
                "Failed to create tmux session %s: %s",
                self.session_name,
                result.stderr.strip(),
            )
            return False
        return True

    @staticmethod
    def _tmux_server_running() -> bool:
        """True when a tmux server is already accepting connections."""
        return _run(["tmux", "list-sessions"]).returncode == 0

    def _start_server_detached(self, new_session: list[str]) -> bool:
        """Start the tmux server outside our own cgroup via systemd-run (#503).

        Returns True only once the session actually answers, so a systemd-run
        that starts the unit but whose tmux then dies is reported as failure and
        the caller can fall back.
        """
        forwarded = [
            f"--setenv={name}={os.environ[name]}"
            for name in _SYSTEMD_RUN_FORWARDED_ENV
            if os.environ.get(name)
        ]
        result = _run([*_SYSTEMD_RUN_ARGS, *forwarded, "--", *new_session])
        if result.returncode == 0:
            deadline = time.monotonic() + _SERVER_START_TIMEOUT
            while time.monotonic() < deadline:
                if _run(["tmux", "has-session", "-t", self.session_name]).returncode == 0:
                    logger.info(
                        "Started tmux server in its own systemd unit — a c-lord "
                        "restart will no longer kill tmux sessions (#503)"
                    )
                    return True
                time.sleep(_SERVER_START_POLL)

        logger.warning(
            "Could not start the tmux server in its own systemd unit (%s); falling "
            "back to starting it in c-lord's cgroup — restarting c-lord will kill "
            "every tmux session on this host (#503)",
            result.stderr.strip() or f"exit {result.returncode}",
        )
        return False

    def _find_window_for_thread(self, thread_id: int) -> str | None:
        """Find the window name for a thread by checking ``@thread_id`` options.

        Returns the window name or None if not found.
        """
        # Check in-memory cache first
        cached = self._thread_to_window.get(thread_id)
        if cached is not None:
            # Verify the window still exists
            result = _run(
                [
                    "tmux",
                    "show-option",
                    "-w",
                    "-v",
                    "-t",
                    f"{self.session_name}:{cached}",
                    "@thread_id",
                ]
            )
            if result.returncode == 0 and result.stdout.strip() == str(thread_id):
                return cached
            # Stale cache entry. Use pop(): capture_pane runs in a thread executor
            # and is called many times per turn, so a concurrent call may have
            # already evicted this key — a bare ``del`` raised KeyError (#410).
            self._thread_to_window.pop(thread_id, None)

        # Fallback: scan all windows
        self._rebuild_mapping()
        return self._thread_to_window.get(thread_id)

    def _next_window_name(self) -> str:
        """Generate the next ``w{N}`` name and increment the counter."""
        name = f"{WINDOW_PREFIX}{self._next_work_id}"
        self._next_work_id += 1
        return name

    def _rebuild_mapping(self) -> None:
        """Rebuild ``_thread_to_window`` from live tmux state.

        Primary key is the ``@thread_id`` window option. When that option
        is missing — which happens after a tmux server restart, because
        tmux-resurrect (and similar) restore window names but not user
        options — fall back to extracting the thread_id from the pane's
        current path (``<session_dir_base>/<thread_id>``) and repair the
        ``@thread_id`` option so future lookups stay cheap. (Issue #69.)

        Several windows can claim the same thread — tmux-resurrect restores a
        stale window next to the live one, both sitting in the thread's session
        dir. Such a conflict is resolved in favour of the window actually
        running Claude, never by list order (issue #501).

        Also updates ``_next_work_id`` to be one past the highest existing
        work window number.
        """
        # #485: build a fresh map locally and swap it in atomically at the end,
        # instead of clear()+repopulate in place. The old in-place rebuild left
        # ``_thread_to_window`` momentarily EMPTY, so concurrent callers (capture
        # and send_keys run in thread executors) read ``None`` for a live window
        # and mis-fired — dropped keystrokes, and a bridge falsely concluding the
        # AskUserQuestion menu had resolved. A local build + single assignment
        # means readers only ever observe a complete map (old or new, never
        # partial). Reproduced in tests/test_tmux_mapping_race.py.
        new_map: dict[int, str] = {}

        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_name}\t#{pane_current_path}",
            ]
        )
        if result.returncode != 0:
            # Keep the current (complete) mapping rather than wiping it to empty.
            return

        # Pass 0: restore from persistent mapping file for windows whose
        # @thread_id was cleared and whose pane has cd'd away (issue #113 Fix-B).
        # Must run after we know the session exists but before the window scan,
        # so that subsequent @thread_id reads in the scan pick up the repaired values.
        self._load_from_mapping_file(new_map)

        # #501: collect the claims first instead of writing straight into the
        # map. The old loop assigned as it walked, so when two windows claimed
        # one thread the LAST in index order silently won — binding the thread
        # to a dead shell while Claude ran in the first. Gathering first lets
        # _resolve_claim() pick by evidence (who is running Claude) instead.
        max_id = 0
        opt_claims: dict[int, list[str]] = {}
        path_claims: dict[int, list[tuple[str, str]]] = {}

        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t", 1)
            window_name = parts[0]
            pane_path = parts[1] if len(parts) > 1 else ""
            if not window_name:
                continue

            opt_result = _run(
                [
                    "tmux",
                    "show-option",
                    "-w",
                    "-v",
                    "-t",
                    f"{self.session_name}:{window_name}",
                    "@thread_id",
                ]
            )
            if opt_result.returncode == 0 and opt_result.stdout.strip().isdigit():
                opt_claims.setdefault(int(opt_result.stdout.strip()), []).append(window_name)
            elif pane_path:
                m = _THREAD_ID_FROM_PATH_RE.search(pane_path)
                if m:
                    path_claims.setdefault(int(m.group(1)), []).append((window_name, pane_path))

            # Count both ``w{N}`` and legacy ``work{N}`` windows toward the high
            # watermark so numbering stays monotonic across the prefix rename.
            n = parse_work_number(window_name)
            if n is not None:
                max_id = max(max_id, n)

        # Windows that already carry @thread_id win over path-derived guesses.
        for thread_id, windows in opt_claims.items():
            new_map[thread_id] = self._resolve_claim(thread_id, windows, clear_losers=True)

        # Path fallback (#69) — only for threads no window has claimed outright,
        # so a stale pane sitting in the session dir can never steal a thread
        # from the window that owns the option (#501).
        for thread_id, matches in path_claims.items():
            if thread_id in opt_claims:
                logger.debug(
                    "Ignoring path-derived claim(s) %s for thread %d — already owned by %s",
                    ", ".join(w for w, _ in matches),
                    thread_id,
                    new_map[thread_id],
                )
                continue
            winner = self._resolve_claim(thread_id, [w for w, _ in matches], clear_losers=False)
            pane_path = next(p for w, p in matches if w == winner)
            _run(
                [
                    "tmux",
                    "set-option",
                    "-w",
                    "-t",
                    f"{self.session_name}:{winner}",
                    "@thread_id",
                    str(thread_id),
                ]
            )
            new_map[thread_id] = winner
            logger.info(
                "Recovered thread_id %d for window %s from pane path %s",
                thread_id,
                winner,
                pane_path,
            )

        # Atomic swap (#485): a single rebinding, so no reader ever sees a
        # partially-built map. dict assignment is atomic under CPython's GIL.
        self._thread_to_window = new_map
        self._next_work_id = max_id + 1

    def _window_has_claude(self, window_name: str) -> bool:
        """True when *window_name*'s pane runs ``claude`` in the foreground."""
        result = _run(
            [
                "tmux",
                "list-panes",
                "-t",
                f"{self.session_name}:{window_name}",
                "-F",
                "#{pane_current_command}",
            ]
        )
        if result.returncode != 0:
            return False
        return "claude" in result.stdout.strip().lower()

    def _resolve_claim(self, thread_id: int, windows: list[str], *, clear_losers: bool) -> str:
        """Pick which of *windows* owns *thread_id* when several claim it.

        Preference order: the window actually running Claude, then the window
        the map already points at (continuity across rebuilds), then first in
        list order. Index order alone is *not* a signal — in #501 the dead
        window happened to sort last and won every rebuild.

        With ``clear_losers`` the runners-up have their ``@thread_id`` unset so
        the conflict self-heals, but only when Claude positively identified the
        winner. Without that evidence we leave tmux untouched: a freshly
        created window whose Claude has not started yet must not be stripped.
        """
        if len(windows) == 1:
            return windows[0]

        live = [w for w in windows if self._window_has_claude(w)]
        if live:
            winner, reason = live[0], "running claude"
        elif (known := self._thread_to_window.get(thread_id)) in windows:
            winner, reason = known, "already bound"
        else:
            winner, reason = windows[0], "first in list order"

        logger.warning(
            "Duplicate @thread_id %d claimed by %s; binding to %s (%s)",
            thread_id,
            ", ".join(windows),
            winner,
            reason,
        )

        if clear_losers and live:
            for loser in windows:
                if loser == winner:
                    continue
                _run(
                    [
                        "tmux",
                        "set-option",
                        "-uw",
                        "-t",
                        f"{self.session_name}:{loser}",
                        "@thread_id",
                    ]
                )
                logger.info("Cleared stale @thread_id %d from window %s", thread_id, loser)

        return winner

    def _save_mapping(self) -> None:
        """Persist the current thread→window mapping to disk (issue #113).

        No-op when mapping_path is empty (disabled or test mode).
        """
        if not self._mapping_path:
            return
        try:
            data = {str(tid): win for tid, win in self._thread_to_window.items()}
            with open(self._mapping_path, "w") as f:
                json.dump(data, f)
        except OSError as exc:
            logger.warning("Failed to save window mapping to %s: %s", self._mapping_path, exc)

    def _load_from_mapping_file(self, target: dict[int, str]) -> None:
        """Restore @thread_id from the persistent mapping file into *target*.

        Called at the start of _rebuild_mapping to recover mappings that were
        cleared by a tmux restart, even when the pane has cd'd away from the
        session directory (issue #113 Fix-B). Populates the *target* map the
        caller is building (#485: rebuild is now build-local-then-swap), not
        ``self._thread_to_window`` directly.
        No-op when mapping_path is empty (disabled or test mode).
        """
        if not self._mapping_path:
            return
        try:
            with open(self._mapping_path) as f:
                data: dict[str, str] = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        for tid_str, window_name in data.items():
            if not tid_str.isdigit():
                continue
            thread_id = int(tid_str)
            if thread_id in target:
                continue  # already resolved by @thread_id option or path regex

            # Verify the window still exists in the session
            list_result = _run(
                [
                    "tmux",
                    "list-windows",
                    "-t",
                    self.session_name,
                    "-F",
                    "#{window_name}",
                ]
            )
            if list_result.returncode != 0:
                return
            existing_windows = {
                line.split("\t", 1)[0] for line in list_result.stdout.splitlines() if line
            }
            if window_name not in existing_windows:
                continue

            # Check if @thread_id is already set (another thread adopted this window)
            opt_result = _run(
                [
                    "tmux",
                    "show-option",
                    "-w",
                    "-v",
                    "-t",
                    f"{self.session_name}:{window_name}",
                    "@thread_id",
                ]
            )
            if opt_result.returncode == 0 and opt_result.stdout.strip():
                continue  # window already has a different @thread_id

            # Restore @thread_id
            _run(
                [
                    "tmux",
                    "set-option",
                    "-w",
                    "-t",
                    f"{self.session_name}:{window_name}",
                    "@thread_id",
                    str(thread_id),
                ]
            )
            target[thread_id] = window_name
            logger.info(
                "Restored thread_id %d for window %s from mapping file",
                thread_id,
                window_name,
            )

    def _find_window_by_working_dir(self, working_dir: str) -> str | None:
        """Return the first window name whose pane_current_path matches working_dir.

        Pre-creation guard for create_session — prevents twin windows for the
        same session directory when @thread_id options are cleared on tmux
        restart. (Issue #111.)
        """
        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_name}\t#{pane_current_path}",
            ]
        )
        if result.returncode != 0:
            return None
        target = working_dir.rstrip("/")
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                pane_path = parts[1].rstrip("/")
                # Exact match (pane still at session dir) OR pane is inside a
                # subdirectory of working_dir (Claude cd'd into a subdir — Fix-A
                # for issue #113).
                if pane_path == target or pane_path.startswith(target + "/"):
                    return parts[0]
        return None

    def _find_window_in_other_sessions(
        self, thread_id: int, working_dir: str
    ) -> tuple[str, str, str] | None:
        """Find this thread's window living in a *different* tmux session (#427).

        Returns ``(session_name, window_id, window_name)`` or None. The
        ``window_id`` (``@123``) is what callers must address: it is unique
        server-wide and survives renames and moves, whereas ``session:name`` is
        ambiguous the moment two windows share a name — which is exactly what
        happened on staging, where the move failed with ``can't find window``.

        Path equality is the safety anchor, not ``@thread_id``: a parallel
        c-lord instance (staging) can legitimately hold a window for the same
        Discord thread, but it runs out of its own session-dir base so the pane
        path never collides. ``@thread_id`` only breaks ties — several of the
        windows this has to find lost that option to a tmux restart and are
        identifiable by path alone.
        """
        result = _run(
            [
                "tmux",
                "list-windows",
                "-a",
                "-F",
                "#{session_name}\t#{window_id}\t#{window_name}\t"
                "#{@thread_id}\t#{pane_current_path}",
            ]
        )
        if result.returncode != 0:
            return None

        target = working_dir.rstrip("/")
        fallback: tuple[str, str, str] | None = None
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 5:
                continue
            session, window_id, window, tid, pane_path = parts
            if session == self.session_name or not window_id:
                continue
            pane_path = pane_path.rstrip("/")
            if pane_path != target and not pane_path.startswith(target + "/"):
                continue
            if tid == str(thread_id):
                return session, window_id, window
            if fallback is None:
                fallback = (session, window_id, window)
        return fallback

    def _adopt_window_from_other_session(self, thread_id: int, working_dir: str) -> str | None:
        """Move this thread's existing window here from another session (#427).

        ``resolve_tmux_manager`` now honours thread bindings, so a thread whose
        repo differs from its channel's resolves to a *different* session than
        the one its window was created in. Creating a fresh window there would
        leave the original running: two Claude processes on one checkout, one of
        them invisible to Discord. Move the window instead — the conversation,
        the pane and its scrollback all come along.

        Every tmux op targets the immutable ``window_id``; the rename to this
        session's ``w{N}`` scheme happens *after* the move, so the new name only
        has to be free in the destination.

        Returns the window's new name, or None when there is nothing to adopt or
        the move failed.
        """
        found = self._find_window_in_other_sessions(thread_id, working_dir)
        if found is None:
            return None
        src_session, window_id, src_name = found

        # Tag before moving: windows that lost @thread_id to a tmux restart were
        # matched by path, and later lookups need the option to be there.
        _run(["tmux", "set-option", "-w", "-t", window_id, "@thread_id", str(thread_id)])

        result = _run(
            [
                "tmux",
                "move-window",
                # -d: don't steal the attached client's focus (same reason as
                # the -d on new-window, #374).
                "-d",
                "-s",
                window_id,
                "-t",
                f"{self.session_name}:",
            ]
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to move window %s (%s:%s) -> %s: %s",
                window_id,
                src_session,
                src_name,
                self.session_name,
                result.stderr.strip(),
            )
            return None

        new_name = self._next_window_name()
        _run(["tmux", "rename-window", "-t", window_id, new_name])

        self._thread_to_window[thread_id] = new_name
        self._save_mapping()
        logger.info(
            "Adopted tmux window %s (%s:%s) -> %s:%s (thread=%d, dir=%s)",
            window_id,
            src_session,
            src_name,
            self.session_name,
            new_name,
            thread_id,
            working_dir,
        )
        return new_name

    # ── Public API ────────────────────────────────────────────────────

    def create_session(self, thread_id: int, working_dir: str) -> str:
        """Create a tmux window for a thread inside the global session.

        Returns the window name.  Re-uses an existing window if one is
        already mapped to this thread.
        """
        if not self._check_available():
            return f"{WINDOW_PREFIX}0"

        # Check for existing window
        existing = self._find_window_for_thread(thread_id)
        if existing is not None:
            logger.debug("tmux window already exists for thread %d: %s", thread_id, existing)
            return existing

        # Serialize the create-and-sort critical section (#374) so concurrent
        # create_session() calls don't interleave their move-window ops.
        with self._lock:
            if not self._ensure_session():
                return f"{WINDOW_PREFIX}0"

            # Guard: if any window is already sitting in working_dir, adopt it
            # instead of creating a duplicate. This covers the post-tmux-restart
            # case where @thread_id options were cleared but the pane hasn't moved.
            # (Issue #111.)  Adoption reuses an existing window, so no new window
            # is added and no re-sort is needed.
            adopted = self._find_window_by_working_dir(working_dir)
            if adopted is not None:
                _run(
                    [
                        "tmux",
                        "set-option",
                        "-w",
                        "-t",
                        f"{self.session_name}:{adopted}",
                        "@thread_id",
                        str(thread_id),
                    ]
                )
                self._thread_to_window[thread_id] = adopted
                logger.info(
                    "Adopted window %s for thread %d by dir match: %s",
                    adopted,
                    thread_id,
                    working_dir,
                )
                self._save_mapping()
                return adopted

            # #427: the thread may already own a window in *another* session
            # (its repo binding changed after that window was created). Move it
            # here instead of starting a second Claude on the same checkout.
            migrated = self._adopt_window_from_other_session(thread_id, working_dir)
            if migrated is not None:
                self._sort_windows_unlocked()
                return migrated

            window_name = self._next_window_name()

            result = _run(
                [
                    "tmux",
                    "new-window",
                    # -d: create detached so a new thread doesn't steal the
                    # attached user's tmux focus (#374).
                    "-d",
                    "-t",
                    self.session_name,
                    "-n",
                    window_name,
                    "-c",
                    working_dir,
                ]
            )
            if result.returncode != 0:
                logger.warning(
                    "Failed to create tmux window %s: %s",
                    window_name,
                    result.stderr.strip(),
                )
                return window_name

            # Store thread_id as a window option
            _run(
                [
                    "tmux",
                    "set-option",
                    "-w",
                    "-t",
                    f"{self.session_name}:{window_name}",
                    "@thread_id",
                    str(thread_id),
                ]
            )

            # Fit the new (manual-sized) window to the attached client while it
            # is still empty, so it looks right and then stays fixed (#403).
            self._fit_window_to_client(window_name)

            self._thread_to_window[thread_id] = window_name
            self._save_mapping()
            # Keep the session ordered by window number (#374). The new window
            # was inserted at the lowest free index (tmux default), so it may be
            # out of order; re-sort restores ascending w{N} order.
            self._sort_windows_unlocked()
            logger.info(
                "Created tmux window: %s (thread=%d, dir=%s)",
                window_name,
                thread_id,
                working_dir,
            )
            return window_name

    def _sort_windows(self) -> None:
        """Re-sort the session's windows by number (thread-safe wrapper).

        Acquires the manager lock; safe to call from outside the create path
        (e.g. a future manual ``/sort`` command).
        """
        with self._lock:
            self._sort_windows_unlocked()

    def _sort_windows_unlocked(self) -> None:
        """Reorder the session's windows into ascending ``w{N}`` order.

        Numbered windows (``w{N}`` / legacy ``work{N}``) come first in ascending
        numeric order; windows without a parseable number (the initial shell,
        manually-created or adopted windows) are pushed to the end, preserving
        their relative order.

        Only tmux window *indices* change — window names, the ``@thread_id``
        option and the panes are untouched (the bot identifies windows by
        ``@thread_id`` / name, not index, so this is invisible to it). This is a
        purely cosmetic ordering for the human watching ``tmux attach`` (#374).

        No-op when tmux is unavailable, when there are ≤1 windows, or when the
        windows are already sorted (so no needless ``move-window`` churn).

        The caller must hold ``self._lock``; concurrent re-sorts would interleave
        their ``move-window`` operations and corrupt the layout. The sort key is
        deliberately factored out (``_window_sort_key``) so other orderings
        (by activity, by log length, a manual ``/sort <key>``) can be added later
        — see ``docs/specs/tmux-window-ordering.md``.
        """
        if not self._check_available():
            return

        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_id}\t#{window_name}\t#{window_active}",
            ]
        )
        if result.returncode != 0:
            return

        entries: list[tuple[str, str]] = []
        active_id: str | None = None
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t", 2)
            window_id = parts[0]
            window_name = parts[1] if len(parts) > 1 else ""
            if not window_id:
                continue
            entries.append((window_id, window_name))
            if len(parts) > 2 and parts[2] == "1":
                active_id = window_id

        if len(entries) <= 1:
            return

        ordered = [entry for _, entry in sorted(enumerate(entries), key=self._window_sort_key)]
        if ordered == entries:
            return  # already sorted — avoid move-window churn

        # Park every window in a free high-index range in the desired order,
        # then renumber the session to compact them back from base-index.
        for offset, (window_id, _name) in enumerate(ordered):
            _run(
                [
                    "tmux",
                    "move-window",
                    "-s",
                    window_id,
                    "-t",
                    f"{self.session_name}:{_SORT_TMP_BASE + offset}",
                ]
            )
        _run(["tmux", "move-window", "-r", "-t", self.session_name])

        # Restore the active window: parking/renumbering resets the session's
        # current window to the base index, which would yank an attached user's
        # view away from what they were watching — defeating the ``-d`` focus
        # guarantee (#374). Re-select the window that was active before the sort.
        if active_id is not None:
            _run(["tmux", "select-window", "-t", active_id])

    @staticmethod
    def _window_sort_key(item: tuple[int, tuple[str, str]]) -> tuple[int, int]:
        """Sort key for :meth:`_sort_windows_unlocked`.

        ``item`` is ``(original_index, (window_id, window_name))``. Numbered
        windows sort first (group 0) by their number; unnumbered windows sort
        last (group 1) keeping their original relative order.
        """
        orig_index, (_window_id, window_name) = item
        number = parse_work_number(window_name)
        if number is not None:
            return (0, number)
        return (1, orig_index)

    def remap_window(self, thread_id: int, window_name: str) -> bool:
        """Remap an existing tmux window to a new thread ID.

        Updates the ``@thread_id`` window option and the in-memory cache.
        Any previous thread mapping to this window is removed.

        Returns True on success, False if the window does not exist or
        tmux is unavailable.
        """
        if not self._check_available():
            return False

        # Check the window exists via list-windows. We cannot use
        # ``show-option -w @thread_id`` because tmux returns rc=1 for both
        # "window missing" and "option unset" — the latter is the normal
        # case for windows created manually with ``tmux new-window`` (issue #37).
        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_name}",
            ]
        )
        if result.returncode != 0:
            logger.debug("remap_window: session %s not found", self.session_name)
            return False
        existing = {line for line in result.stdout.splitlines() if line}
        if window_name not in existing:
            logger.debug("remap_window: window %s not found", window_name)
            return False

        # Update the @thread_id option
        _run(
            [
                "tmux",
                "set-option",
                "-w",
                "-t",
                f"{self.session_name}:{window_name}",
                "@thread_id",
                str(thread_id),
            ]
        )

        # Update cache: remove any old thread→window mapping for this window.
        # pop() (not del) for the same reason as #410 — a concurrent capture_pane
        # call may have evicted one of these keys between the comprehension and here.
        old_threads = [tid for tid, wname in self._thread_to_window.items() if wname == window_name]
        for tid in old_threads:
            self._thread_to_window.pop(tid, None)
        self._thread_to_window[thread_id] = window_name

        logger.info("Remapped window %s → thread %d", window_name, thread_id)
        return True

    def get_window_info(self, thread_id: int) -> tuple[str, int | None] | None:
        """Return ``(window_id, work_number)`` for the thread, or None.

        ``window_id`` is tmux's internal immutable id (e.g. ``@7``).
        ``work_number`` is the ``N`` in the window's ``w{N}`` name (or legacy
        ``work{N}``) — the stable identifier shown as the ``W<N>`` thread-name
        prefix. It is ``None`` for windows that don't follow the convention.

        Note: this is intentionally NOT tmux's volatile ``#{window_index}``.
        See :func:`parse_work_number` for why the stable name is used instead.

        Returns None if tmux is unavailable or no window is mapped to
        the thread.
        """
        if not self._check_available():
            return None
        window_name = self._find_window_for_thread(thread_id)
        if window_name is None:
            return None
        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_name}\t#{window_id}",
            ]
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[0] == window_name:
                return parts[1], parse_work_number(window_name)
        return None

    def list_windows_full(self) -> list[dict[str, str]]:
        """Return one dict per window with name / window_id / window_index / @thread_id.

        Used by the state-sync loop to cross-reference live tmux state
        with DB rows.  Returns an empty list when tmux is unavailable
        or the session does not exist.
        """
        if not self._check_available():
            return []
        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_name}\t#{window_id}\t#{window_index}\t#{@thread_id}",
            ]
        )
        if result.returncode != 0:
            return []
        windows: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            windows.append(
                {
                    "window_name": parts[0],
                    "window_id": parts[1],
                    "window_index": parts[2],
                    "thread_id": parts[3] if len(parts) > 3 else "",
                }
            )
        return windows

    def session_exists(self, thread_id: int) -> bool:
        """Return True if a tmux window exists for the given thread."""
        if not self._check_available():
            return False

        return self._find_window_for_thread(thread_id) is not None

    def kill_session(self, thread_id: int) -> bool:
        """Kill the tmux window for a thread. Returns True if killed."""
        if not self._check_available():
            return False

        window_name = self._find_window_for_thread(thread_id)
        if window_name is None:
            logger.debug("No tmux window found for thread %d", thread_id)
            return False

        result = _run(
            [
                "tmux",
                "kill-window",
                "-t",
                f"{self.session_name}:{window_name}",
            ]
        )
        if result.returncode == 0:
            self._thread_to_window.pop(thread_id, None)
            self._save_mapping()
            logger.info("Killed tmux window: %s (thread=%d)", window_name, thread_id)
            return True
        else:
            logger.debug("tmux window %s not found or already dead", window_name)
            return False

    def list_sessions(self) -> list[dict[str, str]]:
        """List all windows in the ``clord`` tmux session.

        Returns a list of dicts with ``window_name``, ``working_dir``, and
        ``thread_id`` keys.
        """
        if not self._check_available():
            return []

        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_name}:#{pane_current_path}",
            ]
        )
        if result.returncode != 0:
            return []

        windows: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split(":", 1)
            window_name = parts[0]
            working_dir = parts[1] if len(parts) > 1 else ""

            # Read the @thread_id option
            opt_result = _run(
                [
                    "tmux",
                    "show-option",
                    "-w",
                    "-v",
                    "-t",
                    f"{self.session_name}:{window_name}",
                    "@thread_id",
                ]
            )
            tid = opt_result.stdout.strip() if opt_result.returncode == 0 else ""

            windows.append(
                {
                    "window_name": window_name,
                    "working_dir": working_dir,
                    "thread_id": tid,
                }
            )

        return windows

    # ── Claude execution API ────────────────────────────────────────

    def start_claude(
        self,
        thread_id: int,
        prompt: str,
        model: str = "sonnet",
        *,
        permission_mode: str = "acceptEdits",
        dangerously_skip_permissions: bool = False,
        try_continue: bool = False,
        effort: str | None = None,
    ) -> bool:
        """Start Claude Code inside the tmux window for *thread_id*.

        Sends ``claude --model {model} 'prompt'`` via ``send-keys``.
        The window must already exist (call ``create_session`` first).

        The prompt is passed as a CLI argument so Claude starts a new
        conversation immediately (without a prompt, ``claude`` tries to
        resume and exits with "No conversation found to continue").

        The command is prefixed with ``unalias claude`` and
        ``env -u CLAUDECODE`` to bypass any shell aliases (e.g.
        ``--continue``) and to prevent the nested-session guard from
        blocking startup.

        Returns True if the command was sent successfully.
        """
        if not self._check_available():
            return False

        window = self._find_window_for_thread(thread_id)
        if window is None:
            logger.warning("start_claude: no window for thread %d", thread_id)
            return False

        target = f"{self.session_name}:{window}"
        cmd_parts = ["env", "-u", "CLAUDECODE", "claude"]
        if try_continue:
            cmd_parts.append("--continue")
        cmd_parts.extend(["--model", model])
        if dangerously_skip_permissions:
            cmd_parts.append("--dangerously-skip-permissions")
        else:
            cmd_parts.extend(["--permission-mode", permission_mode])
        if effort is not None:
            if effort in _VALID_EFFORT_LEVELS:
                cmd_parts.extend(["--effort", effort])
            else:
                # The --effort flag hard-errors on unknown values, which would
                # abort claude startup and leave the thread with no reply.  Drop
                # the flag and fall back to the CLI default instead of crashing.
                # (ultracode/auto are valid effort levels but only via
                # CLAUDE_CODE_EFFORT_LEVEL / the /effort command, not this flag.)
                logger.warning(
                    "start_claude: ignoring unsupported effort %r (valid: %s)",
                    effort,
                    ", ".join(sorted(_VALID_EFFORT_LEVELS)),
                )

        # #530: mark the prompt as c-lord-originated, exactly as send_input
        # does. Without it the jsonl mirror reads the ``user`` event Claude
        # writes for this prompt as "a human typed into the pane" and posts the
        # whole thing back to the thread — one duplicated line for a short
        # message, a dozen messages burying the answer for a big one.
        from .transcript.formatter import ZWSP_MARKER
        from .transcript.mirror import bridge_mode_jsonl

        marked_prompt = f"{ZWSP_MARKER}{prompt}" if bridge_mode_jsonl() else prompt

        # #529: hand the prompt over in a file rather than typing it. Anything
        # typed at the pane's prompt goes through zsh's line editor, and
        # oh-my-zsh's url-quote-magic rewrites URLs on the way in.
        prelude = ""
        try:
            prompt_path = _write_prompt_file(marked_prompt)
        except OSError as exc:
            # A mangled URL is bad; losing the turn is worse — fall back to the
            # inline command line.
            logger.warning(
                "start_claude: could not stage the prompt in a file (%s); falling back "
                "to an inline command line — URLs may be mangled by the shell (#529)",
                exc,
            )
            safe_prompt = marked_prompt.replace("'", "'\\''")
            cmd_parts.append(f"'{safe_prompt}'")
        else:
            # Read it into a variable and delete the file *before* claude runs,
            # so no prompt text sits on disk for the life of the session.
            prelude = f'CLORD_PROMPT="$(cat {prompt_path})"; rm -f {prompt_path}; '
            cmd_parts.append('"$CLORD_PROMPT"')

        # Prefix with unalias to bypass any shell alias (e.g. --continue).
        cmd = f"unalias claude 2>/dev/null; {prelude}{' '.join(cmd_parts)}"

        # Typed literally and in pieces: the prompt rides on this command line,
        # so a long attachment/paste would otherwise blow past tmux's imsg cap
        # and the whole turn would be lost (#527).
        if not self._type_literal(target, cmd, what="start_claude"):
            return False
        result = _run(["tmux", "send-keys", "-t", target, "Enter"])
        if result.returncode != 0:
            logger.warning("start_claude: send-keys Enter failed: %s", result.stderr.strip())
            return False

        logger.info("start_claude: sent command to %s", target)
        return True

    def _type_literal(self, target: str, text: str, *, what: str) -> bool:
        """Type *text* into *target* with ``send-keys -l``, split for tmux's cap.

        One ``send-keys`` carrying more than ~16KB is refused outright by the
        tmux server (``command too long``) — see :func:`_chunk_for_send_keys`.
        Before #527 that meant a long paste or a large text attachment vanished
        on the way to Claude, with only a bare "Failed to send input" in
        Discord.  Splitting is transparent to the pane: the chunks arrive as one
        continuous stream of characters.

        Returns True only if **every** chunk was accepted; the caller must not
        press Enter on a partially typed payload.
        """
        chunks = _chunk_for_send_keys(text)
        for index, chunk in enumerate(chunks, start=1):
            result = _run(["tmux", "send-keys", "-l", "-t", target, chunk])
            if result.returncode != 0:
                logger.warning(
                    "%s: send-keys -l failed on chunk %d/%d (%d bytes): %s",
                    what,
                    index,
                    len(chunks),
                    len(chunk.encode("utf-8")),
                    result.stderr.strip(),
                )
                if index > 1:
                    # Best effort: wipe the half-typed payload. Left in the box
                    # it would prepend itself to whatever the user sends next —
                    # the same "input silently corrupted" class this fix exists
                    # to remove. C-u is the input box's kill-line.
                    _run(["tmux", "send-keys", "-t", target, "C-u"])
                return False
        return True

    def send_input(self, thread_id: int, text: str) -> bool:
        """Send text to the Claude process in the tmux window via ``send-keys -l``.

        Uses ``-l`` (literal) to prevent tmux from interpreting special
        characters in the text.  Sends Enter afterwards to submit.

        Returns True on success.
        """
        if not self._check_available():
            return False

        window = self._find_window_for_thread(thread_id)
        if window is None:
            logger.warning("send_input: no window for thread %d", thread_id)
            return False

        target = f"{self.session_name}:{window}"

        # Send the text literally (no tmux key interpretation).  Under
        # CLORD_BRIDGE_MODE=jsonl the text is prefixed with a zero-width-space
        # marker so the JSONL ``user`` event Claude Code subsequently writes
        # is recognised as c-lord-originated and skipped by the transcript
        # mirror (Issue #71) — prevents double-posting Discord input back to
        # the same thread.
        from .transcript.formatter import ZWSP_MARKER
        from .transcript.mirror import bridge_mode_jsonl

        # #485: if an interactive menu (AskUserQuestion / plan approval) is open
        # in the pane, a plain message's trailing Enter would SELECT the
        # highlighted option — fabricating an answer the user never made (a
        # normal reply "…提案して" was recorded as the choice "本文を削除"). This
        # happens when the bridge lost track of the menu (concurrent-session
        # churn left it open). Dismiss it with Esc first so the message reaches
        # Claude as text, never a selection. Safe in the normal case too:
        # replying with text instead of clicking cancels the menu and delivers
        # the message. Last line of defense — see tests/test_send_input_menu_guard.py.
        visible = _run(["tmux", "capture-pane", "-p", "-t", target])
        if visible.returncode == 0 and _pane_has_open_menu(visible.stdout):
            logger.warning(
                "send_input: interactive menu open in pane (thread=%d); dismissing "
                "with Esc so this reply can't select an option (#485)",
                thread_id,
            )
            _run(["tmux", "send-keys", "-t", target, "Escape"])
            time.sleep(_MENU_DISMISS_SETTLE)
            visible = _run(["tmux", "capture-pane", "-p", "-t", target])

        # #147: Claude's vim-mode input box drops to NORMAL after some
        # operations (e.g. an Escape sent by cancel_menu()).  Sending literal
        # text in NORMAL makes each character a vim command, corrupting the
        # message.  If not positively in INSERT, press ``i`` first.  We only
        # correct when the pane is positively NOT in INSERT (a recognised status
        # bar without ``-- INSERT``); an indeterminate frame is left untouched so
        # we never inject a stray ``i`` into an already-INSERT box (AC2).
        if visible.returncode == 0 and _pane_in_insert_mode(visible.stdout) is False:
            logger.debug("send_input: pane in NORMAL mode, entering INSERT (thread=%d)", thread_id)
            _run(["tmux", "send-keys", "-t", target, "i"])
            time.sleep(_INSERT_SETTLE)

        payload = f"{ZWSP_MARKER}{text}" if bridge_mode_jsonl() else text
        if not self._type_literal(target, payload, what="send_input"):
            return False

        # Press Enter to submit
        result = _run(["tmux", "send-keys", "-t", target, "Enter"])
        return result.returncode == 0

    def send_literal(self, thread_id: int, text: str) -> bool:
        """Send literal text to the pane WITHOUT submitting (no Enter) (#172).

        Unlike :meth:`send_input`, this does **not** append Enter, and does
        **not** prepend the jsonl bridge ZWSP marker.  It is used to type free
        text onto an open TUI menu's "Type something." row, where:

        - typing the literal text directly replaces the highlighted row's label
          with the text, and
        - the text must NOT be submitted as a separate message — the caller
          presses Enter afterwards to record it as the menu answer, and
        - the answer is not a c-lord-originated *user* turn, so the dedup ZWSP
          would only corrupt the recorded answer.

        Returns True on success.
        """
        if not self._check_available():
            return False

        window = self._find_window_for_thread(thread_id)
        if window is None:
            logger.warning("send_literal: no window for thread %d", thread_id)
            return False

        target = f"{self.session_name}:{window}"
        return self._type_literal(target, text, what="send_literal")

    def send_keys(self, thread_id: int, *keys: str) -> bool:
        """Send raw tmux key names to the window (e.g. ``"Down"``, ``"Enter"``).

        Unlike :meth:`send_input`, the arguments are passed to ``tmux send-keys``
        *without* ``-l``, so tmux interprets them as key names rather than
        literal text.  Used to drive TUI menus such as AskUserQuestion (#166):
        navigate with ``"Down"`` then confirm with ``"Enter"``.

        Returns True on success.
        """
        if not keys:
            return True
        if not self._check_available():
            return False

        window = self._find_window_for_thread(thread_id)
        if window is None:
            logger.warning("send_keys: no window for thread %d", thread_id)
            return False

        target = f"{self.session_name}:{window}"
        result = _run(["tmux", "send-keys", "-t", target, *keys])
        if result.returncode != 0:
            logger.warning("send_keys: send-keys failed: %s", result.stderr.strip())
            return False
        return True

    def capture_pane(self, thread_id: int, history_lines: int = 500) -> str:
        """Capture the current pane text from the tmux window.

        Uses ``tmux capture-pane -p -J`` to retrieve the visible and scrollback
        text with wrapped lines joined.

        Args:
            thread_id: The Discord thread ID.
            history_lines: Number of scrollback lines to capture (default 100).

        Returns:
            The pane text, or empty string on failure.
        """
        if not self._check_available():
            return ""

        window = self._find_window_for_thread(thread_id)
        if window is None:
            return ""

        target = f"{self.session_name}:{window}"
        # -e: preserve escape sequences (ANSI colors, OSC 8 hyperlinks).
        # tmux_runner._normalize_capture rewrites OSC 8 to bare URLs and
        # strips remaining color codes before line-based chrome filters run.
        # Without -e, terminal hyperlinks from Claude TUI lose the URL
        # portion entirely (issue #47).
        result = _run(
            [
                "tmux",
                "capture-pane",
                "-e",
                "-p",
                "-J",
                "-t",
                target,
                "-S",
                f"-{history_lines}",
            ]
        )
        if result.returncode != 0:
            return ""

        return result.stdout

    def _capture_after_grow(
        self,
        target: str,
        capture_args: list[str],
        tall_rows: int,
        fallback: Callable[[], str],
        settle_timeout: float = 3.0,
        poll_interval: float = 0.25,
    ) -> str:
        """Grow *target* to ``tall_rows``, let the TUI redraw, capture, restore.

        Claude Code runs as a full-screen TUI whose alternate screen keeps no
        scrollback, so a plain capture only ever returns the *visible* rows.
        Growing the window makes Claude redraw more of its conversation from
        memory (SIGWINCH). We grow, poll until the redraw settles (two
        consecutive identical captures — bounded by ``settle_timeout`` so a slow
        or never-settling redraw degrades to a best-effort frame, never a hang;
        ``settle_timeout=0`` does exactly one capture), run ``capture_args``,
        then restore the exact original size in a ``finally`` so an attached
        human's view is unchanged. ``fallback`` supplies the result when the
        size can't be read or the window is already ``>= tall_rows`` (a resize
        round-trip would gain nothing / risk a needless SIGWINCH). Shared by
        :meth:`capture_pane_tall` (#468) and :meth:`capture_screen` (#471).
        """
        size = _run(
            ["tmux", "display-message", "-p", "-t", target, "#{window_width} #{window_height}"]
        )
        if size.returncode != 0:
            # Can't read the current size → never risk leaving the window resized.
            return fallback()
        try:
            width_s, height_s = size.stdout.split()
            orig_width, orig_height = int(width_s), int(height_s)
        except ValueError:
            return fallback()

        if orig_height >= tall_rows:
            # Already tall enough — a resize round-trip would gain nothing.
            return fallback()

        def _resize(height: int) -> None:
            _run(
                [
                    "tmux",
                    "resize-window",
                    "-t",
                    target,
                    "-x",
                    str(orig_width),
                    "-y",
                    str(height),
                ]
            )

        text = ""
        try:
            _resize(tall_rows)
            attempts = max(1, int(settle_timeout / poll_interval))
            prev: str | None = None
            for i in range(attempts):
                result = _run(capture_args)
                cur = result.stdout if result.returncode == 0 else ""
                if cur:
                    text = cur
                if cur and cur == prev:
                    break
                prev = cur
                if i < attempts - 1:
                    time.sleep(poll_interval)
        finally:
            _resize(orig_height)

        return text

    def capture_pane_tall(
        self,
        thread_id: int,
        tall_rows: int = 240,
        history_lines: int = 500,
        settle_timeout: float = 3.0,
        poll_interval: float = 0.25,
    ) -> str:
        """Capture the pane after transiently enlarging the window height (#468).

        Claude Code runs as a full-screen TUI whose alternate screen keeps no
        scrollback, so :meth:`capture_pane` only ever returns the *visible*
        rows. When the prose above an AskUserQuestion menu is taller than the
        window, its head scrolls off and is unrecoverable — and
        ``_extract_pane_context`` then returns ``""`` (the pre-menu 経緯/推し is
        lost, so the question reaches Discord with no decision context).

        Claude redraws its whole conversation from memory on SIGWINCH, so
        briefly growing the window reveals the scrolled-off prose. Delegates the
        grow → settle → capture → restore round-trip to
        :meth:`_capture_after_grow`; the menu is idle-waiting for input (no
        spinner) so the capture settles quickly. Falls back to a scrollback
        :meth:`capture_pane` when the window is already tall enough. Returns raw
        text (``-e``) like :meth:`capture_pane`; callers normalize. Empty string
        on failure / no window.
        """
        if not self._check_available():
            return ""

        window = self._find_window_for_thread(thread_id)
        if window is None:
            return ""

        target = f"{self.session_name}:{window}"
        capture_args = [
            "tmux",
            "capture-pane",
            "-e",
            "-p",
            "-J",
            "-t",
            target,
            "-S",
            f"-{history_lines}",
        ]
        return self._capture_after_grow(
            target,
            capture_args,
            tall_rows,
            lambda: self.capture_pane(thread_id, history_lines),
            settle_timeout,
            poll_interval,
        )

    def capture_screen(
        self,
        thread_id: int,
        rows: int | None = None,
        settle_timeout: float = 3.0,
        poll_interval: float = 0.25,
    ) -> str:
        """Capture the pane as ANSI text for a screenshot (#285, #471).

        Unlike :meth:`capture_pane` (which pulls scrollback and joins wrapped
        lines for the event stream), this grabs the on-screen region with escape
        sequences preserved (``-e``) and no wrapped-line joining, so the PNG
        renderer reproduces the exact current screen.

        Because Claude's TUI keeps no scrollback, the only way to show *more*
        history than the ~40-row live window is to transiently grow the window
        so Claude redraws more of the conversation (#471); the taller visible
        screen is then captured and the original size restored exactly (via
        :meth:`_capture_after_grow`). ``rows`` defaults to
        :attr:`screenshot_rows` (env ``CLORD_TMUX_SCREENSHOT_ROWS`` /
        :data:`DEFAULT_SCREENSHOT_ROWS`); ``rows=0`` disables the growth and
        captures the current window as-is.

        Returns the raw ANSI text, or empty string on failure / no window.
        """
        if not self._check_available():
            return ""

        window = self._find_window_for_thread(thread_id)
        if window is None:
            return ""

        target = f"{self.session_name}:{window}"
        # -e: keep ANSI colors/hyperlinks. No -S (visible region only) and no
        # -J (preserve the exact on-screen layout) — this is a screenshot of
        # the *current* screen (after the optional growth), not a scrollback dump.
        capture_args = ["tmux", "capture-pane", "-e", "-p", "-t", target]

        def _capture_visible() -> str:
            result = _run(capture_args)
            return result.stdout if result.returncode == 0 else ""

        target_rows = self.screenshot_rows if rows is None else rows
        if target_rows and target_rows > 0:
            return self._capture_after_grow(
                target, capture_args, target_rows, _capture_visible, settle_timeout, poll_interval
            )
        return _capture_visible()

    def list_window_tabs(self) -> list[tuple[int, str, bool]]:
        """List this session's windows as ``(index, name, is_active)`` tuples.

        Feeds the synthesized tmux-style status bar in the screenshot (#285).
        Tab-delimited so window names containing spaces survive. Returns an
        empty list on failure / when tmux is unavailable.
        """
        if not self._check_available():
            return []

        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_index}\t#{window_name}\t#{window_active}",
            ]
        )
        if result.returncode != 0:
            return []

        tabs: list[tuple[int, str, bool]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                index = int(parts[0])
            except ValueError:
                continue
            tabs.append((index, parts[1], parts[2] == "1"))
        return tabs

    def send_interrupt(self, thread_id: int) -> bool:
        """Send C-c (SIGINT) to the tmux window.

        Returns True on success.
        """
        if not self._check_available():
            return False

        window = self._find_window_for_thread(thread_id)
        if window is None:
            logger.warning("send_interrupt: no window for thread %d", thread_id)
            return False

        target = f"{self.session_name}:{window}"
        result = _run(["tmux", "send-keys", "-t", target, "C-c"])
        return result.returncode == 0

    def is_claude_running(self, thread_id: int) -> bool:
        """Check if the pane's current process is ``claude``.

        Uses ``tmux list-panes -F '#{pane_current_command}'`` to inspect the
        foreground command.

        Returns True if claude is the active foreground process.
        """
        if not self._check_available():
            return False

        window = self._find_window_for_thread(thread_id)
        if window is None:
            return False

        return self._window_has_claude(window)

    def pane_foreground_command(self, thread_id: int) -> str | None:
        """Foreground command of *thread_id*'s pane, or None when unreadable (#510).

        Distinct from :meth:`is_claude_running`, which folds "no window" and
        "tmux unavailable" into False.  Callers that suppress behaviour on a
        dead pane need to tell *not claude* apart from *don't know*: pair this
        with :func:`pane_command_is_dead`.
        """
        if not self._check_available():
            return None

        window = self._find_window_for_thread(thread_id)
        if window is None:
            return None

        result = _run(
            [
                "tmux",
                "list-panes",
                "-t",
                f"{self.session_name}:{window}",
                "-F",
                "#{pane_current_command}",
            ]
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    # ── Cleanup ──────────────────────────────────────────────────────

    def cleanup_orphaned(self, active_thread_ids: set[int]) -> int:
        """Kill tmux windows whose threads are no longer active.

        Returns the number of windows killed.
        """
        if not self._check_available():
            return 0

        killed = 0
        for window in self.list_sessions():
            tid_str = window.get("thread_id", "")
            if not tid_str.isdigit():
                continue
            thread_id = int(tid_str)
            if thread_id not in active_thread_ids and self.kill_session(thread_id):
                killed += 1

        return killed
