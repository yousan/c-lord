"""Tests for the Co-authored-by trailer hook (#518).

The hook is what actually guarantees the trailers land, so most tests here
drive a **real** git repository: install the hook, run ``git commit``, and
assert on the resulting commit message. Mocking git would test nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from c_lord.coauthor import (
    CLAUDE_COAUTHOR,
    DATA_FILE_NAME,
    HOOK_MARKER,
    HOOK_NAME,
    build_trailers,
    coauthor_enabled,
    discord_trailer,
    install_coauthor_hook,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _user(user_id: int = 4242, display_name: str = "yousan") -> MagicMock:
    """A stand-in for discord.Member / discord.User."""
    u = MagicMock()
    u.id = user_id
    u.display_name = display_name
    u.name = display_name
    return u


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An initialized git repo with a deterministic identity."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Host Bot")
    _git(path, "config", "user.email", "host@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(repo: Path, message: str, *, name: str = "f", extra: list[str] | None = None) -> str:
    """Create a commit and return its full message body."""
    (repo / name).write_text(message, encoding="utf-8")
    _git(repo, "add", name)
    result = _git(repo, "commit", "-m", message, *(extra or []))
    assert result.returncode == 0, result.stderr
    return _git(repo, "log", "-1", "--format=%B").stdout


# ---------------------------------------------------------------------------
# trailer rendering / sanitization (pure logic)
# ---------------------------------------------------------------------------


class TestDiscordTrailer:
    def test_format(self) -> None:
        assert (
            discord_trailer(_user(4242, "yousan"))
            == "Co-authored-by: yousan <4242@users.noreply.discord.com>"
        )

    def test_prefers_display_name_over_username(self) -> None:
        u = _user(1, "handle")
        u.display_name = "Nickname"
        assert "Nickname" in (discord_trailer(u) or "")

    def test_newline_cannot_forge_extra_trailers(self) -> None:
        u = _user(7, "evil\nCo-authored-by: Someone Else <a@b.c>")
        trailer = discord_trailer(u)
        assert trailer is not None
        assert "\n" not in trailer
        assert trailer.count("Co-authored-by:") == 1

    def test_angle_brackets_are_stripped(self) -> None:
        trailer = discord_trailer(_user(7, "ev<il>"))
        assert trailer == "Co-authored-by: evil <7@users.noreply.discord.com>"

    def test_control_characters_are_removed(self) -> None:
        trailer = discord_trailer(_user(7, "a\x00b\x1fc"))
        assert trailer is not None
        assert all(ch >= " " for ch in trailer)

    def test_blank_name_falls_back_to_discord_id(self) -> None:
        assert discord_trailer(_user(99, "   ")) == (
            "Co-authored-by: discord-99 <99@users.noreply.discord.com>"
        )

    def test_long_name_is_truncated(self) -> None:
        trailer = discord_trailer(_user(1, "x" * 300))
        assert trailer is not None
        assert len(trailer) < 200

    def test_none_user_returns_none(self) -> None:
        assert discord_trailer(None) is None

    def test_user_without_id_returns_none(self) -> None:
        u = MagicMock()
        u.id = "not-an-int"
        assert discord_trailer(u) is None


class TestBuildTrailers:
    def test_includes_both_when_user_known(self) -> None:
        trailers = build_trailers(_user())
        assert trailers[0].startswith("Co-authored-by: yousan <")
        assert CLAUDE_COAUTHOR in trailers

    def test_claude_only_when_user_unknown(self) -> None:
        assert build_trailers(None) == [CLAUDE_COAUTHOR]


class TestCoauthorEnabled:
    def test_default_is_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLORD_COAUTHOR", raising=False)
        assert coauthor_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
    def test_opt_out_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("CLORD_COAUTHOR", value)
        assert coauthor_enabled() is False


# ---------------------------------------------------------------------------
# real-git behaviour
# ---------------------------------------------------------------------------


class TestHookOnRealRepo:
    def test_commit_gets_both_trailers(self, repo: Path) -> None:
        install_coauthor_hook(repo, user=_user(4242, "yousan"))
        body = _commit(repo, "feat: something")
        assert "Co-authored-by: yousan <4242@users.noreply.discord.com>" in body
        assert CLAUDE_COAUTHOR in body

    def test_applies_even_with_no_verify(self, repo: Path) -> None:
        user = _user()
        install_coauthor_hook(repo, user=user)
        body = _commit(repo, "feat: bypass hooks", extra=["--no-verify"])
        assert discord_trailer(user) in body

    def test_claude_trailer_not_duplicated_when_claude_already_signed(self, repo: Path) -> None:
        install_coauthor_hook(repo, user=_user())
        body = _commit(
            repo,
            "feat: x\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
        )
        assert body.lower().count("noreply@anthropic.com") == 1
        assert "Claude Opus 5" in body

    def test_no_duplicate_on_amend(self, repo: Path) -> None:
        install_coauthor_hook(repo, user=_user())
        _commit(repo, "feat: x")
        _git(repo, "commit", "--amend", "--no-edit")
        body = _git(repo, "log", "-1", "--format=%B").stdout
        assert body.count("users.noreply.discord.com") == 1
        assert body.lower().count("noreply@anthropic.com") == 1

    def test_reinstall_with_a_different_user_switches_the_trailer(self, repo: Path) -> None:
        install_coauthor_hook(repo, user=_user(1, "alice"))
        _commit(repo, "feat: from alice", name="a")
        install_coauthor_hook(repo, user=_user(2, "bob"))
        body = _commit(repo, "feat: from bob", name="b")
        assert "bob <2@users.noreply.discord.com>" in body
        assert "alice" not in body

    def test_shell_metacharacters_in_name_are_not_executed(self, repo: Path) -> None:
        marker = repo / "pwned"
        user = _user(7, "a$(touch pwned)`touch pwned`b")
        install_coauthor_hook(repo, user=user)
        body = _commit(repo, "feat: injection attempt")
        assert not marker.exists(), "display name was executed by the hook"
        # The name survives verbatim as *text* — it is data, never shell.
        assert discord_trailer(user) in body

    def test_merge_commits_are_left_alone(self, repo: Path) -> None:
        install_coauthor_hook(repo, user=_user())
        _commit(repo, "base")
        _git(repo, "checkout", "-q", "-b", "side")
        _commit(repo, "side change", name="side.txt")
        _git(repo, "checkout", "-q", "main")
        _commit(repo, "main change", name="main.txt")
        result = _git(repo, "merge", "--no-ff", "-m", "merge side", "side")
        assert result.returncode == 0, result.stderr
        body = _git(repo, "log", "-1", "--format=%B").stdout
        assert "Co-authored-by:" not in body

    def test_commit_succeeds_when_data_file_is_deleted(self, repo: Path) -> None:
        install_coauthor_hook(repo, user=_user())
        (repo / ".git" / DATA_FILE_NAME).unlink()
        body = _commit(repo, "feat: no data file")
        assert "feat: no data file" in body

    def test_commit_succeeds_when_data_file_is_garbage(self, repo: Path) -> None:
        install_coauthor_hook(repo, user=_user())
        (repo / ".git" / DATA_FILE_NAME).write_text("not a trailer at all\n", encoding="utf-8")
        body = _commit(repo, "feat: garbage data")
        assert "feat: garbage data" in body


class TestHookInstallation:
    def test_hook_is_executable_and_marked(self, repo: Path) -> None:
        path = install_coauthor_hook(repo, user=_user())
        assert path is not None
        hook = Path(path)
        assert hook.name == HOOK_NAME
        assert HOOK_MARKER in hook.read_text(encoding="utf-8")
        assert hook.stat().st_mode & 0o111

    def test_hooks_path_inside_the_git_dir_is_used(self, repo: Path) -> None:
        custom = repo / ".git" / "alt-hooks"
        custom.mkdir()
        _git(repo, "config", "core.hooksPath", str(custom))
        user = _user()
        path = install_coauthor_hook(repo, user=user)
        assert path is not None
        assert Path(path).parent == custom
        body = _commit(repo, "feat: custom hooks dir")
        assert discord_trailer(user) in body

    def test_hooks_path_in_the_working_tree_is_refused(self, repo: Path) -> None:
        """husky-style `core.hooksPath=.husky`: writing there would leave an
        untracked file in the user's checkout, ready to be swept into a
        `git add -A`. Decline instead."""
        husky = repo / ".husky"
        husky.mkdir()
        _git(repo, "config", "core.hooksPath", ".husky")
        assert install_coauthor_hook(repo, user=_user()) is None
        assert not (husky / HOOK_NAME).exists()

    def test_hooks_path_outside_the_repo_is_refused(self, repo: Path, tmp_path: Path) -> None:
        """A *global* core.hooksPath would make a per-session hook leak into
        every repo on the host. Never write outside this session's git dir."""
        shared = tmp_path / "global-hooks"
        shared.mkdir()
        _git(repo, "config", "core.hooksPath", str(shared))
        assert install_coauthor_hook(repo, user=_user()) is None
        assert list(shared.iterdir()) == []

    def test_foreign_hook_is_not_clobbered(self, repo: Path) -> None:
        hook = repo / ".git" / "hooks" / HOOK_NAME
        hook.write_text("#!/bin/sh\n# husky\nexit 0\n", encoding="utf-8")
        assert install_coauthor_hook(repo, user=_user()) is None
        assert "husky" in hook.read_text(encoding="utf-8")

    def test_own_hook_is_overwritten(self, repo: Path) -> None:
        first = install_coauthor_hook(repo, user=_user(1, "alice"))
        second = install_coauthor_hook(repo, user=_user(2, "bob"))
        assert first == second

    def test_disabled_removes_existing_hook(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = install_coauthor_hook(repo, user=_user())
        assert path is not None
        monkeypatch.setenv("CLORD_COAUTHOR", "0")
        assert install_coauthor_hook(repo, user=_user()) is None
        assert not Path(path).exists()
        body = _commit(repo, "feat: disabled")
        assert "Co-authored-by:" not in body

    def test_non_git_directory_is_a_noop(self, tmp_path: Path) -> None:
        assert install_coauthor_hook(tmp_path, user=_user()) is None
