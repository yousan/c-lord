"""Put Discord attachments on disk so Claude can ``Read`` them (#528).

The old behaviour pasted text attachments straight into the prompt and dropped
anything past 50KB **silently** — from Discord it looked like "I attached a
file and Claude said it can't find it", with nothing anywhere explaining why.
Inlining also meant a single ``.md`` could blow past tmux's input cap (#527).

Writing the file next to the checkout fixes all three: the prompt only carries
a path, there is no reason left for a size cap, and "attach a file" finally
means what the user thinks it means — Claude opens a file.

The filename comes from whoever uploaded it, so it is reduced to exactly one
harmless path component before it is ever joined to a directory.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

logger = logging.getLogger(__name__)

# Kept under a dotted directory of our own rather than loose in the checkout,
# and git-excluded (see ``ensure_git_excluded``) so Claude's ``git add -A``
# cannot sweep a user's upload into a commit.
ATTACHMENT_SUBDIR = ".clord/attachments"

_MAX_FILENAME_CHARS = 120
# Anything outside this is replaced: path separators and NUL obviously, but
# also control characters, which make for unreadable, un-typeable paths.
_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")
_FALLBACK_NAME = "attachment"


def sanitize_filename(name: str) -> str:
    """Reduce *name* to a single filename that is safe to join to a directory.

    Traversal (``../``), absolute paths and Windows paths are cut down to their
    last component; control characters are replaced; a name that would produce
    a dotfile is prefixed. Unicode is preserved — it is not dangerous, just a
    filename.
    """
    # Take the basename under *both* conventions: a POSIX host still has to
    # cope with "C:\\Windows\\evil.bat" arriving from a Windows client.
    candidate = PurePosixPath(PureWindowsPath(name).name).name
    candidate = unicodedata.normalize("NFC", candidate)
    candidate = _UNSAFE_RE.sub("_", candidate).strip()

    # ".", "..", "..." and friends carry no usable name.
    if not candidate or set(candidate) <= {"."}:
        return _FALLBACK_NAME
    # Never create a dotfile out of an upload — it would hide in listings.
    if candidate.startswith("."):
        candidate = f"_{candidate}"

    if len(candidate) > _MAX_FILENAME_CHARS:
        stem, dot, suffix = candidate.rpartition(".")
        if dot and 0 < len(suffix) <= 16:
            keep = _MAX_FILENAME_CHARS - len(suffix) - 1
            candidate = f"{stem[:keep]}.{suffix}"
        else:
            candidate = candidate[:_MAX_FILENAME_CHARS]
    return candidate


def attachment_dir(session_dir: str, message_id: int) -> Path:
    """Directory holding the attachments of one Discord message."""
    return Path(session_dir) / ATTACHMENT_SUBDIR / str(message_id)


def save_attachment(session_dir: str, message_id: int, filename: str, data: bytes) -> str:
    """Write *data* as *filename* under the message's attachment directory.

    Returns the absolute path. Never overwrites: a second upload of the same
    name lands next to the first as ``name-2.ext`` — two files the user sent
    are two files, even when Discord let them share a name.
    """
    directory = attachment_dir(session_dir, message_id)
    directory.mkdir(parents=True, exist_ok=True)

    safe = sanitize_filename(filename)
    path = directory / safe
    if path.exists():
        stem, dot, suffix = safe.rpartition(".")
        base, ext = (stem, f".{suffix}") if dot else (safe, "")
        counter = 2
        while path.exists():
            path = directory / f"{base}-{counter}{ext}"
            counter += 1

    path.write_bytes(data)
    return str(path.resolve())


def ensure_git_excluded(session_dir: str) -> None:
    """Hide the attachment directory from git, locally.

    Claude works inside this checkout and runs ``git add``; an upload showing
    up as untracked is one ``git add -A`` away from being committed. Uses
    ``.git/info/exclude`` rather than ``.gitignore`` so the user's repository
    is not modified. Best effort — never raises.
    """
    top_level = ATTACHMENT_SUBDIR.split("/", 1)[0]
    entry = f"/{top_level}/"
    exclude = Path(session_dir) / ".git" / "info" / "exclude"
    try:
        if not (Path(session_dir) / ".git").exists():
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if entry in existing.splitlines():
            return
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        exclude.write_text(
            f"{existing}{prefix}# c-lord: Discord attachments (#528)\n{entry}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not git-exclude %s in %s: %s", entry, session_dir, exc)
