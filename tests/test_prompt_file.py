"""The cold-start prompt must not be typed into the user's shell (#529).

``start_claude`` builds ``claude … '<prompt>'`` and types it at the pane's zsh
prompt. oh-my-zsh binds ``url-quote-magic`` to ``self-insert``, so every ``?``,
``=`` and ``&`` inside a URL gets backslash-escaped **as it is typed**:

    1通目: …/image.png\\?ex\\=6a8548b6\\&is\\=…      ← what Claude received
    2通目: …/image.png?ex=6a854a08&is=…            ← send_input path, intact

Claude then fetched a URL that does not exist. The fix is to keep the prompt
out of the shell's line editor entirely: write it to a file and have the
command read it into a variable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from c_lord.tmux import TmuxSessionManager
from c_lord.transcript.formatter import ZWSP_MARKER

_URL = "https://cdn.discordapp.com/attachments/1/2/image.png?ex=6a8548b6&is=6a83f736&hm=deadbeef&"
_PROMPT = f"この画像を見て\n\n--- Attached file: image.png ---\nURL: {_URL}\n"


def _mgr() -> TmuxSessionManager:
    mgr = TmuxSessionManager(mapping_path="")
    mgr._available = True
    mgr.session_name = "t"
    mgr._find_window_for_thread = lambda _tid: "w1"  # type: ignore[method-assign]
    return mgr


def _typed(prompt: str, *, jsonl: bool = True) -> str:
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="")

    with (
        patch("c_lord.tmux._run", side_effect=fake_run),
        patch("c_lord.transcript.mirror.bridge_mode_jsonl", return_value=jsonl),
    ):
        assert _mgr().start_claude(12345, prompt, "sonnet") is True
    return "".join(c[-1] for c in calls if "send-keys" in c and "-l" in c)


def _prompt_path(cmd: str) -> Path:
    match = re.search(r'"\$\(cat (\S+)\)"', cmd)
    assert match, f"expected the command to read the prompt from a file: {cmd!r}"
    return Path(match.group(1))


# ── the prompt never reaches the line editor ────────────────────────


def test_the_prompt_is_not_typed_into_the_shell() -> None:
    cmd = _typed(_PROMPT)
    assert _URL not in cmd, "a URL on the command line is what zsh mangles (#529)"
    assert "この画像を見て" not in cmd


def test_the_prompt_file_holds_the_prompt_verbatim() -> None:
    cmd = _typed(_PROMPT)
    path = _prompt_path(cmd)
    try:
        body = path.read_text(encoding="utf-8")
        assert body == f"{ZWSP_MARKER}{_PROMPT}", "the prompt must survive byte-for-byte"
        assert _URL in body
    finally:
        path.unlink(missing_ok=True)


def test_the_prompt_file_is_readable_only_by_its_owner() -> None:
    """It holds whatever the user typed into Discord — not world-readable."""
    cmd = _typed(_PROMPT)
    path = _prompt_path(cmd)
    try:
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    finally:
        path.unlink(missing_ok=True)


def test_the_command_deletes_the_prompt_file_before_starting_claude() -> None:
    cmd = _typed(_PROMPT)
    path = _prompt_path(cmd)
    try:
        assert f"rm -f {path}" in cmd
        assert cmd.index(f"rm -f {path}") < cmd.index("claude --model"), (
            "delete it before claude runs, not after it exits"
        )
    finally:
        path.unlink(missing_ok=True)


def test_the_command_stays_small_however_long_the_prompt_is() -> None:
    """A 60KB prompt used to be a 60KB command line (#527's other half)."""
    big = "あ" * 20000
    cmd = _typed(big)
    path = _prompt_path(cmd)
    try:
        assert len(cmd.encode("utf-8")) < 1000
        assert path.read_text(encoding="utf-8").endswith(big)
    finally:
        path.unlink(missing_ok=True)


def test_the_marker_goes_into_the_file_not_the_command_line() -> None:
    cmd = _typed("hello")
    path = _prompt_path(cmd)
    try:
        assert ZWSP_MARKER not in cmd
        assert path.read_text(encoding="utf-8") == f"{ZWSP_MARKER}hello"
    finally:
        path.unlink(missing_ok=True)


# ── failure must not lose the turn ──────────────────────────────────


def test_falls_back_to_an_inline_prompt_when_the_file_cannot_be_written() -> None:
    """Losing the turn would be worse than a mangled URL."""
    with patch("c_lord.tmux._write_prompt_file", side_effect=OSError("no space")):
        cmd = _typed("hello world")
    assert "'​hello world'" in cmd
    assert "$(cat" not in cmd


# ── leftovers do not pile up ────────────────────────────────────────


def test_stale_prompt_files_are_swept(tmp_path: Path) -> None:
    from c_lord.tmux import _PROMPT_FILE_MAX_AGE, _sweep_stale_prompt_files

    old = tmp_path / "clord-prompt-old.txt"
    fresh = tmp_path / "clord-prompt-fresh.txt"
    old.write_text("x")
    fresh.write_text("y")
    stale = os.stat(old).st_mtime - _PROMPT_FILE_MAX_AGE - 60
    os.utime(old, (stale, stale))

    _sweep_stale_prompt_files(tmp_path)

    assert not old.exists(), "a prompt file left behind holds user text — clean it up"
    assert fresh.exists(), "a file for a turn still starting must survive"
