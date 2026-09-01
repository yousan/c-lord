"""Locate the active Claude Code transcript for a given tmux cwd.

Claude Code writes one JSONL per session under
``~/.claude/projects/<slug>/<session-id>.jsonl`` where ``<slug>`` is the cwd
with every ``/`` replaced by ``-`` (the leading slash too — so
``/home/yousan/c-lord`` → ``-home-yousan-c-lord``).

**One cwd can hold many sessions.**  Every ``claude -p`` invocation and every
sub-agent started in that working copy writes its own ``<session-id>.jsonl``
into the same directory — the #627 example dir held **182** of them.  Picking
the mtime-latest one therefore does not answer "which transcript belongs to
this Discord thread"; it answers "who wrote last", and a sub-invocation writing
one line was enough to hijack the thread's mirror.  Its private conversation
was then posted into the user's thread, ``user``-role events included, i.e.
with a 👤 marker saying the user had said things they never said.

:class:`ThreadSessionResolver` answers the real question instead — see
:data:`CLORD_INPUT_MARKER`.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# How c-lord recognises *its own* session among the many in a project dir.
#
# Every prompt c-lord drives into the tmux pane is prefixed with U+200B
# (``tmux.send_input`` and, since #530, ``tmux.start_claude``).  Claude Code
# stores that as a ``user``-role **string** event, and its ``JSON.stringify``
# writes the character as raw UTF-8 rather than a ``​`` escape — so these
# bytes appear in the transcript of every session c-lord drives, and in no
# other.  A ``claude -p`` sub-invocation or a sub-agent in the same cwd never
# carries it.  (Verified against production transcripts 2026-08-31: 64 raw
# occurrences, 0 escaped.)
#
# Measured the same day: in the #627 example dir, 1 of 182 jsonl files carried
# the marker (the thread's own), and across all 313 live session dirs there was
# no case where "newest marked" differed from the file the old mtime rule picked
# — i.e. this narrows the choice without changing it for threads that were
# already behaving.
CLORD_INPUT_MARKER = b"\xe2\x80\x8b"

# ...and the context that makes a zero-width space *c-lord's marker* rather than
# one that merely occurs inside some tool output: it must open the JSON string
# value of ``content``, which is where the prompt itself is stored.  Matched
# with tolerance for separator whitespace so the probe does not hinge on a
# serialiser's formatting choice (Claude Code's ``JSON.stringify`` writes
# ``"content":"``; Python's ``json.dumps`` would write ``"content": "``).
_MARKER_CONTEXT_RE = re.compile(rb'"content"\s*:\s*"$')
# How far back the context is allowed to reach.  Generous enough for any amount
# of pretty-printing, small enough to stay a cheap check per candidate hit.
_MARKER_LOOKBACK = 64

# Ownership is decided by a byte search, not by parsing: the file is read in
# bounded slices so a 100 MB transcript neither blocks nor is slurped into
# memory (#537).
_PROBE_CHUNK_BYTES = 1 << 20
# Carry-over between slices so a marker (or its context) straddling a chunk
# boundary is still matched.
_PROBE_OVERLAP = _MARKER_LOOKBACK + len(CLORD_INPUT_MARKER)

# How long a directory must have been quiet before its cached listing is
# trusted.  Directory mtimes come from a coarse clock, so anything shorter
# risks caching a listing that already missed a same-tick creation.
_LISTING_SETTLE_SECONDS = 1.0


def derive_project_dir(cwd: str, *, projects_root: Path | None = None) -> Path:
    """Return the transcript directory for ``cwd``.

    The directory may not exist yet — Claude Code creates it lazily on first
    write — so callers should not assume ``.is_dir()`` is true here.

    The slug must match Claude Code's own algorithm, which (#320):

    * is computed from the **absolute** cwd — Claude Code always records its
      working directory as an absolute path. ``working_dir`` may be stored
      relative (the default session base is ``data/sessions``), so a relative
      input is resolved against the current process CWD first.
    * replaces **both** ``/`` and ``.`` with ``-`` — e.g. ``/home/u/.config``
      becomes ``-home-u--config`` (the leading ``/`` plus the converted dot
      yields a double dash).
    """
    if not os.path.isabs(cwd):
        cwd = os.path.realpath(cwd)
    root = projects_root if projects_root is not None else Path.home() / ".claude" / "projects"
    return root / cwd.replace("/", "-").replace(".", "-")


def latest_session_jsonl(project_dir: Path) -> Path | None:
    """Return the most recently modified ``*.jsonl`` directly under ``project_dir``.

    Returns ``None`` if the directory is missing or contains no jsonl file.
    Subdirectories and other extensions are ignored.

    This is "who wrote last", **not** "whose transcript is this" — see the module
    docstring and #627.  Callers that mirror a transcript into a Discord thread
    must use :class:`ThreadSessionResolver`; this remains for the places that
    only ask "did this workspace ever run Claude" (`session_cleanup`,
    `session_reattach`, `/workspace` status).
    """
    if not project_dir.is_dir():
        return None
    candidates = [p for p in project_dir.glob("*.jsonl") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _buffer_has_marker(buf: bytes) -> bool:
    """True when ``buf`` opens a ``content`` string with c-lord's marker.

    Scans with ``bytes.find`` (a zero-width space is rare, so this usually finds
    nothing at all) and only then pays for the context check.
    """
    start = 0
    while True:
        hit = buf.find(CLORD_INPUT_MARKER, start)
        if hit < 0:
            return False
        if _MARKER_CONTEXT_RE.search(buf, max(0, hit - _MARKER_LOOKBACK), hit):
            return True
        start = hit + 1


def is_clord_driven_jsonl(path: Path) -> bool:
    """True when ``path`` is a transcript of a session c-lord drives.

    Byte search for :data:`CLORD_INPUT_MARKER` in its ``"content":"`` context,
    read in bounded slices.  A missing or unreadable file is "not ours" — never
    a reason to fall back to somebody else's transcript.

    Blocking: reads the file.  Callers on the event loop must hand it to a
    worker thread (#537).
    """
    try:
        with path.open("rb") as f:
            carry = b""
            while True:
                chunk = f.read(_PROBE_CHUNK_BYTES)
                if not chunk:
                    return False
                if _buffer_has_marker(carry + chunk):
                    return True
                carry = chunk[-_PROBE_OVERLAP:]
    except OSError:
        return False


@dataclass
class ThreadSessionResolver:
    """Pick the jsonl a Discord thread's mirror may read, and stay on it.

    Answers "which transcript belongs to this thread", where
    :func:`latest_session_jsonl` only answered "who wrote last" (#627).

    Three rules, in order:

    1. **Only c-lord-driven transcripts are eligible** (:data:`CLORD_INPUT_MARKER`).
       A ``claude -p`` sub-invocation writing into the same working copy can no
       longer capture the mirror.
    2. **Only a *successor* may take over.**  Once a transcript is pinned, the
       pin moves only to a file that **appeared after** it was pinned — which is
       what a ``/clear`` produces.  A file that was already sitting in the
       directory can never take the pin, however new its mtime looks, so a
       ``touch`` (a resume, an editor, a backup tool) cannot send the mirror
       back over a transcript it has already read and re-post it (#627 AC3).
    3. **Nothing eligible → ``None``.**  The mirror posts nothing and says so
       once in the log.  Silence is the safe failure: reading somebody else's
       conversation is the bug (#627 AC4).  It heals by itself — the next turn
       c-lord drives writes the marker into that thread's transcript.

    Cheap enough to call twice a second per mirror (#537): the directory is
    re-listed only when it has actually changed (appending to a transcript does
    not touch its directory entry), and each eligibility verdict is cached until
    that file's size changes.
    """

    project_dir: Path
    _dir_mtime: float = -1.0
    _listed_at: float = 0.0
    _candidates: list[Path] = field(default_factory=list)
    # path -> (size when probed, verdict)
    _verdicts: dict[Path, tuple[int, bool]] = field(default_factory=dict)
    _pinned: Path | None = None
    # The directory contents as of the moment the current pin was chosen.  A
    # successor is a candidate that is *not* in here (rule 2).
    _known_at_pin: set[Path] = field(default_factory=set)
    _warned_none: bool = False

    def resolve(self) -> Path | None:
        """Return the jsonl to read, or ``None`` when none is eligible.

        Blocking (``stat`` / listing / bounded reads): callers on the event loop
        must run it in a worker thread (#537).
        """
        try:
            dir_mtime = self.project_dir.stat().st_mtime
        except OSError:
            return None

        if self._listing_is_stale(dir_mtime):
            try:
                self._candidates = [p for p in self.project_dir.glob("*.jsonl") if p.is_file()]
            except OSError:
                return self._pinned
            self._dir_mtime = dir_mtime
            self._listed_at = time.time()
            live = set(self._candidates)
            self._verdicts = {p: v for p, v in self._verdicts.items() if p in live}

        stats = self._stats()
        alive = {path for path, _, _ in stats}
        if self._pinned is not None and self._pinned not in alive:
            self._pinned = None  # the transcript was deleted — start over

        # Newest first: the first eligible successor wins, and the probe (which
        # may read a file) is paid for as few candidates as possible.
        for path, _mtime, size in reversed(stats):
            if path == self._pinned:
                continue
            if self._pinned is not None and path in self._known_at_pin:
                continue  # rule 2: not a successor, just an old file touched
            if not self._is_ours(path, size):
                continue
            if self._pinned is not None:
                logger.info(
                    "TranscriptMirror: following a newer c-lord session %s (was %s)",
                    path.name,
                    self._pinned.name,
                )
            self._pinned = path
            self._known_at_pin = set(self._candidates)
            self._warned_none = False
            return path

        if self._pinned is None:
            if not self._warned_none:
                self._warned_none = True
                logger.warning(
                    "TranscriptMirror: no c-lord-driven transcript in %s "
                    "(%d jsonl file(s) present) — posting nothing rather than "
                    "mirroring another session's conversation (#627)",
                    self.project_dir,
                    len(self._candidates),
                )
            return None
        return self._pinned

    def _listing_is_stale(self, dir_mtime: float) -> bool:
        """Whether the cached directory listing has to be rebuilt.

        Normally "the directory's mtime changed".  But a directory mtime is
        written from a coarse clock, so a file created in the same tick as our
        listing leaves the mtime looking untouched — and the listing would then
        stay wrong forever, which is how a ``/clear`` would be missed for the
        life of the mirror.  So a listing taken *close to* the directory's last
        change is also treated as unsettled and redone; once the directory has
        been quiet for a moment, the cache is trusted and the poll costs two
        stats instead of a listing of every transcript (28.9 ms for the
        2481-file dir measured in #537).
        """
        if dir_mtime != self._dir_mtime:
            return True
        return self._listed_at - dir_mtime < _LISTING_SETTLE_SECONDS

    def _stats(self) -> list[tuple[Path, float, int]]:
        """``(path, mtime, size)`` for each candidate that still exists, oldest first."""
        out: list[tuple[Path, float, int]] = []
        for path in self._candidates:
            try:
                st = path.stat()
            except OSError:
                continue
            out.append((path, st.st_mtime, st.st_size))
        out.sort(key=lambda item: item[1])
        return out

    def _is_ours(self, path: Path, size: int) -> bool:
        """Cached ownership verdict, re-probed when the file has grown.

        Re-probing on a size change is what catches a ``/clear``: Claude Code
        creates the successor jsonl *before* c-lord's marked prompt is written
        into it, so the first look at that file legitimately says "not ours".
        """
        cached = self._verdicts.get(path)
        if cached is not None and cached[0] == size:
            return cached[1]
        verdict = is_clord_driven_jsonl(path)
        self._verdicts[path] = (size, verdict)
        return verdict
