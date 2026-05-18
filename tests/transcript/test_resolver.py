"""Tests for c_lord.transcript.resolver — cwd → JSONL session file."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from c_lord.transcript.resolver import (
    derive_project_dir,
    latest_session_jsonl,
)


def test_derive_project_dir_replaces_slashes_with_dashes(tmp_path: Path) -> None:
    # Claude Code stores transcripts under ~/.claude/projects/<slug>/ where
    # <slug> is cwd with every '/' replaced by '-' (leading slash too).
    got = derive_project_dir("/home/yousan/c-lord", projects_root=tmp_path)
    assert got == tmp_path / "-home-yousan-c-lord"


def test_derive_project_dir_handles_trailing_slash(tmp_path: Path) -> None:
    got = derive_project_dir("/home/yousan/c-lord/", projects_root=tmp_path)
    # Trailing slash produces a trailing dash; Claude Code uses naive replace.
    assert got == tmp_path / "-home-yousan-c-lord-"


def test_latest_session_jsonl_returns_most_recently_modified(tmp_path: Path) -> None:
    project = tmp_path / "-home-yousan-c-lord"
    project.mkdir()
    older = project / "aaaa.jsonl"
    newer = project / "bbbb.jsonl"
    older.write_text("{}\n")
    time.sleep(0.01)
    newer.write_text("{}\n")
    # Force ordering via explicit mtime to defeat fs resolution flakiness.
    import os

    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    got = latest_session_jsonl(project)
    assert got == newer


def test_latest_session_jsonl_missing_dir_returns_none(tmp_path: Path) -> None:
    assert latest_session_jsonl(tmp_path / "nope") is None


def test_latest_session_jsonl_empty_dir_returns_none(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert latest_session_jsonl(tmp_path / "empty") is None


def test_latest_session_jsonl_ignores_non_jsonl(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "notes.txt").write_text("ignored")
    (project / "subdir").mkdir()
    assert latest_session_jsonl(project) is None


def test_derive_then_resolve_end_to_end(tmp_path: Path) -> None:
    project = tmp_path / "-tmp-foo"
    project.mkdir()
    jsonl = project / "session.jsonl"
    jsonl.write_text("{}\n")

    pd = derive_project_dir("/tmp/foo", projects_root=tmp_path)
    assert pd == project
    assert latest_session_jsonl(pd) == jsonl


def test_derive_project_dir_default_root_uses_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    got = derive_project_dir("/x/y")
    assert got == tmp_path / ".claude" / "projects" / "-x-y"
