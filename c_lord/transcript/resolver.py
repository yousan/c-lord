"""Locate the active Claude Code transcript for a given tmux cwd.

Claude Code writes one JSONL per session under
``~/.claude/projects/<slug>/<session-id>.jsonl`` where ``<slug>`` is the cwd
with every ``/`` replaced by ``-`` (the leading slash too — so
``/home/yousan/c-lord`` → ``-home-yousan-c-lord``).
"""

from __future__ import annotations

from pathlib import Path


def derive_project_dir(cwd: str, *, projects_root: Path | None = None) -> Path:
    """Return the transcript directory for ``cwd``.

    The directory may not exist yet — Claude Code creates it lazily on first
    write — so callers should not assume ``.is_dir()`` is true here.
    """
    root = projects_root if projects_root is not None else Path.home() / ".claude" / "projects"
    return root / cwd.replace("/", "-")


def latest_session_jsonl(project_dir: Path) -> Path | None:
    """Return the most recently modified ``*.jsonl`` directly under ``project_dir``.

    Returns ``None`` if the directory is missing or contains no jsonl file.
    Subdirectories and other extensions are ignored.
    """
    if not project_dir.is_dir():
        return None
    candidates = [p for p in project_dir.glob("*.jsonl") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
