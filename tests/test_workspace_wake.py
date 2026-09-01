"""Waking a stopped workspace so it can be looked at — #642.

``/tmux-screenshot`` used to dead-end on a stopped workspace with a sentence
telling the user to send a message. #572 made "stopped" the normal state of any
thread nobody touched for four hours, so that sentence became the usual answer.
These tests pin the wake path: bring the pane back with the conversation
restored, **without running a turn**, and only for workspaces a plain message
would have restored anyway.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.claude.tmux_runner import TmuxClaudeRunner

#: A pane parked at Claude's input box: idle, not generating.
IDLE_PANE = "\n".join(["  ⏺ done", "", "╭──────────╮", "❯", "╰──────────╯", "  -- INSERT --"])


@pytest.fixture
def tmux_manager():
    mgr = MagicMock()
    mgr.capture_pane.return_value = IDLE_PANE
    mgr.is_claude_running.return_value = False
    mgr.start_claude.return_value = True
    return mgr


@pytest.fixture
def runner(tmux_manager):
    return TmuxClaudeRunner(
        tmux_manager=tmux_manager,
        thread_id=123,
        model="sonnet",
        working_dir="/tmp/work",
        dangerously_skip_permissions=True,
        effort="high",
    )


def _fast(monkeypatch):
    """Collapse the runner's real-time waits so a test finishes in milliseconds."""
    monkeypatch.setattr("c_lord.claude.tmux_runner._POLL_INTERVAL", 0.001)
    monkeypatch.setattr("c_lord.claude.tmux_runner._CONTINUE_CHECK_DELAY", 0.001)


class TestRunnerWake:
    async def test_live_pane_is_left_alone(self, runner, tmux_manager, monkeypatch):
        """An awake workspace must not be restarted — that would kill the turn."""
        _fast(monkeypatch)
        tmux_manager.is_claude_running.return_value = True

        assert await runner.wake() is True
        tmux_manager.start_claude.assert_not_called()

    async def test_restores_the_conversation_without_running_a_turn(
        self, runner, tmux_manager, monkeypatch
    ):
        """``--continue`` with **no prompt**: the TUI comes back on the old
        conversation and sits idle. A prompt here would run a turn nobody asked
        for."""
        _fast(monkeypatch)
        tmux_manager.is_claude_running.side_effect = [False, True, True]

        assert await runner.wake() is True

        tmux_manager.start_claude.assert_called_once()
        args, kwargs = tmux_manager.start_claude.call_args
        assert args[0] == 123
        assert args[1] is None, "no prompt — waking must not start a turn"
        assert kwargs["try_continue"] is True
        # Same startup conditions as the message path, so the process this
        # leaves behind is the one the next message can talk to (#642 AC3).
        assert kwargs["dangerously_skip_permissions"] is True
        assert kwargs["effort"] == "high"

    async def test_falls_back_to_a_fresh_start_when_there_is_nothing_to_continue(
        self, runner, tmux_manager, monkeypatch
    ):
        """``claude --continue`` exits immediately when the transcript is gone.
        Mirrors the #123 Part 2 fallback on the turn path."""
        _fast(monkeypatch)
        # dead → still dead after --continue → alive after the fresh start
        tmux_manager.is_claude_running.side_effect = [False, False, True, True]

        assert await runner.wake() is True

        assert tmux_manager.start_claude.call_count == 2
        assert tmux_manager.start_claude.call_args_list[0].kwargs["try_continue"] is True
        assert tmux_manager.start_claude.call_args_list[1].kwargs["try_continue"] is False

    async def test_start_failure_reports_failure(self, runner, tmux_manager, monkeypatch):
        _fast(monkeypatch)
        tmux_manager.start_claude.return_value = False

        assert await runner.wake() is False

    async def test_pane_that_never_paints_times_out(self, runner, tmux_manager, monkeypatch):
        """A workspace that never reaches its input box is not awake, and saying
        it is would hand the caller an empty screenshot."""
        _fast(monkeypatch)
        tmux_manager.is_claude_running.side_effect = [False] + [True] * 50
        tmux_manager.capture_pane.return_value = "starting…"

        assert await runner.wake(timeout=0.05) is False

    async def test_dead_process_at_a_shell_prompt_is_not_awake(
        self, runner, tmux_manager, monkeypatch
    ):
        """A zsh theme can render the same ``❯`` glyph as Claude's input box, so
        the pane text alone cannot decide this — the process has to be alive."""
        _fast(monkeypatch)
        tmux_manager.is_claude_running.side_effect = [False, True] + [False] * 50

        assert await runner.wake(timeout=0.05) is False

    async def test_accepts_the_trust_dialog(self, runner, tmux_manager, monkeypatch):
        """A recreated window can land on the folder-trust dialog; left
        unanswered it blocks the pane forever."""
        _fast(monkeypatch)
        tmux_manager.is_claude_running.side_effect = [False] + [True] * 50
        trust = (
            "Do you trust the files in this folder?\n❯ 1. Yes, I trust this folder\n  2. No, exit"
        )
        tmux_manager.capture_pane.side_effect = [trust, IDLE_PANE, IDLE_PANE]
        runner._accept_trust_prompt = AsyncMock()

        assert await runner.wake(timeout=1.0) is True
        runner._accept_trust_prompt.assert_awaited_once()


class TestStartClaudeWithoutPrompt:
    """``start_claude(prompt=None)`` — the tmux-level half of the wake."""

    def _manager(self, typed: list[str]):
        from c_lord.tmux import TmuxSessionManager

        mgr = TmuxSessionManager(session_name="c-lord")
        mgr._check_available = MagicMock(return_value=True)  # type: ignore[method-assign]
        mgr._find_window_for_thread = MagicMock(return_value="w1")  # type: ignore[method-assign]
        mgr._pane_path = MagicMock(return_value="/tmp/work")  # type: ignore[method-assign]
        mgr._type_literal = lambda target, text, *, what: (  # type: ignore[method-assign]
            typed.append(text) or True
        )
        return mgr

    def test_no_prompt_means_no_prompt_argument(self):
        typed: list[str] = []
        mgr = self._manager(typed)
        with patch("c_lord.tmux._run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert mgr.start_claude(123, None, "sonnet", try_continue=True) is True

        cmd = typed[0]
        assert "--continue" in cmd
        assert "CLORD_PROMPT" not in cmd, "a prompt file must not be staged for a wake"

    def test_a_prompt_still_rides_in_a_file(self):
        """Regression guard for #529 — the normal path is unchanged."""
        typed: list[str] = []
        mgr = self._manager(typed)
        with patch("c_lord.tmux._run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert mgr.start_claude(123, "hello", "sonnet") is True

        assert "CLORD_PROMPT" in typed[0]


class TestChatCogWakeWorkspace:
    """``ClaudeChatCog.wake_workspace`` — the assembly around the runner (#642)."""

    @staticmethod
    def _cog(tmux_mgr, session_dir_mgr=None):
        import discord

        from c_lord.cogs.claude_chat import ClaudeChatCog

        bot = MagicMock()
        bot.settings_repo = None
        bot.get_cog = MagicMock(return_value=None)
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        repo.touch = AsyncMock()
        repo.set_slept = AsyncMock()
        runner = MagicMock()
        runner.model = "sonnet"
        runner.effort = None
        runner.working_dir = "/fallback"
        runner.timeout_seconds = 300
        cog = ClaudeChatCog(bot=bot, repo=repo, runner=runner)
        cog._resolve_tmux_manager = AsyncMock(return_value=tmux_mgr)  # type: ignore[method-assign]
        cog._resolve_session_dir_manager = AsyncMock(  # type: ignore[method-assign]
            return_value=session_dir_mgr
        )
        cog._apply_thread_naming = AsyncMock()  # type: ignore[method-assign]
        thread = MagicMock(spec=discord.Thread)
        thread.id = 123
        thread.parent_id = 456
        return cog, thread

    async def test_creates_the_window_then_wakes_and_clears_the_sleep_mark(self, monkeypatch):
        _fast(monkeypatch)
        tmux_mgr = MagicMock()
        tmux_mgr.is_claude_running.side_effect = [False, True, True]
        tmux_mgr.start_claude.return_value = True
        tmux_mgr.capture_pane.return_value = IDLE_PANE
        session_dir_mgr = MagicMock()
        session_dir_mgr.create_session_dir = MagicMock(return_value="/work/123")
        cog, thread = self._cog(tmux_mgr, session_dir_mgr)

        assert await cog.wake_workspace(thread) is True

        # The window is gone after a sleep, so it has to be recreated *before*
        # start_claude — which refuses when there is no window.
        tmux_mgr.create_session.assert_called_once_with(123, "/work/123")
        cog.repo.touch.assert_awaited_once_with(123)
        cog.repo.set_slept.assert_awaited_once_with(123, False)

    async def test_marks_the_row_used_before_starting_claude(self, monkeypatch):
        """Observed on staging: the 4-hour sweep killed the window 47s into a
        wake, because the row still carried yesterday's ``last_used_at``. The
        claim has to land before the pane exists, or a reaper can take it."""
        _fast(monkeypatch)
        order: list[str] = []
        tmux_mgr = MagicMock()
        tmux_mgr.is_claude_running.side_effect = [False, True, True]
        tmux_mgr.start_claude.side_effect = lambda *a, **k: order.append("start") or True
        tmux_mgr.create_session.side_effect = lambda *a, **k: order.append("window")
        tmux_mgr.capture_pane.return_value = IDLE_PANE
        cog, thread = self._cog(tmux_mgr)
        cog.repo.touch = AsyncMock(side_effect=lambda _tid: order.append("touch"))

        assert await cog.wake_workspace(thread) is True
        assert order[0] == "touch", order

    async def test_a_failed_wake_leaves_the_sleep_mark_alone(self, monkeypatch):
        """``slept_at`` words the next resume. A restore that never came up did
        not change what happened, so claiming otherwise would make the next
        message announce a crash the user never had."""
        _fast(monkeypatch)
        tmux_mgr = MagicMock()
        tmux_mgr.is_claude_running.return_value = False
        tmux_mgr.start_claude.return_value = False
        cog, thread = self._cog(tmux_mgr)

        assert await cog.wake_workspace(thread) is False
        cog.repo.set_slept.assert_not_awaited()

    async def test_unbound_channel_cannot_be_woken(self):
        cog, thread = self._cog(None)
        assert await cog.wake_workspace(thread) is False


    async def test_waits_for_the_per_thread_setup_lock(self, monkeypatch):
        """Two clicks (or a message landing mid-wake) must not each start a
        Claude into the same pane — the second would type into the first's TUI."""
        import asyncio

        _fast(monkeypatch)
        tmux_mgr = MagicMock()
        tmux_mgr.is_claude_running.side_effect = [False, True, True]
        tmux_mgr.start_claude.return_value = True
        tmux_mgr.capture_pane.return_value = IDLE_PANE
        cog, thread = self._cog(tmux_mgr)

        lock = cog._thread_locks.setdefault(thread.id, asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(cog.wake_workspace(thread))
        await asyncio.sleep(0.01)
        assert tmux_mgr.start_claude.call_count == 0, "started while the lock was held"

        lock.release()
        assert await task is True
        assert tmux_mgr.start_claude.call_count == 1
