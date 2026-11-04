"""The webhooks c-lord speaks through for sub-agents — and must never obey (#971).

With ``CLORD_SUBAGENT_VOICES=1`` a sub-agent's messages are posted through a
webhook whose username is the agent's name. c-lord *runs* webhook messages in
its threads: that is how one Claude dispatches work to another (2026-10-08), and
possession of a webhook is authorization (:func:`command_gate.is_message_authorized`).
Without this registry every message an agent said would come straight back as a
new instruction to the parent — a loop that talks to itself.

The ids are kept on disk, under ``~/.c-lord/``, because the danger outlives the
process: after a restart the pick-up of missed messages (#745/#776) reads the
thread back, and the voices posted before the restart are in it. They are
shared by every instance on the host on purpose — no instance should obey
another one's voices either. So the file is never overwritten with one
process's view: registering re-reads and merges under a lock, and an id this
process does not know sends it back to the file (#1024).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]

from .clord_home import clord_home

logger = logging.getLogger(__name__)

#: What c-lord names the webhook it creates (one per parent channel).
VOICE_WEBHOOK_NAME = "c-lord subagents"

_lock = threading.Lock()
_ids: set[int] | None = None
# What the file looked like when ``_ids`` was read from it: ``(mtime_ns, size)``.
_seen_stamp: tuple[int, int] | None = None
# The version of the file the last failed read was logged for (#1108): retried
# on every unknown webhook (#1075), but logged once per version, not per message.
_failed_stamp: tuple[int, int] | None = None
# The ids this process registered itself (#1176). Whenever the file is read and
# one of them is missing — it could not be written while the file was
# unreadable (#1109), or the file was replaced — it is written then, because
# there is usually no second ``register``: the voice webhook is cached per
# parent channel for the life of the process.
_ours: set[int] = set()
# Some of ``_ours`` is not known to be on disk yet (#1176). While so, any lookup
# — our own voices included — tries the file again, at most every
# ``_RETRY_SECONDS``: a quiet instance may see no unknown webhook for days.
_unwritten = False
_next_retry = 0.0
_RETRY_SECONDS = 60.0


def registry_path() -> Path:
    return clord_home() / "voice-webhooks.json"


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_file(path: Path, *, quiet: bool = False) -> set[int] | None:
    """The ids in *path*; ``None`` when it could not be read (not the same as empty).

    *quiet*: this version of the file has already been reported (#1108).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        if not quiet:
            logger.warning(
                "voice webhooks: could not read %s (retried on each unknown webhook; "
                "logged again only when the file changes — #1108)",
                path,
                exc_info=True,
            )
        return None
    if not isinstance(raw, list):
        return set()
    return {int(x) for x in raw if isinstance(x, int | str) and str(x).isdigit()}


def _load(*, refresh: bool = False) -> set[int]:
    """The ids this process knows, merged with the file when it has changed.

    The file is shared by every c-lord on the host (#1024), so another instance
    may have added to it since we last looked. Merged, never replaced: an id
    this process registered but could not write must not be forgotten.
    """
    global _ids, _seen_stamp, _failed_stamp
    if _ids is None:
        _ids = set()
        refresh = True
    if refresh:
        path = registry_path()
        stamp = _stamp(path)
        if stamp is None and _ours and time.monotonic() >= _next_retry:
            # #1204: the file is gone. Ours must be on it — recreate it (another
            # instance's ids are lost with the file either way; they re-add theirs).
            _flush_ours(path, quiet=_unwritten)
            return _ids
        if stamp is not None and stamp != _seen_stamp:
            read = _read_file(path, quiet=stamp == _failed_stamp)
            if read is None:
                # Not read, so not seen (#1075): remembering the stamp would skip
                # the file until it changes, and another instance's voice would
                # pass as an order until then. Only the *log* is once per
                # version (#1108) — a file that stays broken is otherwise one
                # WARNING with a traceback per webhook message, forever.
                _failed_stamp = stamp
                return _ids
            _failed_stamp = None
            _ids |= read
            if not _ours <= read:
                # Ours is not on disk: until it is, a restart or another
                # instance obeys our sub-agents' voices (#1176). Reported once:
                # a write that keeps failing is not one WARNING per message.
                _flush_ours(path, quiet=_unwritten)
                return _ids
        _seen_stamp = stamp
    return _ids


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock shared with every other c-lord on the host (#1024)."""
    if fcntl is None:  # pragma: no cover - not POSIX
        yield
        return
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _write_merged(path: Path, *, quiet: bool = False) -> bool:
    """Send what this process knows to *path*, merged with it, under the host lock.

    Call with ``_lock`` held and the file lock *not* held (``flock`` is per open
    file, so taking it twice in one process deadlocks). Unreadable is not empty
    (#1109): nothing is written then, and ours stays in memory for the next read.
    ``True`` when all of ours is on disk afterwards. Raises ``OSError`` when the
    write fails.
    """
    global _ids, _seen_stamp
    ids = _ids if _ids is not None else set()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        on_disk = _read_file(path, quiet=quiet)
        if on_disk is None:
            return False
        ids |= on_disk
        _ids = ids
        if _ours <= on_disk:
            _seen_stamp = _stamp(path)
            return True
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(sorted(ids)), encoding="utf-8")
        tmp.replace(path)
        _seen_stamp = _stamp(path)
        return True


def _flush_ours(path: Path, *, quiet: bool) -> None:
    """Try to get all of ``_ours`` onto disk; on failure, look at the file again later.

    Call with ``_lock`` held. A failed read or write forgets the file's stamp:
    a permission put back, or a directory made writable again, changes neither
    its mtime nor its size, and a remembered stamp would skip it for good (#1176).
    """
    global _unwritten, _next_retry, _seen_stamp
    _next_retry = time.monotonic() + _RETRY_SECONDS
    try:
        written = _write_merged(path, quiet=quiet)
    except OSError:
        _unwritten = True
        _seen_stamp = None
        if not quiet:
            # Still known for this process; only a restart would forget it.
            logger.warning(
                "voice webhooks: could not write %s (tried again at most every %.0fs)",
                path,
                _RETRY_SECONDS,
                exc_info=True,
            )
        return
    _unwritten = not written
    if not written:
        _seen_stamp = None


def register(webhook_id: int) -> None:
    """Remember *webhook_id* as one of ours, on disk, before anything is sent through it.

    Read-merge-write under a host-wide lock (#1024): writing back only this
    process's in-memory set erased the ids another instance had registered in
    the meantime, and their voices became orders again after a restart.
    """
    with _lock:
        _load().add(webhook_id)
        _ours.add(webhook_id)
        # Not written while the file is unreadable (#1109); then it goes to the
        # file the next time a read of it succeeds (#1176).
        _flush_ours(registry_path(), quiet=False)


def is_voice_webhook(webhook_id: int | None) -> bool:
    if not webhook_id:
        return False
    with _lock:
        if (
            _ours
            and time.monotonic() >= _next_retry
            and (_unwritten or _stamp(registry_path()) is None)
        ):
            # #1176: ours is not on disk yet — try again, quietly. #1204: nor
            # if the file was removed — our own voices may be all that posts.
            _flush_ours(registry_path(), quiet=True)
        if webhook_id in _load():
            return True
        # Unknown here — another instance may have registered it since (#1024).
        # One ``stat`` unless the file changed; only webhook messages get here.
        return webhook_id in _load(refresh=True)


def is_voice_message(message: object) -> bool:
    """Was *message* posted by c-lord itself, speaking for a sub-agent?"""
    return is_voice_webhook(getattr(message, "webhook_id", None))


def _reset_for_tests() -> None:
    global _ids, _seen_stamp, _failed_stamp, _unwritten, _next_retry
    with _lock:
        _ids = None
        _seen_stamp = None
        _failed_stamp = None
        _unwritten = False
        _next_retry = 0.0
        _ours.clear()
