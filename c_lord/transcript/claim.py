"""c-lord names its own transcript, instead of looking for a mark inside it (#773).

One working copy holds many Claude Code sessions (#627), so a mirror has to
answer "which of these jsonl files is *this thread's*".  Until #773 the answer
was read out of the file: every prompt c-lord typed into the pane carried a
zero-width space, and the transcript that contained one was ours.

**Claude Code 2.1.278 removed that character from interactive input** before
writing the transcript — the TUI even says so ("Removed 1 invisible character
from the launch prompt before sending it").  Every thread's transcript then
looked like a stranger's, the mirror posted nothing, and the whole fleet went
silent without a single failed call (#773).

The lesson is not "find a better character".  It is that **ownership must not
depend on what the CLI does to our input**.  So c-lord now passes
``--session-id <uuid>`` when it starts Claude: the CLI writes that session to
``<uuid>.jsonl``, and the name is a thing c-lord chose rather than a thing it
hopes to find.  This module stores that choice next to the transcript it
describes, so the answer survives a bot restart and needs no database.

The claim file lives **inside the project dir** (``~/.claude/projects/<slug>/``)
because that is the one directory both writer and reader already hold: the
writer is ``TmuxSessionManager.start_claude`` (it knows the pane's cwd) and the
reader is :class:`~c_lord.transcript.resolver.ThreadSessionResolver` (it knows
nothing else).  It is a dot-file, so it is invisible to every ``*.jsonl`` glob —
c-lord's and Claude Code's alike — and it disappears with the transcripts it
names when the directory is removed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: Name of the claim file inside a project dir.
CLAIM_FILENAME = ".clord-session"

# The CLI refuses ``--session-id`` unless it is a valid UUID, and the transcript
# is named after it, so this doubles as the guard that keeps a corrupted claim
# from turning into a path (``../../etc/passwd``) we then go looking for.
_SESSION_ID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
# A claim holds one uuid and nothing else; anything larger is not one, and
# reading it in full is not worth the syscalls.
_MAX_CLAIM_BYTES = 256


def new_session_id() -> str:
    """A fresh session id to hand ``claude --session-id``."""
    return str(uuid.uuid4())


def is_session_id(value: str) -> bool:
    """Whether ``value`` is a session id the CLI would accept."""
    return bool(_SESSION_ID_RE.match(value))


def claim_path(project_dir: Path) -> Path:
    """Where the claim for ``project_dir`` lives."""
    return project_dir / CLAIM_FILENAME


def write_claim(project_dir: Path, session_id: str) -> bool:
    """Record that ``session_id`` is the session c-lord is about to start.

    Written before Claude runs — Claude Code creates the project dir lazily on
    its first write, so the directory usually does not exist yet and is created
    here.  Replaced atomically so a mirror polling twice a second never reads a
    half-written id.

    Returns False (and logs) rather than raising: a claim that could not be
    written costs the thread the new ownership rule, not the turn.  Blocking.
    """
    if not is_session_id(session_id):
        logger.warning("write_claim: refusing to record a malformed session id %r", session_id)
        return False
    target = claim_path(project_dir)
    tmp = target.with_name(f"{CLAIM_FILENAME}.{os.getpid()}.tmp")
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(f"{session_id}\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning("write_claim: could not record %s in %s (%s)", session_id, project_dir, exc)
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    return True


def read_claim(project_dir: Path) -> str | None:
    """The session id c-lord last started in ``project_dir``, or ``None``.

    A missing, unreadable or malformed claim is "no claim" — never a reason to
    go looking for a file whose name we did not choose.  Blocking.
    """
    try:
        with claim_path(project_dir).open("rb") as f:
            raw = f.read(_MAX_CLAIM_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_CLAIM_BYTES:
        return None
    value = raw.decode("utf-8", errors="replace").strip()
    return value if is_session_id(value) else None


def claimed_transcript(project_dir: Path) -> Path | None:
    """The claimed ``<session-id>.jsonl`` in ``project_dir``, if it exists yet.

    ``None`` covers both "no claim" and "Claude has not written its first line
    yet" — the caller falls back to the older marker rule in both cases, so a
    thread is never blind while Claude starts up.  Blocking.
    """
    session_id = read_claim(project_dir)
    if session_id is None:
        return None
    candidate = project_dir / f"{session_id}.jsonl"
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


# ── /clear moves the claim to its successor (#803) ─────────────────────────
#
# ``/clear`` is typed into the TUI (#803), and Claude Code answers it with a new
# session: a fresh ``<uuid>.jsonl`` whose first user event is the command
# itself (measured on CLI 2.1.282 — the file appears the moment ``/clear`` is
# submitted, not lazily on the next prompt).  The claim still names the old
# uuid, so without this the mirror would stay on a transcript nobody writes to
# any more — the post-clear answers would never reach Discord — and a later
# ``--resume <claim>`` would reopen the conversation the user just cleared.

#: How the successor announces itself.  A ``claude -p`` sub-invocation writing
#: into the same working copy meanwhile does not *start* with this, so it cannot
#: take the thread over (#627).
#: Anchored to the start of the ``content`` string (the same idea as the
#: resolver's marker context), so a message that merely *mentions* the command
#: does not qualify either.
_CLEAR_COMMAND_RE = re.compile(rb'"content"\s*:\s*"<command-name>/clear</command-name>')
# The command is the first user event; a few KB covers the mode / snapshot lines
# in front of it without reading a large file whole.
_CLEAR_PROBE_BYTES = 64 * 1024


def list_transcripts(project_dir: Path) -> set[str]:
    """The ``*.jsonl`` names in ``project_dir`` right now (empty if it is missing).

    Taken *before* ``/clear`` is sent, so :func:`adopt_cleared_session` can tell
    the file this clear produced from a clear transcript left by an earlier one.
    Blocking.
    """
    try:
        return {p.name for p in project_dir.glob("*.jsonl")}
    except OSError:
        return set()


def _starts_with_clear(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return _CLEAR_COMMAND_RE.search(f.read(_CLEAR_PROBE_BYTES)) is not None
    except OSError:
        return False


def adopt_cleared_session(project_dir: Path, before: set[str]) -> str | None:
    """Claim the transcript a ``/clear`` just produced; its session id, or ``None``.

    Only a file that was **not** in ``before`` and that opens with the
    ``/clear`` command qualifies.  ``None`` means "not there yet" as much as
    "never coming" — the caller polls.  Blocking.
    """
    try:
        fresh = [p for p in project_dir.glob("*.jsonl") if p.name not in before and p.is_file()]
    except OSError:
        return None
    for path in sorted(fresh, key=lambda p: p.stat().st_mtime, reverse=True):
        session_id = path.stem
        if not is_session_id(session_id) or not _starts_with_clear(path):
            continue
        if not write_claim(project_dir, session_id):
            return None
        logger.info(
            "adopt_cleared_session: /clear started session %s in %s — claim moved (#803)",
            session_id,
            project_dir,
        )
        return session_id
    return None


# ── ...and so does a /clear typed straight into the pane (#803) ─────────────
#
# 2026-09-28 10:09 JST, production: ``/clear`` typed by hand in a thread's pane.
# Claude Code started a new transcript, the claim still named the old one, and
# the mirror stayed on a file nobody wrote to again — the 10:31 answer never
# reached Discord.  Nothing c-lord typed was involved, so the resolver itself
# has to recognise the successor.  It is the clear transcript that
#
# * opens with the ``/clear`` command event,
# * was cleared **after the claimed session started** — an older clear
#   transcript lying in the directory is somebody's past, not this session's
#   future — and
# * was cleared **no earlier than the claimed transcript's last write**, give or
#   take :data:`_SUCCESSOR_SLACK` (Claude Code appends a few bookkeeping lines to
#   the old file as it switches).  A live session keeps writing, so a clear that
#   was not its own can never satisfy this.

_SUCCESSOR_SLACK = 5.0
_CLEAR_CONTENT_PREFIX = "<command-name>/clear</command-name>"


def _head_events(path: Path) -> list[dict]:
    """The complete JSON lines within the first :data:`_CLEAR_PROBE_BYTES` of ``path``."""
    try:
        with path.open("rb") as f:
            head = f.read(_CLEAR_PROBE_BYTES)
    except OSError:
        return []
    events: list[dict] = []
    for raw in head.split(b"\n")[:-1]:  # the last piece may be a cut-off line
        with contextlib.suppress(ValueError):
            event = json.loads(raw)
            if isinstance(event, dict):
                events.append(event)
    return events


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _started_at(events: list[dict]) -> float | None:
    for event in events:
        ts = _parse_ts(event.get("timestamp"))
        if ts is not None:
            return ts
    return None


def cleared_at(path: Path) -> float | None:
    """When the ``/clear`` that opens ``path`` ran, or ``None`` if it opens with none.

    Blocking.
    """
    for event in _head_events(path):
        if event.get("type") != "user":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.startswith(_CLEAR_CONTENT_PREFIX):
            return _parse_ts(event.get("timestamp"))
    return None


def clear_successor(
    project_dir: Path,
    claimed_id: str,
    *,
    probe: Callable[[Path], float | None] = cleared_at,
) -> str | None:
    """The session a ``/clear`` of ``claimed_id`` started, or ``None`` (#803).

    ``probe`` lets the resolver put a cache in front of :func:`cleared_at`; the
    rule is the same either way.  Blocking.
    """
    claimed = project_dir / f"{claimed_id}.jsonl"
    try:
        last_write = claimed.stat().st_mtime
        candidates = [p for p in project_dir.glob("*.jsonl") if p != claimed]
    except OSError:
        return None
    started = _started_at(_head_events(claimed))
    best: tuple[float, str] | None = None
    for path in candidates:
        if not is_session_id(path.stem):
            continue
        at = probe(path)
        if at is None:
            continue
        if started is not None and at <= started:
            continue
        if at < last_write - _SUCCESSOR_SLACK:
            continue
        if best is None or at > best[0]:
            best = (at, path.stem)
    return best[1] if best is not None else None
