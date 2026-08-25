"""Discord attachments land on disk as real files (#528).

Before this, a text attachment was pasted into the prompt and anything over
50KB was dropped without a word — the user attached a file and Claude answered
"そんなファイルは見つかりません", with nothing anywhere saying why.

Saving them instead means (a) Claude can ``Read`` the path like any other file,
(b) there is no reason for a size cap, and (c) the filename is attacker-shaped
input written to disk, so it has to be reduced to one harmless path component.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from c_lord.attachments import (
    ATTACHMENT_SUBDIR,
    attachment_dir,
    ensure_git_excluded,
    sanitize_filename,
    save_attachment,
)

# ── filename sanitising ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("notes.md", "notes.md"),
        ("仕様書.md", "仕様書.md"),  # unicode is fine, it is not a path separator
        ("my notes (1).txt", "my notes (1).txt"),
        ("../../etc/passwd", "passwd"),  # traversal reduced to its basename
        ("/etc/shadow", "shadow"),
        ("..", "attachment"),  # nothing left that is safe to use
        (".", "attachment"),
        ("", "attachment"),
        ("...", "attachment"),
        ("a/b/c.txt", "c.txt"),
        ("C:\\Windows\\evil.bat", "evil.bat"),  # windows separators too
        (".bashrc", "_.bashrc"),  # never write a dotfile
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_strips_control_characters_and_nul() -> None:
    assert "\x00" not in sanitize_filename("ev\x00il.txt")
    assert "\n" not in sanitize_filename("two\nlines.txt")


def test_sanitize_filename_caps_length_but_keeps_extension() -> None:
    out = sanitize_filename("あ" * 500 + ".md")
    assert len(out) <= 120
    assert out.endswith(".md")


# ── where files land ────────────────────────────────────────────────


def test_attachment_dir_is_scoped_by_message(tmp_path: Path) -> None:
    d = attachment_dir(str(tmp_path), 4242)
    assert d == tmp_path / ATTACHMENT_SUBDIR / "4242"


def test_save_attachment_writes_the_bytes(tmp_path: Path) -> None:
    path = save_attachment(str(tmp_path), 7, "spec.md", b"# hello\n")
    assert Path(path).read_bytes() == b"# hello\n"
    assert Path(path).name == "spec.md"


def test_save_attachment_can_never_escape_the_session_dir(tmp_path: Path) -> None:
    """A hostile filename must not write outside the attachment directory."""
    for hostile in ("../../escaped.txt", "/tmp/escaped.txt", "..\\..\\escaped.txt"):
        path = Path(save_attachment(str(tmp_path), 7, hostile, b"x"))
        assert tmp_path in path.parents, f"{hostile!r} escaped to {path}"
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_save_attachment_does_not_clobber_a_same_named_file(tmp_path: Path) -> None:
    first = save_attachment(str(tmp_path), 7, "a.txt", b"one")
    second = save_attachment(str(tmp_path), 7, "a.txt", b"two")
    assert first != second
    assert Path(first).read_bytes() == b"one"
    assert Path(second).read_bytes() == b"two"


# ── keeping the checkout clean ──────────────────────────────────────


def test_ensure_git_excluded_keeps_attachments_out_of_git(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    save_attachment(str(tmp_path), 7, "spec.md", b"x")
    ensure_git_excluded(str(tmp_path))

    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert ".clord" not in status.stdout, (
        "saved attachments must not show up as untracked — Claude runs `git add` in here"
    )


def test_ensure_git_excluded_is_idempotent(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    ensure_git_excluded(str(tmp_path))
    ensure_git_excluded(str(tmp_path))
    body = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert body.count(f"/{ATTACHMENT_SUBDIR.split('/')[0]}/") == 1


def test_ensure_git_excluded_is_a_noop_outside_a_repo(tmp_path: Path) -> None:
    ensure_git_excluded(str(tmp_path))  # must not raise
    assert not (tmp_path / ".git").exists()
