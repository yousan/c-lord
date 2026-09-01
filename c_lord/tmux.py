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

import getpass
import json
import logging
import os
import re
import socket
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

# tmux's immutable per-window handle, e.g. ``@218``. Unique for the life of the
# server, so it needs no session qualifier and — unlike a window *name* — can
# never resolve to a sibling that happens to share it (#649).
_WINDOW_ID_RE = re.compile(r"^@\d+$")

logger = logging.getLogger(__name__)

# One lock per tmux SESSION NAME, shared by every TmuxSessionManager pointing at
# that session (#649). ``resolve_tmux_manager()`` builds a separate manager per
# Discord channel and per thread, so an *instance* lock serialized nothing that
# mattered: two managers for one session could both mint ``w134`` and both run
# ``new-window -n w134``. tmux does not enforce unique window names, so two
# windows then answered to that name and every ``session:name`` target became
# ambiguous — thread A's ``@thread_id`` landed on thread B's window.
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _session_lock(session_name: str) -> threading.Lock:
    """The process-wide lock guarding window creation in *session_name*."""
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(session_name, threading.Lock())


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

# #147: when Claude Code runs with ``editorMode: vim`` its input box has a vim
# NORMAL mode in which literal characters (``send-keys -l``) are interpreted as
# editor commands, corrupting the message.  Pressing ``i`` first fixes that.
#
# #544: but vim mode is *not* the default, and c-lord must not assume it.  The
# status bar on Claude Code v2.1.246 looks like this:
#
#     vim on,  INSERT      ``-- INSERT -- ⏵⏵ bypass permissions on …``
#     vim on,  NORMAL      ``⏵⏵ bypass permissions on …``
#     vim off (default)    ``⏵⏵ bypass permissions on …``      <- identical
#
# ``⏵⏵``/``⏸⏸`` are the permission/plan indicators; they are present regardless
# of the editor mode, so they prove only that the pane is sitting at the input
# prompt.  c-lord used to treat that bare status bar as "vim NORMAL" and press
# ``i``, which for every consumer who does not use vim mode typed a literal
# ``i`` in front of each Discord message.  Only an explicit ``-- INSERT`` /
# ``-- NORMAL`` marker is evidence about vim; everything else is undecidable
# from one frame, and :meth:`TmuxSessionManager._ensure_insert_mode` resolves it
# by probing the pane instead of guessing.
_INSERT_MARKER = "-- INSERT"
# Not rendered by v2.1.246 (NORMAL shows no marker at all), but older/newer
# builds do show it and it is unambiguous when present.
_NORMAL_MARKER = "-- NORMAL"
# Status-bar anchors that prove the pane is sitting at the input prompt — i.e.
# that a keypress would land in the input box.  Deliberately NOT evidence of
# the editor mode (that was the #544 bug).
_STATUS_BAR_ANCHORS = ("⏵⏵", "⏸⏸")
# Pause after pressing ``i`` so the TUI commits the INSERT-mode switch (or
# renders the literal character) before we look at the pane / type the message.
# Verified on staging: the switch is near-instant.
_INSERT_SETTLE = 0.15
# #485: pause after Esc-dismissing a stuck menu so the TUI closes it before the
# message is typed (otherwise the text could still land on the closing menu).
_MENU_DISMISS_SETTLE = 0.3

# #560: ``send-keys -l`` delivers a long message as one fast burst, which the
# Claude Code TUI treats as a *paste* and folds into a ``[Pasted text #N +M
# lines]`` placeholder.  Folding is debounced, and an Enter that lands inside
# that window is absorbed as part of the paste instead of submitting — the
# message then sits in the input box until a human presses Enter (observed in
# production: 20+ minutes).  Measured on v2.1.246: ~335 characters still typed
# through as plain text, ~1029 folded.  The threshold is set below the observed
# fold point so anything that *might* fold gets the settle; shorter messages keep
# the old zero-latency path.
_PASTE_FOLD_MIN_CHARS = 300
# Pause between the last chunk of text and Enter, so the fold completes first.
_PASTE_SETTLE = 0.4
# #560: waits around reading the input box back after Enter. The first check is
# delayed so the TUI has redrawn; later ones pace the retries.
_SUBMIT_SETTLE = 0.35
_SUBMIT_RETRY_DELAY = 0.5
# How many times to look for the message leaving the box (each failed look after
# the first re-presses Enter). Three looks ≈ 1.35s worst case before giving up.
_SUBMIT_ATTEMPTS = 3
# A horizontal rule drawn by the TUI. The input box sits between the last two.
_BOX_RULE_RE = re.compile(r"^\s*[─━]{10,}\s*$")
# The placeholder a folded paste leaves in the input box. Compared against
# whitespace-stripped text because ``capture-pane`` hard-wraps the pane and can
# split the marker across two lines.
_PASTED_PLACEHOLDER = "[Pastedtext"
# How much of the payload to look for when deciding whether it is still in the
# box. Long enough to be distinctive, short enough to survive the TUI's wrapping.
_PAYLOAD_FINGERPRINT = 24

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


# OTEL_RESOURCE_ATTRIBUTES is comma/equals separated and gets typed into a
# shell inside single quotes, so any of these in a value would either split the
# attribute list or break the command line.
_OTEL_UNSAFE = re.compile(r"""[,='"$`\\\s]""")


def _repo_name(workdir: str) -> str | None:
    """Repository name for *workdir*, taken from its ``origin`` remote.

    c-lord runs each session in a directory named after the Discord thread, so
    the directory name is a snowflake ID and useless as a cost-attribution key.
    The remote URL is the only place the human-readable repository name lives.
    Returns None when *workdir* is not a git repo (or has no origin).
    """
    result = _run(["git", "-C", workdir, "remote", "get-url", "origin"])
    if result.returncode != 0:
        return None
    name = result.stdout.strip().rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name or None


def _otel_resource_attributes(workdir: str | None) -> str:
    """Build ``OTEL_RESOURCE_ATTRIBUTES`` for a Claude Code launch.

    Claude Code exports its own OpenTelemetry metrics (cost, tokens, session
    counts). Those carry only the attributes present in the environment, and
    c-lord never set any — so every session it started landed in a single
    unlabelled bucket and "which repository is the spend going to?" could not
    be answered at all. Only launches through the user's shell wrapper were
    ever labelled.

    Attributes whose value would break the encoding are dropped rather than
    escaped: a mangled command line would stop Claude from starting, which is
    far worse than one missing label.
    """
    attrs: dict[str, str] = {
        "host.user": getpass.getuser(),
        "host.name": socket.gethostname(),
    }
    if workdir:
        attrs["cwd"] = workdir
        repo = _repo_name(workdir)
        if repo:
            attrs["project"] = repo
    return ",".join(
        f"{key}={value}" for key, value in attrs.items() if value and not _OTEL_UNSAFE.search(value)
    )


def _status_zone(pane_text: str) -> str:
    """The bottom few non-blank lines — where Claude Code draws its status bar.

    Restricting every status check to this zone keeps a stale marker lingering
    in scrollback from outvoting the current frame.
    """
    lines = [ln for ln in pane_text.splitlines() if ln.strip()]
    return "\n".join(lines[-8:])


def _pane_in_insert_mode(pane_text: str) -> bool | None:
    """Return True if the input box is in vim INSERT mode, False for vim NORMAL.

    Returns ``None`` when the frame does not say — which covers both "no status
    bar at all" (mid-redraw, Claude generating) *and* the common case of a pane
    whose editor simply is not in vim mode (#544).  A bare ``⏵⏵`` status bar is
    emitted identically by vim-NORMAL and by a vim-less input box, so it is not
    evidence either way and must never be answered with ``False``.

    Callers must treat ``None`` as "the status bar cannot decide" — press no
    keys on this alone, or resolve it by probing.
    """
    if not pane_text.strip():
        return None
    zone = _status_zone(pane_text)
    if _INSERT_MARKER in zone:
        return True
    if _NORMAL_MARKER in zone:
        return False
    return None


def _pane_at_input_prompt(pane_text: str) -> bool:
    """True if the pane is showing Claude Code's input prompt status bar.

    Says nothing about the editor mode — only that a keypress would land in the
    input box, so it is safe to probe/correct there.
    """
    return any(anchor in _status_zone(pane_text) for anchor in _STATUS_BAR_ANCHORS)


def _squash(text: str) -> str:
    """Drop all whitespace (and the bridge ZWSP) so wrapped text can be matched.

    ``capture-pane`` hard-wraps the pane at its width, so a marker like
    ``[Pasted text #2 +5 lines]`` can arrive split across two lines. Comparing
    whitespace-free forms makes the match independent of where it wrapped.
    """
    from .transcript.formatter import ZWSP_MARKER

    return "".join(text.split()).replace(ZWSP_MARKER, "")


def _input_box_text(pane_text: str) -> str | None:
    """Whitespace-free contents of the TUI input box, or ``None`` if not found.

    Claude Code draws the input box between two horizontal rules just above the
    status bar::

        ────────────────────────────
        ❯ what the user is typing
        ────────────────────────────
           Model: …

    So the box is what lies between the last two rules. When the typed text is
    tall enough to push its own top rule off the top of the pane there is only
    one rule left, and everything above it *is* the box — handled by treating a
    missing top rule as "starts at the top of the capture".

    Returns ``""`` for an empty box.
    """
    lines = pane_text.splitlines()
    rules = [i for i, line in enumerate(lines) if _BOX_RULE_RE.match(line)]
    if not rules:
        return None
    bottom = rules[-1]
    above = [i for i in rules if i < bottom]
    top = above[-1] if above else -1
    content = _squash("\n".join(lines[top + 1 : bottom]))
    return content[1:] if content.startswith("❯") else content


def _input_box_retains(pane_text: str, payload: str) -> bool | None:
    """Is *payload* still sitting unsent in the input box? ``None`` if unknowable.

    Positive evidence only (#544's rule, applied again): a frame that cannot be
    parsed, or a box holding something we do not recognise, is **not** treated as
    a failed send.  A wrong "it failed" would tell the user their message was
    dropped when it actually went through, and the empty box also legitimately
    carries a greyed placeholder hint (``Try "refactor <filepath>"``).

    Two things count as evidence:

    * ``[Pasted text …]`` — the fold placeholder.  A submitted paste leaves the
      box empty, so a placeholder still there means our Enter never landed.
    * a fingerprint of the payload itself, for messages too short to be folded.
    """
    box = _input_box_text(pane_text)
    if box is None:
        return None
    if not box:
        return False
    if _PASTED_PLACEHOLDER in box:
        return True
    squashed = _squash(payload)
    if not squashed:
        return False
    head = squashed[:_PAYLOAD_FINGERPRINT]
    tail = squashed[-_PAYLOAD_FINGERPRINT:]
    return head in box or tail in box


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
        # Fallback only. The authority on the next free ``w{N}`` is live tmux
        # state (#649) — see :meth:`_next_window_name`; this is what that method
        # falls back to when ``list-windows`` itself fails.
        self._next_work_id: int = 1
        # thread_id -> tmux ``window_id`` (``@218``), never a window name (#649).
        self._thread_to_window: dict[int, str] = {}
        # #544: window id -> "is this pane's Claude running in vim editor
        # mode?".  Learned from the pane (an observed ``-- INSERT``/``-- NORMAL``
        # marker, or a probe), never assumed.  In-memory only: a restart just
        # means the next send re-learns it, which is cheap and self-correcting.
        self._vim_mode: dict[str, bool] = {}
        # Persistent thread→window mapping file. Survives tmux restarts; used
        # as fallback in _rebuild_mapping when pane has cd'd away (issue #113).
        if mapping_path is not None:
            self._mapping_path: str = mapping_path
        else:
            cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "c-lord")
            os.makedirs(cache_dir, exist_ok=True)
            self._mapping_path = os.path.join(cache_dir, f"{self.session_name}-window-map.json")

    @property
    def _lock(self) -> threading.Lock:
        """Serializes window creation + the post-create sort for this session.

        Keyed by ``session_name`` rather than held per instance (#649): the
        managers racing here are *different objects* pointing at one tmux
        session, so an instance lock serialized nothing. Resolved on each access
        so a manager whose ``session_name`` is reassigned (tests do this) still
        takes the lock that actually guards its session.
        """
        return _session_lock(self.session_name)

    def _target(self, window: str) -> str:
        """tmux target for *window*, given either a ``@id`` or a window name.

        Every internal lookup yields a ``window_id`` (#649), which is unique and
        therefore needs no session qualifier — and cannot silently resolve to a
        sibling sharing its name. Plain names still arrive from the operator-facing
        :meth:`remap_window`, and those stay session-qualified.
        """
        return window if _WINDOW_ID_RE.match(window) else f"{self.session_name}:{window}"

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

    def _fit_window_to_client(self, window: str) -> None:
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
                self._target(window),
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
        """Find the thread's tmux ``window_id`` via its ``@thread_id`` option.

        Returns a ``window_id`` (``@218``) — deliberately *not* the window name
        (#649). Names are not unique in tmux, so a name-based target resolves to
        whichever duplicate comes first, which is how one thread's keystrokes
        reached another thread's checkout. Returns None if not found.
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
                    self._target(cached),
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
        """Next free ``w{N}`` name, read from live tmux state (#649).

        The number used to come from ``self._next_work_id``, an instance
        counter. Two managers for one session each carried their own, so both
        handed out ``w134`` and tmux — which does not enforce unique window
        names — accepted both. Asking tmux for the current high-water mark
        instead means the answer is derived from the single shared source of
        truth, and callers hold the session lock while they use it.

        Falls back to the instance counter only when ``list-windows`` fails,
        where the alternative is not creating the window at all.
        """
        result = _run(["tmux", "list-windows", "-t", self.session_name, "-F", "#{window_name}"])
        if result.returncode != 0:
            name = f"{WINDOW_PREFIX}{self._next_work_id}"
            self._next_work_id += 1
            return name

        max_id = 0
        for line in result.stdout.splitlines():
            # Count both ``w{N}`` and legacy ``work{N}`` so numbering stays
            # monotonic across the prefix rename.
            n = parse_work_number(line.strip())
            if n is not None:
                max_id = max(max_id, n)
        self._next_work_id = max_id + 2  # fallback value should the next call fail
        return f"{WINDOW_PREFIX}{max_id + 1}"

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

        # One list-windows carries id, name, tag and path together (#649). Two
        # gains over the old per-window ``show-option`` follow-ups: the four
        # facts come from a single consistent snapshot, and every claim is keyed
        # by the unique ``window_id`` — so duplicate *names* can no longer make
        # a window collide with itself ("claimed by w134, w134").
        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_id}\t#{window_name}\t#{@thread_id}\t#{pane_current_path}",
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
        names: dict[str, str] = {}
        opt_claims: dict[int, list[str]] = {}
        path_claims: dict[int, list[tuple[str, str]]] = {}

        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            window_id, window_name = parts[0], parts[1]
            tag = parts[2] if len(parts) > 2 else ""
            # The path is the last field, so re-join anything past it: a tab in
            # a path would otherwise truncate the tail the thread-id regex reads.
            pane_path = "\t".join(parts[3:])
            if not window_id:
                continue
            names[window_id] = window_name

            if tag.isdigit():
                opt_claims.setdefault(int(tag), []).append(window_id)
            elif pane_path:
                m = _THREAD_ID_FROM_PATH_RE.search(pane_path)
                if m:
                    path_claims.setdefault(int(m.group(1)), []).append((window_id, pane_path))

            # Count both ``w{N}`` and legacy ``work{N}`` windows toward the high
            # watermark so numbering stays monotonic across the prefix rename.
            n = parse_work_number(window_name)
            if n is not None:
                max_id = max(max_id, n)

        # Windows that already carry @thread_id win over path-derived guesses.
        for thread_id, windows in opt_claims.items():
            new_map[thread_id] = self._resolve_claim(
                thread_id, windows, clear_losers=True, names=names
            )

        # Path fallback (#69) — only for threads no window has claimed outright,
        # so a stale pane sitting in the session dir can never steal a thread
        # from the window that owns the option (#501).
        for thread_id, matches in path_claims.items():
            if thread_id in opt_claims:
                logger.debug(
                    "Ignoring path-derived claim(s) %s for thread %d — already owned by %s",
                    ", ".join(self._describe(w, names) for w, _ in matches),
                    thread_id,
                    self._describe(new_map[thread_id], names),
                )
                continue
            winner = self._resolve_claim(
                thread_id, [w for w, _ in matches], clear_losers=False, names=names
            )
            pane_path = next(p for w, p in matches if w == winner)
            _run(
                [
                    "tmux",
                    "set-option",
                    "-w",
                    "-t",
                    self._target(winner),
                    "@thread_id",
                    str(thread_id),
                ]
            )
            new_map[thread_id] = winner
            logger.info(
                "Recovered thread_id %d for window %s from pane path %s",
                thread_id,
                self._describe(winner, names),
                pane_path,
            )

        # Atomic swap (#485): a single rebinding, so no reader ever sees a
        # partially-built map. dict assignment is atomic under CPython's GIL.
        self._thread_to_window = new_map
        self._next_work_id = max_id + 1

    def _window_has_claude(self, window: str) -> bool:
        """True when *window*'s pane runs ``claude`` in the foreground."""
        result = _run(
            [
                "tmux",
                "list-panes",
                "-t",
                self._target(window),
                "-F",
                "#{pane_current_command}",
            ]
        )
        if result.returncode != 0:
            return False
        return "claude" in result.stdout.strip().lower()

    @staticmethod
    def _describe(window: str, names: dict[str, str] | None = None) -> str:
        """``@218 (w134)`` — the id that identifies plus the name that reads.

        Logs used to name windows only, which made two genuinely different
        windows print identically ("claimed by w134, w134") and read as a bug in
        the logging (#649). Leading with the id keeps every line unambiguous.
        """
        name = (names or {}).get(window)
        return f"{window} ({name})" if name and name != window else window

    def _resolve_claim(
        self,
        thread_id: int,
        windows: list[str],
        *,
        clear_losers: bool,
        names: dict[str, str] | None = None,
    ) -> str:
        """Pick which of *windows* owns *thread_id* when several claim it.

        *windows* are ``window_id``s (#649), so two entries always mean two real
        windows; *names* is an optional id→name map used only to make the log
        legible.

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
            ", ".join(self._describe(w, names) for w in windows),
            self._describe(winner, names),
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
                        self._target(loser),
                        "@thread_id",
                    ]
                )
                logger.info(
                    "Cleared stale @thread_id %d from window %s",
                    thread_id,
                    self._describe(loser, names),
                )

        return winner

    def _window_name(self, window: str) -> str:
        """The display name (``w134``) of *window*, falling back to *window*.

        Internal state keys on ``window_id`` (#649), but humans attach with
        ``tmux attach -t <session>:<name>`` and threads are labelled ``W<N>``,
        so the name is what surfaces.
        """
        if not _WINDOW_ID_RE.match(window):
            return window
        result = _run(["tmux", "display-message", "-p", "-t", window, "#{window_name}"])
        if result.returncode != 0:
            return window
        return result.stdout.strip() or window

    def _window_names(self) -> dict[str, str]:
        """``window_id`` → window name for this session ({} when unreadable)."""
        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_id}\t#{window_name}",
            ]
        )
        if result.returncode != 0:
            return {}
        pairs: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[0]:
                pairs[parts[0]] = parts[1]
        return pairs

    def _save_mapping(self) -> None:
        """Persist the current thread→window mapping to disk (issue #113).

        Stores window **names**, not the ``window_id``s the in-memory map now
        holds (#649): this file exists to survive a tmux *server* restart, and a
        restart reassigns every id while tmux-resurrect restores the names.

        No-op when mapping_path is empty (disabled or test mode).
        """
        if not self._mapping_path:
            return
        names = self._window_names()
        try:
            data = {str(tid): names.get(win, win) for tid, win in self._thread_to_window.items()}
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

        # One snapshot for the whole file: the old loop ran a ``list-windows``
        # per entry, and the map holds one entry per live thread.
        live_names = self._window_names()

        for tid_str, window_name in data.items():
            if not tid_str.isdigit():
                continue
            thread_id = int(tid_str)
            if thread_id in target:
                continue  # already resolved by @thread_id option or path regex

            # Resolve the stored name to live window_id(s). The file holds names
            # (they are what survives a tmux restart), but everything downstream
            # must address the unique id (#649). A name shared by several windows
            # is not a mapping we can trust — skip it and let the @thread_id /
            # pane-path passes decide, rather than guessing at the first match.
            candidates = [wid for wid, name in live_names.items() if name == window_name]
            if len(candidates) != 1:
                if candidates:
                    logger.warning(
                        "Mapping file names window %s for thread %d, but %d windows "
                        "carry that name (%s) — ignoring the ambiguous entry",
                        window_name,
                        thread_id,
                        len(candidates),
                        ", ".join(candidates),
                    )
                continue
            window_id = candidates[0]

            # Check if @thread_id is already set (another thread adopted this window)
            opt_result = _run(
                [
                    "tmux",
                    "show-option",
                    "-w",
                    "-v",
                    "-t",
                    window_id,
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
                    window_id,
                    "@thread_id",
                    str(thread_id),
                ]
            )
            target[thread_id] = window_id
            logger.info(
                "Restored thread_id %d for window %s (%s) from mapping file",
                thread_id,
                window_id,
                window_name,
            )

    def _find_window_by_working_dir(self, working_dir: str) -> str | None:
        """Return the first ``window_id`` whose pane_current_path matches working_dir.

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
                "#{window_id}\t#{pane_current_path}",
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

        self._thread_to_window[thread_id] = window_id
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
            name = self._window_name(existing)
            logger.debug("tmux window already exists for thread %d: %s", thread_id, name)
            return name

        # Serialize the create-and-sort critical section (#374) so concurrent
        # create_session() calls don't interleave their move-window ops. The
        # lock is keyed by session name, not held per instance (#649) — the
        # racing callers are separate managers pointing at one tmux session.
        with self._lock:
            if not self._ensure_session():
                return f"{WINDOW_PREFIX}0"

            # Re-check under the lock: a concurrent create for this same thread
            # may have finished while we queued, and creating a second window
            # would mean two Claude processes on one checkout.
            existing = self._find_window_for_thread(thread_id)
            if existing is not None:
                return self._window_name(existing)

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
                        adopted,
                        "@thread_id",
                        str(thread_id),
                    ]
                )
                self._thread_to_window[thread_id] = adopted
                adopted_name = self._window_name(adopted)
                logger.info(
                    "Adopted window %s (%s) for thread %d by dir match: %s",
                    adopted,
                    adopted_name,
                    thread_id,
                    working_dir,
                )
                self._save_mapping()
                return adopted_name

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
                    # -P -F: print the new window's id. Everything after this
                    # addresses that id rather than the name (#649) — the name
                    # is only unique because we just checked, and tmux would
                    # happily resolve it to somebody else's window if it were not.
                    "-P",
                    "-F",
                    "#{window_id}",
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

            window_id = result.stdout.strip()
            if not _WINDOW_ID_RE.match(window_id):
                # An old tmux (or a stubbed _run) gave us no id. Fall back to the
                # name — correct as long as it is unique, which it is here
                # because we minted it under the session lock from live state.
                logger.warning(
                    "tmux new-window did not report a window_id for %s (got %r); "
                    "falling back to name-based targeting",
                    window_name,
                    result.stdout.strip(),
                )
                window_id = window_name

            # Store thread_id as a window option
            _run(
                [
                    "tmux",
                    "set-option",
                    "-w",
                    "-t",
                    self._target(window_id),
                    "@thread_id",
                    str(thread_id),
                ]
            )

            # Fit the new (manual-sized) window to the attached client while it
            # is still empty, so it looks right and then stays fixed (#403).
            self._fit_window_to_client(window_id)

            self._thread_to_window[thread_id] = window_id
            self._save_mapping()
            # Keep the session ordered by window number (#374). The new window
            # was inserted at the lowest free index (tmux default), so it may be
            # out of order; re-sort restores ascending w{N} order.
            self._sort_windows_unlocked()
            logger.info(
                "Created tmux window: %s (%s) (thread=%d, dir=%s)",
                window_name,
                window_id,
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
        # The lookup also resolves the name to its unique ``window_id``, which
        # is what the mapping stores (#649); an operator naming an ambiguous
        # window is told so rather than being silently given the first match.
        names = self._window_names()
        if not names:
            logger.debug("remap_window: session %s not found or empty", self.session_name)
            return False
        candidates = [wid for wid, name in names.items() if name == window_name]
        if not candidates:
            logger.debug("remap_window: window %s not found", window_name)
            return False
        if len(candidates) > 1:
            logger.warning(
                "remap_window: %d windows are named %s (%s) — refusing to guess "
                "which one thread %d means",
                len(candidates),
                window_name,
                ", ".join(candidates),
                thread_id,
            )
            return False
        window_id = candidates[0]

        # Update the @thread_id option
        _run(
            [
                "tmux",
                "set-option",
                "-w",
                "-t",
                window_id,
                "@thread_id",
                str(thread_id),
            ]
        )

        # Update cache: remove any old thread→window mapping for this window.
        # pop() (not del) for the same reason as #410 — a concurrent capture_pane
        # call may have evicted one of these keys between the comprehension and here.
        old_threads = [tid for tid, wid in self._thread_to_window.items() if wid == window_id]
        for tid in old_threads:
            self._thread_to_window.pop(tid, None)
        self._thread_to_window[thread_id] = window_id

        logger.info("Remapped window %s (%s) → thread %d", window_name, window_id, thread_id)
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
        window_id = self._find_window_for_thread(thread_id)
        if window_id is None:
            return None
        # #649: the lookup already yields the unique id, so the name is only
        # needed for the ``W<N>`` label. A stale id (window killed since the
        # last rebuild) resolves to no name, and reports no window.
        names = self._window_names()
        if window_id not in names:
            return None
        return window_id, parse_work_number(names[window_id])

    def window_name(self, thread_id: int) -> str | None:
        """The thread's window name (``w134``), or None when it has no window.

        The human-facing handle: what ``tmux attach -t <session>:<name>`` takes
        and what the synthesized status bar highlights. Internal targeting uses
        :meth:`_find_window_for_thread`'s ``window_id`` instead (#649).
        """
        if not self._check_available():
            return None
        window_id = self._find_window_for_thread(thread_id)
        if window_id is None:
            return None
        return self._window_name(window_id)

    def duplicate_window_names(self) -> list[str]:
        """Names carried by more than one live window in this session (#649).

        Nothing c-lord creates can land here any more — names are minted from
        live tmux state under the session lock. Leftovers from before that fix,
        and hand-made windows, still can, and one is enough to make every
        ``session:NAME`` target ambiguous. The failure that produces looks
        nothing like the dead pane the generic wording blames, so the runner
        asks this before telling the user what to do.
        """
        if not self._check_available():
            return []
        seen: dict[str, int] = {}
        for name in self._window_names().values():
            seen[name] = seen.get(name, 0) + 1
        return sorted(name for name, count in seen.items() if count > 1)

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

        window = self._find_window_for_thread(thread_id)
        if window is None:
            logger.debug("No tmux window found for thread %d", thread_id)
            return False

        result = _run(
            [
                "tmux",
                "kill-window",
                "-t",
                self._target(window),
            ]
        )
        if result.returncode == 0:
            self._thread_to_window.pop(thread_id, None)
            self._save_mapping()
            logger.info("Killed tmux window: %s (thread=%d)", window, thread_id)
            return True
        else:
            logger.debug("tmux window %s not found or already dead", window)
            return False

    def list_sessions(self) -> list[dict[str, str]]:
        """List all windows in the ``clord`` tmux session.

        Returns a list of dicts with ``window_name``, ``working_dir``, and
        ``thread_id`` keys.
        """
        if not self._check_available():
            return []

        # #649: ``@thread_id`` comes straight out of the format string, so each
        # row's tag is read from the window that row *is*. The old follow-up
        # ``show-option -t session:NAME`` per row re-resolved the name, and with
        # duplicate names every duplicate reported the first one's tag. Tab
        # separated (the old ``:`` split mangled any path containing a colon).
        result = _run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.session_name,
                "-F",
                "#{window_name}\t#{@thread_id}\t#{window_id}\t#{pane_current_path}",
            ]
        )
        if result.returncode != 0:
            return []

        windows: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t")
            windows.append(
                {
                    "window_name": parts[0],
                    "thread_id": parts[1] if len(parts) > 1 else "",
                    # #649: callers that go on to *act* on a row need the unique
                    # id — the name may be shared with another window.
                    "window_id": parts[2] if len(parts) > 2 else "",
                    # Last field, so it absorbs any tab a path might contain.
                    "working_dir": "\t".join(parts[3:]),
                }
            )

        return windows

    # ── Claude execution API ────────────────────────────────────────

    def start_claude(
        self,
        thread_id: int,
        prompt: str | None,
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

        ``prompt=None`` starts the TUI with **no turn to run** — used by
        :meth:`TmuxClaudeRunner.wake` to bring a stopped workspace back so it
        can be looked at (#642). Pair it with ``try_continue=True`` to reopen on
        the existing conversation; on its own it opens an empty session.

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

        # #544: the Claude we are about to start may not have the editor mode
        # the previous one in this window had (a recycled window, or changed
        # settings), so forget what was learned about it.  Observed on staging:
        # a window probed as vim-less was reused by a vim-mode Claude, and the
        # stale verdict meant no ``i`` was pressed — the message ran as vim
        # commands and left the pane in ``-- VISUAL LINE --``.
        self._vim_mode.pop(window, None)

        target = self._target(window)
        cmd_parts = ["env", "-u", "CLAUDECODE"]
        # Label the telemetry with the working directory and its repository so
        # cost/token metrics can be attributed per project instead of piling up
        # in one unlabelled bucket.
        otel_attributes = _otel_resource_attributes(self._pane_path(target))
        if otel_attributes:
            cmd_parts.append(f"OTEL_RESOURCE_ATTRIBUTES='{otel_attributes}'")
        cmd_parts.append("claude")
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

        prelude = ""
        # #642: a wake passes ``prompt=None`` — no positional argument at all,
        # so claude opens its TUI on the restored conversation and waits.
        # Staging an empty prompt file here would submit an empty turn instead.
        if prompt is not None:
            # #530: mark the prompt as c-lord-originated, exactly as send_input
            # does. Without it the jsonl mirror reads the ``user`` event Claude
            # writes for this prompt as "a human typed into the pane" and posts
            # the whole thing back to the thread — one duplicated line for a
            # short message, a dozen messages burying the answer for a big one.
            from .transcript.formatter import ZWSP_MARKER
            from .transcript.mirror import bridge_mode_jsonl

            marked_prompt = f"{ZWSP_MARKER}{prompt}" if bridge_mode_jsonl() else prompt

            # #529: hand the prompt over in a file rather than typing it. Anything
            # typed at the pane's prompt goes through zsh's line editor, and
            # oh-my-zsh's url-quote-magic rewrites URLs on the way in.
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

    def _pane_path(self, target: str) -> str | None:
        """Current working directory of *target*'s pane, or None if unknown."""
        result = _run(["tmux", "display-message", "-p", "-t", target, "#{pane_current_path}"])
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

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

    def _ensure_insert_mode(self, target: str, window: str, pane_text: str, thread_id: int) -> None:
        """Put a vim-mode input box into INSERT before literal text is typed.

        The hard part is that Claude Code v2.1.246 renders *nothing* in vim
        NORMAL, so its status bar is byte-identical to that of an input box with
        vim mode switched off entirely.  c-lord used to read that frame as
        NORMAL and press ``i`` unconditionally, which prefixed every message
        from a non-vim consumer with a literal ``i`` (#544).

        So the mode is established from evidence, never assumed:

        * ``-- INSERT`` — vim, already in INSERT.  Nothing to do.
        * ``-- NORMAL`` — vim, in NORMAL.  Press ``i``.
        * neither, and no input prompt — indeterminate frame (mid-redraw, or
          Claude is generating).  Touch nothing.
        * neither, but at the input prompt — undecidable from the frame, so
          *probe*: press ``i`` and look at what it did.  A pane that flips to
          ``-- INSERT`` was vim in NORMAL and the keypress did its job; a pane
          that does not has vim off, so the ``i`` was a literal character and is
          erased with a BSpace.  Either way the answer is remembered per window,
          so the probe costs one extra capture the first time only.

        The remembered answer is also refreshed from every marker we see, so a
        probe that raced a redraw self-corrects on the next send rather than
        sticking.
        """
        mode = _pane_in_insert_mode(pane_text)
        if mode is not None:
            # An explicit marker: this pane definitely runs vim mode.
            self._vim_mode[window] = True
            if mode is False:
                logger.debug(
                    "send_input: pane in vim NORMAL, entering INSERT (thread=%d)", thread_id
                )
                _run(["tmux", "send-keys", "-t", target, "i"])
                time.sleep(_INSERT_SETTLE)
            return

        if not _pane_at_input_prompt(pane_text):
            return  # no input prompt in this frame — never type blind

        known = self._vim_mode.get(window)
        if known is False:
            return  # vim is off on this pane: the box takes text as-is
        if known is True:
            logger.debug("send_input: pane in vim NORMAL, entering INSERT (thread=%d)", thread_id)
            _run(["tmux", "send-keys", "-t", target, "i"])
            time.sleep(_INSERT_SETTLE)
            return

        # Undecidable and unknown — probe (#544).
        _run(["tmux", "send-keys", "-t", target, "i"])
        time.sleep(_INSERT_SETTLE)
        after = _run(["tmux", "capture-pane", "-p", "-t", target])
        if after.returncode == 0 and _pane_in_insert_mode(after.stdout) is True:
            self._vim_mode[window] = True
            logger.info(
                "send_input: probe says vim editor mode is ON (window=%s thread=%d); "
                "the 'i' switched the box to INSERT",
                window,
                thread_id,
            )
            return
        # No INSERT marker appeared, so ``i`` was typed as a literal character.
        # Erase it — the message must reach Claude exactly as the user wrote it.
        # The BSpace is sent even when the verification capture itself failed:
        # the input box is empty at this point in a normal send, so deleting the
        # character we just typed is harmless, whereas leaving it behind would
        # reproduce the very bug this method exists to prevent.
        if after.returncode == 0:
            self._vim_mode[window] = False
            logger.info(
                "send_input: probe says vim editor mode is OFF (window=%s thread=%d); "
                "erasing the probe character so the message is sent unprefixed (#544)",
                window,
                thread_id,
            )
        else:
            # Nothing was actually observed, so don't remember a verdict —
            # re-probe next time instead of caching a guess.
            logger.warning(
                "send_input: could not read the pane back after the vim probe "
                "(window=%s thread=%d); erasing the probe character and retrying "
                "the detection on the next message",
                window,
                thread_id,
            )
        _run(["tmux", "send-keys", "-t", target, "BSpace"])
        time.sleep(_INSERT_SETTLE)

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

        target = self._target(window)

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

        # #147/#544: a vim-mode input box drops to NORMAL after some operations
        # (e.g. an Escape sent by cancel_menu() or the menu guard above), and
        # literal text typed in NORMAL becomes vim commands.  Pressing ``i``
        # fixes that — but only for panes that actually run vim mode.
        if visible.returncode == 0:
            self._ensure_insert_mode(target, window, visible.stdout, thread_id)

        payload = f"{ZWSP_MARKER}{text}" if bridge_mode_jsonl() else text
        if not self._type_literal(target, payload, what="send_input"):
            return False

        # #560: a payload big enough to be treated as a paste is folded into a
        # ``[Pasted text …]`` placeholder, and an Enter arriving inside that
        # debounce is swallowed by the fold instead of submitting. Give the TUI
        # time to finish folding first. Short messages never fold, so they keep
        # the old zero-latency path.
        if len(payload) >= _PASTE_FOLD_MIN_CHARS:
            time.sleep(_PASTE_SETTLE)

        # Press Enter to submit
        result = _run(["tmux", "send-keys", "-t", target, "Enter"])
        if result.returncode != 0:
            logger.warning(
                "send_input: Enter keypress failed (thread=%d): %s",
                thread_id,
                result.stderr.strip(),
            )
            return False
        return self._confirm_submitted(target, payload, thread_id)

    def input_box_holds(self, thread_id: int, text: str) -> bool | None:
        """Is *text* still sitting unsent in this thread's input box? (#560)

        Used on the delivery-failure path to tell the two failure modes apart:
        a pane that never took the input at all (#527 — the advice there is
        ``/restart-claude``) versus a message that is typed in and just will not
        submit, where restarting would **throw the user's message away**.

        ``None`` when it cannot be determined.
        """
        if not self._check_available():
            return None
        window = self._find_window_for_thread(thread_id)
        if window is None:
            return None
        capture = _run(["tmux", "capture-pane", "-p", "-J", "-t", self._target(window)])
        if capture.returncode != 0:
            return None
        from .transcript.formatter import ZWSP_MARKER
        from .transcript.mirror import bridge_mode_jsonl

        payload = f"{ZWSP_MARKER}{text}" if bridge_mode_jsonl() else text
        return _input_box_retains(capture.stdout, payload)

    def _confirm_submitted(self, target: str, payload: str, thread_id: int) -> bool:
        """Read the input box back and make sure the message actually left it (#560).

        The Enter ``send-keys`` exits 0 whether or not the TUI acted on it, so
        its return code proves nothing.  Before this check c-lord reported
        success for messages that were still sitting in the box — the turn then
        died on an idle timeout and neither the user nor the log said why.

        A message still in the box gets another Enter, up to
        :data:`_SUBMIT_ATTEMPTS` looks.  If it still will not go, return
        ``False`` so the caller surfaces a delivery failure rather than a silent
        drop.  Anything we cannot read is reported as success — see
        :func:`_input_box_retains` for why this only ever acts on positive
        evidence.
        """
        for attempt in range(1, _SUBMIT_ATTEMPTS + 1):
            time.sleep(_SUBMIT_SETTLE if attempt == 1 else _SUBMIT_RETRY_DELAY)
            capture = _run(["tmux", "capture-pane", "-p", "-J", "-t", target])
            if capture.returncode != 0:
                logger.warning(
                    "send_input: could not read the pane back to confirm delivery (thread=%d): %s",
                    thread_id,
                    capture.stderr.strip(),
                )
                return True
            retained = _input_box_retains(capture.stdout, payload)
            if retained is None:
                logger.warning(
                    "send_input: could not locate the input box to confirm delivery "
                    "(thread=%d); assuming the message was submitted",
                    thread_id,
                )
                return True
            if not retained:
                if attempt > 1:
                    logger.info(
                        "send_input: message submitted after %d Enter press(es) (thread=%d)",
                        attempt,
                        thread_id,
                    )
                return True
            if _pane_has_open_menu(capture.stdout):
                # Pressing Enter again would answer the menu rather than submit
                # (#485's failure mode). Whatever is in the box, it is not worth
                # fabricating a menu answer over.
                logger.warning(
                    "send_input: input box still holds the message but a menu is open "
                    "(thread=%d); not pressing Enter again",
                    thread_id,
                )
                return True
            if attempt < _SUBMIT_ATTEMPTS:
                logger.warning(
                    "send_input: message is still in the input box after Enter "
                    "(attempt %d/%d, thread=%d); pressing Enter again (#560)",
                    attempt,
                    _SUBMIT_ATTEMPTS,
                    thread_id,
                )
                _run(["tmux", "send-keys", "-t", target, "Enter"])
        logger.error(
            "send_input: message never left the input box after %d Enter presses "
            "(thread=%d, %d chars); reporting a delivery failure instead of a "
            "silent drop (#560)",
            _SUBMIT_ATTEMPTS,
            thread_id,
            len(payload),
        )
        return False

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

        target = self._target(window)
        return self._type_literal(target, text, what="send_literal")

    def pane_working_dir(self, thread_id: int) -> str | None:
        """The cwd of the thread's tmux pane, or None when it cannot be read (#651).

        Used to locate Claude Code's transcript for that pane
        (``transcript.resolver.derive_project_dir``), which is where the real
        outcome of an AskUserQuestion menu is recorded. Read from tmux rather
        than from c-lord's own session-dir bookkeeping so it reflects where the
        Claude process actually is.
        """
        if not self._check_available():
            return None
        window = self._find_window_for_thread(thread_id)
        if window is None:
            return None
        return self._pane_path(f"{self.session_name}:{window}")

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

        target = self._target(window)
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

        target = self._target(window)
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

        target = self._target(window)
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

        target = self._target(window)
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

        target = self._target(window)
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
                self._target(window),
                "-F",
                "#{pane_current_command}",
            ]
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    # ── Cleanup ──────────────────────────────────────────────────────

    def cleanup_orphaned(self, active_thread_ids: set[int]) -> int:
        """Kill leftover tmux windows in this session. Returns the count killed.

        A window is reaped only when **all three** hold:

        1. it carries an ``@thread_id`` — windows without one were created by
           hand (``factorio-server-1``, ``work3``, …) and are none of our
           business,
        2. its thread is not in *active_thread_ids*, and
        3. **its pane is not running Claude.**

        Condition 3 is the one that makes this callable at all (#570). Callers
        pass ``active_thread_ids=set()`` at startup — the bot has no in-flight
        threads yet — so membership alone would mark *every* window orphaned and
        kill live sessions. The pane's foreground command is the only signal
        that separates a leftover shell from a running Claude, and it is read
        fresh from tmux rather than inferred from our own bookkeeping.
        """
        if not self._check_available():
            return 0

        killed = 0
        for window in self.list_sessions():
            tid_str = window.get("thread_id", "")
            if not tid_str.isdigit():
                continue
            thread_id = int(tid_str)
            if thread_id in active_thread_ids:
                continue
            # #649: probe the window this row *is*, by id. Probing by name meant
            # a duplicate name answered for its twin — and the answer decides
            # whether a live Claude gets killed.
            target = window.get("window_id") or window.get("window_name", "")
            if target and self._window_has_claude(target):
                logger.debug(
                    "cleanup_orphaned: %s still runs Claude — keeping (thread=%d)",
                    self._describe(target),
                    thread_id,
                )
                continue
            if self.kill_session(thread_id):
                killed += 1

        return killed


def list_tmux_sessions() -> list[str]:
    """Every tmux session name on this host, or ``[]`` when tmux is unusable."""
    if not _tmux_available():
        return []
    result = _run(["tmux", "list-sessions", "-F", "#{session_name}"])
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def resident_thread_ids() -> set[int]:
    """Thread ids whose tmux pane is running Claude right now, host-wide (#576).

    **One** ``tmux list-panes -a`` call regardless of how many sessions exist —
    the resident-cap loop asks every 30 seconds, and the per-session walk
    :func:`cleanup_orphaned_all_sessions` does (one ``list-windows`` plus one
    ``list-panes`` each) costs ~40 subprocesses on a busy host.

    Counts only panes that are *positively* running claude. A pane whose
    foreground process is a shell holds a window, not the ~400 MB the cap is
    about, so counting it would evict a live workspace to make room for a
    corpse. Windows without an ``@thread_id`` were made by hand and are none of
    our business.
    """
    if not _tmux_available():
        return set()

    result = _run(["tmux", "list-panes", "-a", "-F", "#{@thread_id}\t#{pane_current_command}"])
    if result.returncode != 0:
        return set()

    resident: set[int] = set()
    for line in result.stdout.splitlines():
        tid, _, command = line.partition("\t")
        tid = tid.strip()
        if tid.isdigit() and "claude" in command.strip().lower():
            resident.add(int(tid))
    return resident


def cleanup_orphaned_all_sessions(active_thread_ids: set[int]) -> int:
    """Reap leftover c-lord windows across **every** tmux session.

    :meth:`TmuxSessionManager.cleanup_orphaned` only ever looks at its own
    ``session_name``. Since #427 the session name follows the *repo*, so real
    windows live in ``c-lord`` / ``project_30_ehon-ya`` / ``pt-jp`` / … while
    ``bot.tmux_manager`` points at the default ``clord`` — which on a busy host
    holds no windows at all. A reaper bound to that one session covers nothing.

    Scanning every session is safe because the per-window guards do the
    filtering: a window is only touched when it carries an ``@thread_id`` and
    is not running Claude. Sessions a human created by hand are full of windows
    with neither, so they are skipped without needing an allowlist of names —
    which matters because c-lord's repo-derived names (``games``, ``pt-jp``)
    collide with hand-made sessions of the same name.

    Returns the total number of windows killed.
    """
    total = 0
    for session_name in list_tmux_sessions():
        # mapping_path="" keeps this off the on-disk window map: the reaper is a
        # read-mostly sweep and must not rewrite another manager's cache file.
        mgr = TmuxSessionManager(session_name=session_name, mapping_path="")
        try:
            killed = mgr.cleanup_orphaned(active_thread_ids)
        except Exception:
            logger.warning(
                "cleanup_orphaned_all_sessions: session %s failed", session_name, exc_info=True
            )
            continue
        if killed:
            logger.info(
                "cleanup_orphaned_all_sessions: killed %d orphaned window(s) in session %s",
                killed,
                session_name,
            )
        total += killed
    return total
