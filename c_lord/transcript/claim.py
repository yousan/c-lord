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
import logging
import os
import re
import uuid
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
