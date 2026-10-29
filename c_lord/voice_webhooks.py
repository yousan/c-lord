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


def registry_path() -> Path:
    return clord_home() / "voice-webhooks.json"


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_file(path: Path) -> set[int] | None:
    """The ids in *path*; ``None`` when it could not be read (not the same as empty)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        logger.warning("voice webhooks: could not read %s", path, exc_info=True)
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
    global _ids, _seen_stamp
    if _ids is None:
        _ids = set()
        refresh = True
    if refresh:
        path = registry_path()
        stamp = _stamp(path)
        if stamp is not None and stamp != _seen_stamp:
            read = _read_file(path)
            if read is None:
                # Not read, so not seen (#1075): remembering the stamp would skip
                # the file until it changes, and another instance's voice would
                # pass as an order until then.
                return _ids
            _ids |= read
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


def register(webhook_id: int) -> None:
    """Remember *webhook_id* as one of ours, on disk, before anything is sent through it.

    Read-merge-write under a host-wide lock (#1024): writing back only this
    process's in-memory set erased the ids another instance had registered in
    the meantime, and their voices became orders again after a restart.
    """
    global _seen_stamp
    with _lock:
        ids = _load()
        ids.add(webhook_id)
        path = registry_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _file_lock(path):
                on_disk = _read_file(path) or set()
                ids |= on_disk
                if webhook_id in on_disk:
                    _seen_stamp = _stamp(path)
                    return
                tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(sorted(ids)), encoding="utf-8")
                tmp.replace(path)
                _seen_stamp = _stamp(path)
        except OSError:
            # Still known for this process; only a restart would forget it.
            logger.warning("voice webhooks: could not write %s", path, exc_info=True)


def is_voice_webhook(webhook_id: int | None) -> bool:
    if not webhook_id:
        return False
    with _lock:
        if webhook_id in _load():
            return True
        # Unknown here — another instance may have registered it since (#1024).
        # One ``stat`` unless the file changed; only webhook messages get here.
        return webhook_id in _load(refresh=True)


def is_voice_message(message: object) -> bool:
    """Was *message* posted by c-lord itself, speaking for a sub-agent?"""
    return is_voice_webhook(getattr(message, "webhook_id", None))


def _reset_for_tests() -> None:
    global _ids, _seen_stamp
    with _lock:
        _ids = None
        _seen_stamp = None
