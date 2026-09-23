"""Issue #629 — a webhook-triggered run must have a tmux window to start Claude in.

``WebhookTriggerCog._execute_trigger`` created a thread, built a
``TmuxClaudeRunner`` and called ``run_claude_with_config`` — without ever
creating the window that runner types into.  Nothing downstream creates it
either, so every trigger ended with a thread that holds a single ❌ and no
Claude.  Same shape as #621 (the scheduler), fixed the same way.

The tests below pin:

* the window is created (and the transcript mirror started) before Claude runs,
* a window that could not be created stops the run loudly — ERROR in the log,
  a message in the thread, ❌ on the trigger message, the owner pinged (#681),
* every Cog path that starts Claude creates a window first (structural).
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from c_lord.claude.config import ClaudeConfig
from c_lord.cogs.webhook_trigger import WebhookTrigger, WebhookTriggerCog

_PATCH_RUN = "c_lord.cogs.webhook_trigger.run_claude_with_config"
_PREFIX = "🔄 docs-sync"

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_cog(*, trigger_dir: str | None, default_dir: str | None = "/srv/default"):
    bot = MagicMock(spec=commands.Bot)
    bot.settings_repo = None
    bot.owner_id = None
    runner = ClaudeConfig(
        command="claude",
        model="sonnet",
        working_dir=default_dir,
        timeout_seconds=300,
    )
    cog = WebhookTriggerCog(
        bot=bot,
        runner=runner,
        triggers={_PREFIX: WebhookTrigger(prompt="Sync docs", working_dir=trigger_dir)},
    )
    tmux = MagicMock()
    tmux.create_session = MagicMock(return_value="w7")
    tmux.session_exists = MagicMock(return_value=True)
    cog._resolve_tmux_manager = AsyncMock(return_value=tmux)
    return cog, tmux


def _make_message() -> tuple[MagicMock, MagicMock]:
    msg = MagicMock(spec=discord.Message)
    msg.content = _PREFIX
    msg.webhook_id = 12345
    msg.channel = MagicMock()
    msg.channel.id = 999
    msg.reply = AsyncMock()
    msg.add_reaction = AsyncMock()
    thread = MagicMock(spec=discord.Thread)
    thread.id = 777
    thread.send = AsyncMock()
    msg.create_thread = AsyncMock(return_value=thread)
    return msg, thread


# ---------------------------------------------------------------------------
# AC2 — the window is created before Claude is started
# ---------------------------------------------------------------------------


class TestWebhookTriggerCreatesWindow:
    async def test_execute_trigger_creates_the_tmux_window(self) -> None:
        """The regression: ``_execute_trigger`` never called ``create_session`` at all."""
        cog, tmux = _make_cog(trigger_dir="/home/user/project")
        msg, thread = _make_message()

        with patch(_PATCH_RUN, new_callable=AsyncMock, return_value="sid"):
            await cog.on_message(msg)

        tmux.create_session.assert_called_once_with(thread.id, "/home/user/project")

    async def test_window_is_created_before_claude_runs(self) -> None:
        """Order matters: a window created after the run is a window Claude never saw."""
        cog, tmux = _make_cog(trigger_dir="/tmp/wd")
        msg, _thread = _make_message()

        order: list[str] = []
        tmux.create_session.side_effect = lambda *_: order.append("create_session") or "w7"

        async def _run(_config):
            order.append("run_claude")
            return "sid"

        with patch(_PATCH_RUN, side_effect=_run):
            await cog.on_message(msg)

        assert order == ["create_session", "run_claude"]

    async def test_window_uses_the_runner_default_when_the_trigger_has_no_working_dir(
        self,
    ) -> None:
        """The window and the runner must land in the same directory."""
        cog, tmux = _make_cog(trigger_dir=None, default_dir="/srv/default")
        msg, thread = _make_message()

        with patch(_PATCH_RUN, new_callable=AsyncMock, return_value="sid") as run:
            await cog.on_message(msg)

        tmux.create_session.assert_called_once_with(thread.id, "/srv/default")
        assert run.call_args[0][0].runner.working_dir == "/srv/default"


# ---------------------------------------------------------------------------
# AC4 — Claude's answer has a path back to the thread
# ---------------------------------------------------------------------------


class TestWebhookTriggerStartsTranscriptMirror:
    async def test_mirror_started_for_the_trigger_thread(self) -> None:
        """The jsonl mirror is the only delivery path (#712) — nothing else posts the answer."""
        cog, _tmux = _make_cog(trigger_dir="/home/user/project")
        mirror_cog = MagicMock()
        mirror_cog.start_for = MagicMock(return_value=True)
        cog.bot.transcript_mirror_cog = mirror_cog  # type: ignore[attr-defined]
        msg, thread = _make_message()

        with patch(_PATCH_RUN, new_callable=AsyncMock, return_value="sid"):
            await cog.on_message(msg)

        mirror_cog.start_for.assert_called_once_with(thread.id, "/home/user/project")

    async def test_mirror_failure_does_not_stop_the_run(self) -> None:
        """A mirror that cannot start costs the delivery, not the run itself."""
        cog, _tmux = _make_cog(trigger_dir="/home/user/project")
        mirror_cog = MagicMock()
        mirror_cog.start_for = MagicMock(side_effect=RuntimeError("boom"))
        cog.bot.transcript_mirror_cog = mirror_cog  # type: ignore[attr-defined]
        msg, _thread = _make_message()

        with patch(_PATCH_RUN, new_callable=AsyncMock, return_value="sid") as run:
            await cog.on_message(msg)

        assert run.called


# ---------------------------------------------------------------------------
# AC3 — no window: say so and stop, rather than start Claude at nothing
# ---------------------------------------------------------------------------


class TestWebhookTriggerMissingWindow:
    async def test_missing_window_aborts_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        cog, tmux = _make_cog(trigger_dir="/tmp/wd")
        tmux.session_exists.return_value = False  # creation silently did nothing
        msg, thread = _make_message()

        with (
            caplog.at_level(logging.WARNING, logger="c_lord.cogs.webhook_trigger"),
            patch(_PATCH_RUN, new_callable=AsyncMock) as run,
        ):
            await cog.on_message(msg)

        assert not run.called, "Claude must not be started without a window"
        assert any(r.levelno >= logging.ERROR for r in caplog.records), (
            "a webhook run that never started must not look like a clean exit"
        )
        thread.send.assert_awaited()  # the thread is not left empty
        text = thread.send.await_args.args[0]
        assert "ウィンドウ" in text
        msg.add_reaction.assert_awaited_with("❌")  # the CI message shows the failure too

    async def test_missing_window_pings_the_owner(self) -> None:
        """#681: nobody is in a webhook thread, so the failure has to reach someone."""
        cog, tmux = _make_cog(trigger_dir="/tmp/wd")
        cog.bot.owner_id = 4242
        tmux.session_exists.return_value = False
        msg, thread = _make_message()

        with patch(_PATCH_RUN, new_callable=AsyncMock):
            await cog.on_message(msg)

        text = thread.send.await_args.args[0]
        assert "<@4242>" in text
        allowed = thread.send.await_args.kwargs.get("allowed_mentions")
        assert allowed is not None, "the mention must be allowed to actually ping"

    async def test_missing_window_does_not_ping_when_the_policy_forbids_it(self) -> None:
        """#525: a deployment that turned the owner fallback off gets no mention."""
        cog, tmux = _make_cog(trigger_dir="/tmp/wd")
        cog.bot.owner_id = 4242
        tmux.session_exists.return_value = False
        msg, thread = _make_message()

        with (
            patch("c_lord.cogs.webhook_trigger.owner_notify_id", return_value=None),
            patch(_PATCH_RUN, new_callable=AsyncMock),
        ):
            await cog.on_message(msg)

        assert "<@" not in thread.send.await_args.args[0]


# ---------------------------------------------------------------------------
# Structural — #621 and #629 were the same hole found twice
# ---------------------------------------------------------------------------

_COGS_DIR = Path(__file__).parent.parent / "c_lord" / "cogs"

#: Paths known to start Claude without creating a window first. Each entry must
#: name the Issue that tracks it; fixing the path means deleting its entry here.
_KNOWN_WINDOWLESS: dict[tuple[str, str], str] = {
    ("skill_command.py", "_run_skill_impl"): "#762",
}


def _functions_that_start_claude() -> list[tuple[str, str, bool]]:
    """(file, function, creates_window_first) for each Cog function that runs Claude."""
    found: list[tuple[str, str, bool]] = []
    for path in sorted(_COGS_DIR.glob("*.py")):
        if path.name in {"_run_helper.py", "run_config.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            runs = [
                n.lineno
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "run_claude_with_config"
            ]
            if not runs:
                continue
            creates = [
                n.lineno
                for n in ast.walk(fn)
                if isinstance(n, ast.Attribute) and n.attr == "create_session"
            ]
            found.append((path.name, fn.name, bool(creates) and min(creates) < min(runs)))
    return found


class TestEveryRunPathCreatesAWindow:
    def test_every_cog_that_starts_claude_creates_the_window_first(self) -> None:
        """A new path that starts Claude must create its window, or this names it.

        ``run_claude_with_config`` types into a window it does not create
        (``_run_helper`` / ``tmux_runner`` have no ``create_session``), so the
        caller has to. #621 (scheduler) and #629 (webhook) each forgot.
        """
        missing = [
            f"{f}::{fn}"
            for f, fn, ok in _functions_that_start_claude()
            if not ok and (f, fn) not in _KNOWN_WINDOWLESS
        ]
        assert not missing, (
            f"these start Claude without creating a tmux window first (see #621 / #629): {missing}"
        )

    def test_known_windowless_entries_are_still_windowless(self) -> None:
        """A fixed path must leave the exception list, so the list cannot rot."""
        still_missing = {(f, fn) for f, fn, ok in _functions_that_start_claude() if not ok}
        stale = [f"{f}::{fn}" for (f, fn) in _KNOWN_WINDOWLESS if (f, fn) not in still_missing]
        assert not stale, f"these now create their window — drop them from the list: {stale}"
