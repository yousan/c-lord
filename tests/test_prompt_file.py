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


def _typed(prompt: str) -> str:
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="")

    with patch("c_lord.tmux._run", side_effect=fake_run):
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


# ── the directory belongs to this user alone (#836) ─────────────────
#
# ``conftest._isolated_prompt_dir`` replaces ``_prompt_file_dir`` for every test;
# this module-level reference was taken at import time, before that patch, so
# the tests below exercise the real resolver.

import stat  # noqa: E402

import pytest  # noqa: E402

from c_lord.tmux import _prompt_file_dir as _real_prompt_file_dir  # noqa: E402


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private stand-in for ``/tmp`` with no XDG runtime dir."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(root))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    return root


def _assert_private(directory: Path) -> None:
    st = os.lstat(directory)
    assert stat.S_ISDIR(st.st_mode)
    assert st.st_uid == os.getuid()
    assert oct(st.st_mode & 0o777) == "0o700"


def test_prefers_the_xdg_runtime_dir(tmp_root: Path, tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "run-user"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    directory = _real_prompt_file_dir()

    assert directory == runtime / "clord-prompts"
    _assert_private(directory)


def test_without_a_runtime_dir_the_temp_dir_is_named_after_the_uid(tmp_root: Path) -> None:
    """A fixed name let the first user on the host own it for everyone (#836)."""
    directory = _real_prompt_file_dir()

    assert directory == tmp_root / f"clord-prompts-{os.getuid()}"
    _assert_private(directory)


def test_a_leftover_shared_dir_from_an_old_build_is_not_used(tmp_root: Path) -> None:
    legacy = tmp_root / "clord-prompts"
    legacy.mkdir(mode=0o700)

    directory = _real_prompt_file_dir()

    assert directory != legacy
    _assert_private(directory)


def test_another_users_dir_does_not_block_this_user(tmp_root: Path) -> None:
    """User B's turn stages its prompt even though user A got there first."""
    (tmp_root / "clord-prompts").mkdir(mode=0o700)  # the old shared name
    (tmp_root / f"clord-prompts-{os.getuid() + 1}").mkdir(mode=0o700)  # user A's

    directory = _real_prompt_file_dir()

    _assert_private(directory)
    fd, name = __import__("tempfile").mkstemp(dir=directory)
    os.close(fd)
    Path(name).unlink()


def test_a_dir_owned_by_someone_else_is_refused(tmp_root: Path, monkeypatch) -> None:
    """Someone pre-created *our* path — they could swap the prompt before ``cat``."""
    fake_uid = os.getuid() + 1
    squatted = tmp_root / f"clord-prompts-{fake_uid}"
    squatted.mkdir(mode=0o700)  # owned by the real uid, i.e. not by ``fake_uid``
    monkeypatch.setattr("c_lord.tmux.os.getuid", lambda: fake_uid)

    with pytest.raises(OSError):
        _real_prompt_file_dir()


def test_a_dir_others_can_write_to_is_refused(tmp_root: Path) -> None:
    loose = tmp_root / f"clord-prompts-{os.getuid()}"
    loose.mkdir()
    loose.chmod(0o777)

    with pytest.raises(OSError):
        _real_prompt_file_dir()
    assert oct(loose.stat().st_mode & 0o777) == "0o777", "refuse it, don't quietly reuse it"


def test_a_symlink_in_place_of_the_dir_is_refused(tmp_root: Path, tmp_path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    (tmp_root / f"clord-prompts-{os.getuid()}").symlink_to(target)

    with pytest.raises(OSError):
        _real_prompt_file_dir()


def test_an_unsafe_runtime_dir_falls_back_to_the_temp_dir(
    tmp_root: Path, tmp_path, monkeypatch
) -> None:
    runtime = tmp_path / "run-user"
    runtime.mkdir(mode=0o700)
    (runtime / "clord-prompts").mkdir()
    (runtime / "clord-prompts").chmod(0o777)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    directory = _real_prompt_file_dir()

    assert directory == tmp_root / f"clord-prompts-{os.getuid()}"
    _assert_private(directory)


def test_an_unusable_dir_still_falls_back_to_an_inline_prompt(tmp_root: Path, monkeypatch) -> None:
    """Refusing the directory must not cost the turn (#529's call)."""
    loose = tmp_root / f"clord-prompts-{os.getuid()}"
    loose.mkdir()
    loose.chmod(0o777)
    monkeypatch.setattr("c_lord.tmux._prompt_file_dir", _real_prompt_file_dir)

    cmd = _typed("hello world")

    assert "'​hello world'" in cmd
    assert list(loose.iterdir()) == [], "nothing may be written into a dir others control"
