"""#518: the co-author hook is wired into the real session-dir lifecycle.

`test_coauthor.py` proves the hook works; these tests prove c-lord actually
installs it, for the user who triggered the turn.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from c_lord.coauthor import CLAUDE_COAUTHOR, DATA_FILE_NAME, HOOK_MARKER, HOOK_NAME
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.session_dir import SessionDirManager


def _fake_clone(args, cwd=None):  # noqa: ANN001, ANN202 — test helper
    """Stand in for `_run`: make `git clone` produce a real (empty) repo."""
    if "clone" in args:
        target = Path(args[-1])
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(target), check=False)
    return MagicMock(returncode=0, stderr="", stdout="")


def _user(user_id: int = 4242, name: str = "yousan") -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.display_name = name
    u.name = name
    # A real discord.User exposes a bool here; #520 keys off it to tell a
    # person from a bot-authored seed message, so the stand-in must too.
    u.bot = False
    return u


class TestSessionDirInstallsHook:
    def test_hook_and_trailers_written_for_the_requesting_user(self, tmp_path: Path) -> None:
        base = str(tmp_path / "sessions")
        with patch("c_lord.session_dir._run", side_effect=_fake_clone):
            mgr = SessionDirManager(base_dir=base, source_repo="/repo")
            target = Path(mgr.create_session_dir(987, coauthor=_user()))

        hook = target / ".git" / "hooks" / HOOK_NAME
        assert hook.exists(), "prepare-commit-msg hook should be installed"
        assert HOOK_MARKER in hook.read_text(encoding="utf-8")

        data = (target / ".git" / DATA_FILE_NAME).read_text(encoding="utf-8")
        assert "Co-authored-by: yousan <4242@users.noreply.discord.com>" in data
        assert CLAUDE_COAUTHOR in data

    def test_without_a_user_only_claude_is_recorded(self, tmp_path: Path) -> None:
        base = str(tmp_path / "sessions")
        with patch("c_lord.session_dir._run", side_effect=_fake_clone):
            mgr = SessionDirManager(base_dir=base, source_repo="/repo")
            target = Path(mgr.create_session_dir(988))

        data = (target / ".git" / DATA_FILE_NAME).read_text(encoding="utf-8")
        assert data.strip() == CLAUDE_COAUTHOR

    def test_refreshed_on_an_existing_session_dir(self, tmp_path: Path) -> None:
        """A later turn by a different user re-points the trailer at them."""
        base = str(tmp_path / "sessions")
        with patch("c_lord.session_dir._run", side_effect=_fake_clone):
            mgr = SessionDirManager(base_dir=base, source_repo="/repo")
            target = Path(mgr.create_session_dir(989, coauthor=_user(1, "alice")))
            mgr.create_session_dir(989, coauthor=_user(2, "bob"))

        data = (target / ".git" / DATA_FILE_NAME).read_text(encoding="utf-8")
        assert "bob <2@users.noreply.discord.com>" in data
        assert "alice" not in data


class _Stop(BaseException):
    """Sentinel raised to halt _run_claude right after the call under test."""


class TestRunClaudePassesTheAuthor:
    async def test_author_of_the_trigger_message_is_passed(self) -> None:
        bot = MagicMock()
        bot.channel_id = 999
        bot.settings_repo = None
        bot.get_cog = MagicMock(return_value=None)
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        repo.save = AsyncMock()
        cog = ClaudeChatCog(bot=bot, repo=repo, runner=MagicMock())

        sdm = MagicMock()
        sdm.create_session_dir = MagicMock(side_effect=_Stop)
        cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        cog._get_dashboard = MagicMock(return_value=None)  # type: ignore[method-assign]
        cog._get_coordination = MagicMock(return_value=None)  # type: ignore[method-assign]
        cog._get_current_model = AsyncMock(return_value=None)  # type: ignore[method-assign]

        thread = MagicMock(spec=discord.Thread)
        thread.id = 501
        thread.parent_id = 500
        thread.send = AsyncMock(return_value=MagicMock())

        message = MagicMock(spec=discord.Message)
        message.id = 77
        message.author = _user()
        message.add_reaction = AsyncMock()
        message.remove_reaction = AsyncMock()
        message.clear_reaction = AsyncMock()

        with contextlib.suppress(BaseException):
            await cog._run_claude(message, thread, "hi", None)

        sdm.create_session_dir.assert_called_once()
        args, kwargs = sdm.create_session_dir.call_args
        passed = kwargs.get("coauthor", args[1] if len(args) > 1 else None)
        assert passed is message.author, "the turn's Discord author must reach the hook"
