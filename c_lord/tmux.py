"""Tmux session management for Claude Code sessions.

Uses a single tmux session ``clord`` with per-thread windows (``work1``,
``work2``, ...).  The mapping between thread IDs and window names is
stored in tmux window options (``@thread_id``) so it survives bot restarts.

All operations use ``asyncio.to_thread`` for non-blocking execution.
When tmux is not installed, operations degrade gracefully (log warning, skip).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time

# Discord snowflake IDs are 17–19 digits. Require ≥10 to avoid matching
# unrelated trailing-numeric path components (PIDs, ports, etc.).
_THREAD_ID_FROM_PATH_RE = re.compile(r"(\d{10,})/*$")

logger = logging.getLogger(__name__)

SESSION_NAME = "clord"
WINDOW_PREFIX = "work"

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


def _tmux_available() -> bool:
    """Return True if tmux is installed and accessible."""
    result = _run(["tmux", "-V"])
    return result.returncode == 0


class TmuxSessionManager:
    """Manages tmux windows for Claude Code Discord threads.

    One global tmux session ``clord`` holds all threads.  Each thread gets
    a window named ``work{N}`` with the thread ID stored in the ``@thread_id``
    window option.
    """

    def __init__(self, session_name: str | None = None, mapping_path: str | None = None) -> None:
        self.session_name: str = session_name or SESSION_NAME
        self._available: bool | None = None
        self._next_work_id: int = 1
        self._thread_to_window: dict[int, str] = {}
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

    def _ensure_session(self) -> bool:
        """Ensure the global ``clord`` tmux session exists.

        Returns True if the session is available (already existed or was
        created), False on failure.
        """
        result = _run(["tmux", "has-session", "-t", self.session_name])
        if result.returncode == 0:
            return True

        # Create with a temporary first window that will be replaced
        result = _run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.session_name,
            ]
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to create tmux session %s: %s",
                self.session_name,
                result.stderr.strip(),
            )
            return False

        logger.info("Created global tmux session: %s", self.session_name)
        return True

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
            # Stale cache entry
            del self._thread_to_window[thread_id]

        # Fallback: scan all windows
        self._rebuild_mapping()
        return self._thread_to_window.get(thread_id)

    def _next_window_name(self) -> str:
        """Generate the next ``work{N}`` name and increment the counter."""
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

        Also updates ``_next_work_id`` to be one past the highest existing
        work window number.
        """
        self._thread_to_window.clear()

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
            return

        # Pass 0: restore from persistent mapping file for windows whose
        # @thread_id was cleared and whose pane has cd'd away (issue #113 Fix-B).
        # Must run after we know the session exists but before the window scan,
        # so that subsequent @thread_id reads in the scan pick up the repaired values.
        self._load_from_mapping_file()

        max_id = 0
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t", 1)
            window_name = parts[0]
            pane_path = parts[1] if len(parts) > 1 else ""
            if not window_name:
                continue

            thread_id: int | None = None

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
            if opt_result.returncode == 0:
                tid_str = opt_result.stdout.strip()
                if tid_str.isdigit():
                    thread_id = int(tid_str)

            if thread_id is None and pane_path:
                m = _THREAD_ID_FROM_PATH_RE.search(pane_path)
                if m:
                    thread_id = int(m.group(1))
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
                    logger.info(
                        "Recovered thread_id %d for window %s from pane path %s",
                        thread_id,
                        window_name,
                        pane_path,
                    )

            if thread_id is not None:
                self._thread_to_window[thread_id] = window_name

            if window_name.startswith(WINDOW_PREFIX):
                suffix = window_name[len(WINDOW_PREFIX) :]
                if suffix.isdigit():
                    max_id = max(max_id, int(suffix))

        self._next_work_id = max_id + 1

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

    def _load_from_mapping_file(self) -> None:
        """Restore @thread_id from the persistent mapping file.

        Called at the start of _rebuild_mapping to recover mappings that were
        cleared by a tmux restart, even when the pane has cd'd away from the
        session directory (issue #113 Fix-B).
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
            if thread_id in self._thread_to_window:
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
            self._thread_to_window[thread_id] = window_name
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

        if not self._ensure_session():
            return f"{WINDOW_PREFIX}0"

        # Guard: if any window is already sitting in working_dir, adopt it
        # instead of creating a duplicate. This covers the post-tmux-restart
        # case where @thread_id options were cleared but the pane hasn't moved.
        # (Issue #111.)
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

        window_name = self._next_window_name()

        result = _run(
            [
                "tmux",
                "new-window",
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

        self._thread_to_window[thread_id] = window_name
        self._save_mapping()
        logger.info(
            "Created tmux window: %s (thread=%d, dir=%s)",
            window_name,
            thread_id,
            working_dir,
        )
        return window_name

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

        # Update cache: remove any old thread→window mapping for this window
        old_threads = [tid for tid, wname in self._thread_to_window.items() if wname == window_name]
        for tid in old_threads:
            del self._thread_to_window[tid]
        self._thread_to_window[thread_id] = window_name

        logger.info("Remapped window %s → thread %d", window_name, thread_id)
        return True

    def get_window_info(self, thread_id: int) -> tuple[str, int] | None:
        """Return ``(window_id, window_index)`` for the thread, or None.

        ``window_id`` is tmux's internal immutable id (e.g. ``@7``).
        ``window_index`` is the volatile per-session integer index
        shown in the tmux status line. Used by the thread-name builder
        as the trailing ``#N`` hint.

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
                "#{window_name}\t#{window_id}\t#{window_index}",
            ]
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] == window_name:
                idx_str = parts[2]
                if idx_str.isdigit():
                    return parts[1], int(idx_str)
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

        # Escape single quotes in the prompt for shell safety.
        safe_prompt = prompt.replace("'", "'\\''")
        cmd_parts.append(f"'{safe_prompt}'")
        # Prefix with unalias to bypass any shell alias (e.g. --continue).
        cmd = f"unalias claude 2>/dev/null; {' '.join(cmd_parts)}"

        result = _run(["tmux", "send-keys", "-t", target, cmd, "Enter"])
        if result.returncode != 0:
            logger.warning("start_claude: send-keys failed: %s", result.stderr.strip())
            return False

        logger.info("start_claude: sent command to %s", target)
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

        # #147: Claude's vim-mode input box drops to NORMAL after some
        # operations (e.g. an Escape sent by cancel_menu()).  Sending literal
        # text in NORMAL makes each character a vim command, corrupting the
        # message.  Capture the current frame and, if not positively in INSERT,
        # press ``i`` first.  We only correct when the pane is positively NOT in
        # INSERT (a recognised status bar without ``-- INSERT``); an
        # indeterminate frame is left untouched so we never inject a stray ``i``
        # into an already-INSERT box (AC2).
        visible = _run(["tmux", "capture-pane", "-p", "-t", target])
        if visible.returncode == 0 and _pane_in_insert_mode(visible.stdout) is False:
            logger.debug("send_input: pane in NORMAL mode, entering INSERT (thread=%d)", thread_id)
            _run(["tmux", "send-keys", "-t", target, "i"])
            time.sleep(_INSERT_SETTLE)

        payload = f"{ZWSP_MARKER}{text}" if bridge_mode_jsonl() else text
        result = _run(["tmux", "send-keys", "-l", "-t", target, payload])
        if result.returncode != 0:
            logger.warning("send_input: send-keys -l failed: %s", result.stderr.strip())
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
        result = _run(["tmux", "send-keys", "-l", "-t", target, text])
        if result.returncode != 0:
            logger.warning("send_literal: send-keys -l failed: %s", result.stderr.strip())
            return False
        return True

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

        target = f"{self.session_name}:{window}"
        result = _run(
            [
                "tmux",
                "list-panes",
                "-t",
                target,
                "-F",
                "#{pane_current_command}",
            ]
        )
        if result.returncode != 0:
            return False

        command = result.stdout.strip()
        return "claude" in command.lower()

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
