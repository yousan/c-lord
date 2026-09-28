"""Issue #477 — a workspace that cannot be set up must say why, in the thread.

Before this, a failed ``git clone`` (or a tmux that would not start) raised out
of ``ClaudeChatCog._run_claude`` before the turn's ``try`` — so the thread kept
its 🟢 forever, the stall lamp came on, and the only record of the cause was a
log line on a host the user usually cannot read. #565 made that log line
appear; this makes the *thread* say it.

Two halves:

* :mod:`c_lord.workspace_failure` turns the exception into text a user can act
  on — the clone's own stderr, "authentication is needed" when that is what
  git said, what to check when tmux is the problem.
* ``_run_claude`` posts it (❌ embed + the requester's mention, #681), turns
  the lamp to ❌ and ends the turn instead of dying mid-flight.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.session_dir import GitCloneError, SessionDirManager
from c_lord.workspace_failure import describe_workspace_failure

_REPO = "https://github.com/yousan/private-thing"

# Real stderr, as git prints it (``git clone`` against GitHub, 2026-09).
_NOT_FOUND = (
    "Cloning into '/home/yousan/c-lord-sessions/1/2'...\n"
    "remote: Repository not found.\n"
    "fatal: repository 'https://github.com/yousan/private-thing/' not found"
)
_NO_USERNAME = (
    "Cloning into '/x'...\n"
    "fatal: could not read Username for 'https://github.com': No such device or address"
)
_SSH_DENIED = (
    "Cloning into '/x'...\n"
    "git@github.com: Permission denied (publickey).\n"
    "fatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights\n"
    "and the repository exists."
)
_BAD_BRANCH = (
    "Cloning into '/x'...\n"
    "warning: Could not find remote branch nope to clone.\n"
    "fatal: Remote branch nope not found in upstream origin"
)


def _clone_error(stderr: str, repo: str = _REPO) -> GitCloneError:
    return GitCloneError(repo=repo, stderr=stderr)


# ---------------------------------------------------------------------------
# GitCloneError — the clone failure carries what the message needs
# ---------------------------------------------------------------------------


class TestGitCloneError:
    def test_create_session_dir_raises_a_git_clone_error(self, tmp_path) -> None:
        result = MagicMock(returncode=128, stderr=_NOT_FOUND)
        with patch("c_lord.session_dir._run", return_value=result):
            mgr = SessionDirManager(base_dir=str(tmp_path), source_repo=_REPO)
            with pytest.raises(GitCloneError) as info:
                mgr.create_session_dir(12345)

        assert info.value.repo == _REPO
        assert "Repository not found" in info.value.stderr

    def test_it_is_still_a_runtime_error_with_the_old_text(self) -> None:
        """Callers that caught ``RuntimeError("git clone failed: …")`` keep working."""
        err = _clone_error("fatal: boom")
        assert isinstance(err, RuntimeError)
        assert str(err).startswith("git clone failed:")


# ---------------------------------------------------------------------------
# AC1 / AC2 — the clone failure, in words the user can act on
# ---------------------------------------------------------------------------


class TestCloneFailureText:
    def test_includes_the_stderr_summary(self) -> None:
        text = describe_workspace_failure(_clone_error(_BAD_BRANCH))
        assert "Remote branch nope not found in upstream origin" in text

    def test_names_the_repo(self) -> None:
        assert _REPO in describe_workspace_failure(_clone_error(_BAD_BRANCH))

    def test_generic_failure_points_at_url_and_branch(self) -> None:
        text = describe_workspace_failure(_clone_error(_BAD_BRANCH))
        assert "クローンに失敗" in text
        assert "ブランチ" in text
        assert "認証" not in text, "a missing branch is not an auth problem"

    @pytest.mark.parametrize(
        "stderr",
        [_NOT_FOUND, _NO_USERNAME, _SSH_DENIED],
        ids=["github-not-found", "no-username", "ssh-publickey"],
    )
    def test_auth_failure_says_authentication_is_needed(self, stderr: str) -> None:
        """AC2: GitHub answers a private repo you cannot read with "not found"."""
        text = describe_workspace_failure(_clone_error(stderr))
        assert "認証" in text
        assert "private" in text

    def test_credentials_in_the_url_are_not_echoed(self) -> None:
        secret_url = "https://yousan:ghp_SECRET123@github.com/yousan/x"
        stderr = f"fatal: unable to access '{secret_url}/': Could not resolve host: github.com"
        text = describe_workspace_failure(_clone_error(stderr, repo=secret_url))
        assert "ghp_SECRET123" not in text
        assert "github.com/yousan/x" in text

    def test_progress_line_and_host_path_are_left_out(self) -> None:
        text = describe_workspace_failure(_clone_error(_NOT_FOUND))
        assert "Cloning into" not in text
        assert "/home/yousan/c-lord-sessions" not in text

    def test_long_stderr_is_cut_down(self) -> None:
        stderr = "\n".join(f"remote: line {i} " + "x" * 200 for i in range(200))
        text = describe_workspace_failure(_clone_error(stderr))
        assert len(text) < 1500, "the embed must stay readable"
        assert "line 199" in text, "the last lines are the ones that say why"

    def test_stderr_is_fenced_so_markdown_cannot_break_out(self) -> None:
        text = describe_workspace_failure(_clone_error("fatal: ``` @everyone"))
        assert "```\nfatal:" in text
        assert "``` @everyone" not in text


# ---------------------------------------------------------------------------
# AC3 — tmux failures point at what to check
# ---------------------------------------------------------------------------


class TestTmuxFailureText:
    def test_missing_tmux_binary(self) -> None:
        exc = FileNotFoundError(2, "No such file or directory", "tmux")
        text = describe_workspace_failure(exc)
        assert "tmux" in text
        assert "インストール" in text

    def test_other_errors_still_name_what_failed(self) -> None:
        text = describe_workspace_failure(OSError("[Errno 24] Too many open files"))
        assert "Too many open files" in text


# ---------------------------------------------------------------------------
# Wiring — _run_claude posts it and ends the turn cleanly
# ---------------------------------------------------------------------------


def _message() -> MagicMock:
    author = MagicMock()
    author.id = 4242
    author.display_name = "yousan"
    author.name = "yousan"
    author.bot = False

    m = MagicMock(spec=discord.Message)
    m.id = 77
    m.author = author
    m.add_reaction = AsyncMock()
    m.remove_reaction = AsyncMock()
    m.clear_reaction = AsyncMock()
    return m


def _make_cog() -> ClaudeChatCog:
    bot = MagicMock()
    bot.channel_id = 999
    bot.owner_id = None
    bot.settings_repo = None
    bot.transcript_mirror_cog = None
    bot.user = MagicMock(id=1111)
    bot.get_cog = MagicMock(return_value=None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock()
    repo.touch = AsyncMock()
    repo.update_trigger_message = AsyncMock()
    runner = MagicMock()
    runner.working_dir = "/tmp/work"
    runner.model = None
    runner.timeout_seconds = 60
    runner.effort = None
    return ClaudeChatCog(bot=bot, repo=repo, runner=runner)


async def _run_turn(
    cog: ClaudeChatCog,
    *,
    clone_exc: BaseException | None = None,
    tmux_exc: BaseException | None = None,
    thread_send: AsyncMock | None = None,
) -> tuple[AsyncMock, MagicMock, MagicMock]:
    """Drive ``_run_claude`` with a failing clone or tmux. Returns (run, thread, message)."""
    sdm = MagicMock()
    sdm.create_session_dir = MagicMock(return_value="/tmp/work", side_effect=clone_exc)
    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w1", side_effect=tmux_exc)

    cog._resolve_session_dir_manager = AsyncMock(return_value=sdm)  # type: ignore[method-assign]
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)  # type: ignore[method-assign]
    cog._get_dashboard = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_coordination = MagicMock(return_value=None)  # type: ignore[method-assign]
    cog._get_current_model = AsyncMock(return_value=None)  # type: ignore[method-assign]
    cog._apply_thread_naming = AsyncMock()  # type: ignore[method-assign]

    thread = MagicMock(spec=discord.Thread)
    thread.id = 501
    thread.parent_id = 500
    thread.send = thread_send or AsyncMock(return_value=MagicMock())
    message = _message()

    run = AsyncMock(return_value=None)
    with patch("c_lord.cogs.claude_chat.run_claude_with_config", run):
        await cog._run_claude(message, thread, "hi", None)
    return run, thread, message


def _error_texts(thread: MagicMock) -> list[str]:
    out: list[str] = []
    for call in thread.send.await_args_list:
        embed = call.kwargs.get("embed")
        if embed is not None and embed.description:
            out.append(embed.description)
    return out


class TestRunClaudeReportsSetupFailure:
    async def test_clone_failure_is_posted_to_the_thread(self) -> None:
        """AC1 — the thread gets the cause, not a 30-second stall."""
        cog = _make_cog()
        run, thread, _msg = await _run_turn(cog, clone_exc=_clone_error(_BAD_BRANCH))

        assert not run.called, "there is no checkout to run Claude in"
        texts = _error_texts(thread)
        assert texts, "the thread must be told"
        assert "Remote branch nope not found" in texts[0]

    async def test_auth_failure_is_posted_as_such(self) -> None:
        """AC2 — end to end: the words reach the thread."""
        _run, thread, _msg = await _run_turn(_make_cog(), clone_exc=_clone_error(_NOT_FOUND))
        assert "認証" in _error_texts(thread)[0]

    async def test_tmux_failure_is_posted_to_the_thread(self) -> None:
        """AC3 — tmux itself refusing is a setup failure too."""
        exc = FileNotFoundError(2, "No such file or directory", "tmux")
        run, thread, _msg = await _run_turn(_make_cog(), tmux_exc=exc)

        assert not run.called
        texts = _error_texts(thread)
        assert texts and "tmux" in texts[0]

    async def test_lamp_turns_to_error(self) -> None:
        """🟢 must not be left burning on a turn that is over."""
        _run, _thread, msg = await _run_turn(_make_cog(), clone_exc=_clone_error(_BAD_BRANCH))
        emojis = [c.args[0] for c in msg.add_reaction.await_args_list]
        assert emojis[-1] == "❌"

    async def test_requester_is_mentioned(self) -> None:
        """#681: an embed never pushes — the mention in the content does."""
        _run, thread, _msg = await _run_turn(_make_cog(), clone_exc=_clone_error(_BAD_BRANCH))
        err_call = next(c for c in thread.send.await_args_list if c.kwargs.get("embed"))
        assert err_call.kwargs.get("content") == "<@4242>"

    async def test_failure_is_logged_as_error(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="c_lord.cogs.claude_chat"):
            await _run_turn(_make_cog(), clone_exc=_clone_error(_BAD_BRANCH))
        assert any("thread=501" in r.getMessage() for r in caplog.records)

    async def test_turn_does_not_stay_registered(self) -> None:
        """A dead turn left in ``_active_tasks`` reads as "still processing"."""
        cog = _make_cog()
        await _run_turn(cog, clone_exc=_clone_error(_BAD_BRANCH))
        assert 501 not in cog._active_tasks
        assert not cog.is_processing(501)

    async def test_a_failing_error_post_does_not_raise(self) -> None:
        """Reporting the failure must not become a second, silent failure."""
        send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=503), "down"))
        cog = _make_cog()
        await _run_turn(cog, clone_exc=_clone_error(_BAD_BRANCH), thread_send=send)
        assert 501 not in cog._active_tasks


# ---------------------------------------------------------------------------
# Comment AC (2026-09-08) — the /api/spawn turn is reported like any other
# ---------------------------------------------------------------------------


class TestSpawnedTurnIsReported:
    async def test_spawned_turn_that_dies_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """``spawn_session``'s task had no done-callback, so its death was invisible (#565)."""
        cog = _make_cog()

        async def _dies(_msg: object, thread: MagicMock, *_a: object, **_kw: object) -> None:
            # Like the real one: the turn parks itself in ``_active_tasks`` —
            # which is exactly what keeps it from ever being garbage-collected,
            # so asyncio's own "never retrieved" warning never fires either.
            task = asyncio.current_task()
            assert task is not None
            cog._active_tasks[thread.id] = task
            raise RuntimeError("died early")

        cog._run_claude = _dies  # type: ignore[method-assign]

        thread = MagicMock(spec=discord.Thread)
        thread.id = 601
        thread.send = AsyncMock(return_value=MagicMock())
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 600
        channel.create_thread = AsyncMock(return_value=thread)

        with caplog.at_level(logging.ERROR, logger="c_lord.cogs.claude_chat"):
            await cog.spawn_session(channel, "do the thing")
            for _ in range(5):
                await asyncio.sleep(0)

        ours = [r for r in caplog.records if r.name == "c_lord.cogs.claude_chat"]
        assert any("died early" in r.getMessage() for r in ours), (
            "a spawned turn that dies must leave an ERROR behind"
        )
