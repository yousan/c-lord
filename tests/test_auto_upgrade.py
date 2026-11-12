"""Upgrading the installed package from a webhook message (``c_lord.cogs.auto_upgrade``).

What an operator sees, from the trigger to the restart:

* which messages count as the trigger, and what happens to a second one while
  the first is still running;
* the progress thread: each command echoed, its output, the ✅ / ❌ verdict on
  the trigger message;
* the wait before a restart — for running turns to finish (drain), and for a
  person to say yes (approval, by reaction or by button);
* the note left for the threads a restart interrupts.

``/upgrade`` — the same pipeline started by a person — is in
``tests/test_upgrade_slash.py``.

Nothing here starts a process, sleeps, or talks to Discord: the subprocess
launcher, ``asyncio.sleep`` and every Discord object are stand-ins.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.config import ClaudeConfig
from c_lord.cogs import auto_upgrade
from c_lord.cogs.auto_upgrade import AutoUpgradeCog, UpgradeApprovalView, UpgradeConfig
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.discord_ui.authorization import Authorizer
from c_lord.thread_settings import DEFAULT_AUTO_ARCHIVE_DURATION

BOT_ID = 9000
HUMAN_ID = 4990
WEBHOOK_ID = 777
CHANNEL_ID = 555
TRIGGER = "🔄 upgrade"
RESTART = ["systemctl", "--user", "restart", "bot.service"]

# Tests replace ``asyncio.sleep``; this is the real one, for letting tasks run.
_real_sleep = asyncio.sleep


async def _let_tasks_run() -> None:
    for _ in range(10):
        await _real_sleep(0)


# ---------------------------------------------------------------------------
# Stand-ins for Discord
# ---------------------------------------------------------------------------


def _posted_message() -> MagicMock:
    """A message that came back from ``send`` — can be reacted to and deleted."""
    message = MagicMock(spec=discord.Message)
    message.id = id(message)
    message.add_reaction = AsyncMock()
    message.delete = AsyncMock()
    return message


def _sendable(spec: type) -> MagicMock:
    """A channel-like object whose ``send`` hands back a fresh message each time.

    The messages are kept, in order, in ``.posted``.
    """
    target = MagicMock(spec=spec)
    target.posted = []

    def send(*_args: Any, **_kwargs: Any) -> MagicMock:
        message = _posted_message()
        target.posted.append(message)
        return message

    target.send = AsyncMock(side_effect=send)
    return target


def make_thread(*, parent: MagicMock | None = None) -> MagicMock:
    thread = _sendable(discord.Thread)
    thread.parent = parent
    return thread


def make_parent() -> MagicMock:
    return _sendable(discord.TextChannel)


def said(target: MagicMock) -> list[str]:
    """Every text posted to *target*, in order."""
    return [call.args[0] for call in target.send.await_args_list]


def make_trigger(
    content: str = TRIGGER,
    *,
    webhook_id: int | None = WEBHOOK_ID,
    channel_id: int = CHANNEL_ID,
    thread: MagicMock | None = None,
) -> MagicMock:
    """A message as the release workflow's webhook would post it."""
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.webhook_id = webhook_id
    message.channel = MagicMock()
    message.channel.id = channel_id
    message.reply = AsyncMock()
    message.add_reaction = AsyncMock()
    message.create_thread = AsyncMock(return_value=thread or make_thread())
    return message


def reactions(message: MagicMock) -> list[str]:
    return [call.args[0] for call in message.add_reaction.await_args_list]


def make_bot(**extra: Any) -> SimpleNamespace:
    """A bot with no cogs and none of the optional repositories, unless given in *extra*."""
    bot = SimpleNamespace(
        cogs={},
        user=SimpleNamespace(id=BOT_ID),
        settings_repo=None,
        wait_for=AsyncMock(),
    )
    vars(bot).update(extra)
    return bot


def make_cog(bot: SimpleNamespace | None = None, **settings: Any) -> AutoUpgradeCog:
    """A cog for package ``pkg``; *settings* go to ``UpgradeConfig`` or the cog itself."""
    cog_options = {
        name: settings.pop(name)
        for name in ("drain_check", "drain_timeout", "drain_poll_interval")
        if name in settings
    }
    config = UpgradeConfig(package_name="pkg", **settings)
    return AutoUpgradeCog(bot or make_bot(), config, **cog_options)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Stand-ins for the operating system
# ---------------------------------------------------------------------------


class FakeShell:
    """Replaces ``asyncio.create_subprocess_exec``.

    Each launch is recorded in :attr:`launches` and :attr:`options`. Commands
    exit 0 with no output unless :meth:`will` scripted something else for the
    program's name (``argv[0]``) or for a whole command line.
    """

    def __init__(self) -> None:
        self.launches: list[list[str]] = []
        self.options: list[dict[str, Any]] = []
        self._scripted: dict[tuple[str, ...], tuple[int, bytes | None, bool]] = {}
        self.launch_error: BaseException | None = None

    def will(
        self, *argv: str, exit_code: int = 0, output: bytes | None = b"", hang: bool = False
    ) -> None:
        self._scripted[argv] = (exit_code, output, hang)

    async def __call__(self, *argv: str, **options: Any) -> MagicMock:
        self.launches.append(list(argv))
        self.options.append(options)
        if self.launch_error is not None:
            raise self.launch_error
        exit_code, output, hang = self._scripted.get(argv, (0, b"", False))

        async def communicate() -> tuple[bytes | None, None]:
            if hang:
                await asyncio.Event().wait()
            return output, None

        process = MagicMock()
        process.returncode = exit_code
        process.communicate = communicate
        return process


@pytest.fixture
def shell(monkeypatch: pytest.MonkeyPatch) -> FakeShell:
    fake = FakeShell()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return fake


@pytest.fixture
def naps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make ``asyncio.sleep`` return at once; the list collects what was asked for."""
    asked: list[float] = []

    async def instant(seconds: float) -> None:
        asked.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", instant)
    return asked


UPGRADE = ["uv", "lock", "--upgrade-package", "pkg"]
SYNC = ["uv", "sync"]


# ---------------------------------------------------------------------------
# UpgradeConfig
# ---------------------------------------------------------------------------


class TestUpgradeConfig:
    def test_only_the_package_name_is_required(self) -> None:
        """Everything else has a default that upgrades without restarting or asking."""
        config = UpgradeConfig(package_name="pkg")

        assert dataclasses.asdict(config) == {
            "package_name": "pkg",
            "trigger_prefix": "🔄 upgrade",
            "working_dir": ".",
            "upgrade_command": None,
            "sync_command": None,
            "restart_command": None,
            "allowed_webhook_ids": None,
            "channel_ids": None,
            "step_timeout": 120,
            "restart_approval": False,
            "upgrade_approval": False,
            "slash_command_enabled": False,
        }

    def test_cannot_be_changed_after_creation(self) -> None:
        """The command lines are fixed at start-up; nothing at runtime may rewrite them."""
        config = UpgradeConfig(package_name="pkg")

        with pytest.raises(dataclasses.FrozenInstanceError):
            config.restart_command = ["rm", "-rf", "/"]  # type: ignore[misc]

    def test_commands_can_be_replaced(self) -> None:
        config = UpgradeConfig(
            package_name="pkg",
            upgrade_command=["pip", "install", "-U", "pkg"],
            sync_command=["true"],
            restart_command=RESTART,
        )

        assert config.upgrade_command == ["pip", "install", "-U", "pkg"]
        assert config.sync_command == ["true"]
        assert config.restart_command == RESTART


# ---------------------------------------------------------------------------
# Which messages are the trigger
# ---------------------------------------------------------------------------


class TestClaimsMessage:
    def test_the_trigger_line_from_a_webhook_is_claimed(self) -> None:
        assert make_cog().claims_message(make_trigger()) is True

    def test_surrounding_whitespace_does_not_matter(self) -> None:
        assert make_cog().claims_message(make_trigger(f"  {TRIGGER}\n")) is True

    def test_a_person_typing_the_trigger_is_not_claimed(self) -> None:
        """Only webhooks trigger: anyone in the channel could type the line."""
        assert make_cog().claims_message(make_trigger(webhook_id=None)) is False

    @pytest.mark.parametrize(
        "content",
        ["something else", f"{TRIGGER} now", f"please {TRIGGER}", TRIGGER.upper(), ""],
        ids=["unrelated", "trailing-words", "leading-words", "other-case", "empty"],
    )
    def test_anything_but_the_exact_line_is_not_claimed(self, content: str) -> None:
        """The whole message must be the trigger; a message merely starting with it is chat."""
        assert make_cog().claims_message(make_trigger(content)) is False

    def test_a_custom_trigger_line_replaces_the_default(self) -> None:
        cog = make_cog(trigger_prefix="deploy!")

        assert cog.claims_message(make_trigger("deploy!")) is True
        assert cog.claims_message(make_trigger(TRIGGER)) is False

    def test_a_webhook_off_the_allowlist_is_not_claimed(self) -> None:
        cog = make_cog(allowed_webhook_ids={WEBHOOK_ID})

        assert cog.claims_message(make_trigger(webhook_id=WEBHOOK_ID)) is True
        assert cog.claims_message(make_trigger(webhook_id=WEBHOOK_ID + 1)) is False

    def test_a_channel_off_the_list_is_not_claimed(self) -> None:
        cog = make_cog(channel_ids={CHANNEL_ID})

        assert cog.claims_message(make_trigger(channel_id=CHANNEL_ID)) is True
        assert cog.claims_message(make_trigger(channel_id=CHANNEL_ID + 1)) is False

    def test_c_lords_own_sub_agent_voice_is_not_claimed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sub-agent quoting the trigger line must not upgrade the bot it runs on."""
        monkeypatch.setattr(auto_upgrade, "is_voice_message", lambda _message: True)

        assert make_cog().claims_message(make_trigger()) is False


class TestIgnoredMessages:
    """A message that is not the trigger leaves no trace at all."""

    @pytest.mark.parametrize(
        ("settings", "message_options"),
        [
            ({}, {"webhook_id": None}),
            ({"allowed_webhook_ids": {1}}, {"webhook_id": 2}),
            ({"channel_ids": {1}}, {"channel_id": 2}),
            ({}, {"content": "🔄 docs-sync"}),
            ({}, {"content": f"{TRIGGER} please"}),
        ],
        ids=["human", "unlisted-webhook", "unlisted-channel", "other-text", "prefix-only"],
    )
    async def test_nothing_runs_and_nothing_is_posted(
        self, shell: FakeShell, settings: dict[str, Any], message_options: dict[str, Any]
    ) -> None:
        cog = make_cog(**settings)
        message = make_trigger(**message_options)

        await cog.on_message(message)

        assert shell.launches == []
        message.create_thread.assert_not_called()
        message.reply.assert_not_called()
        assert reactions(message) == []


# ---------------------------------------------------------------------------
# The pipeline, as seen in the progress thread
# ---------------------------------------------------------------------------


class TestUpgradeWithoutRestart:
    async def test_runs_upgrade_then_sync_in_the_working_directory(self, shell: FakeShell) -> None:
        cog = make_cog(working_dir="/srv/bot")

        await cog.on_message(make_trigger())

        assert shell.launches == [UPGRADE, SYNC]
        assert [options["cwd"] for options in shell.options] == ["/srv/bot", "/srv/bot"]

    async def test_output_is_captured_with_errors_folded_in(self, shell: FakeShell) -> None:
        """stderr goes where stdout goes, so a failing command's message reaches the thread."""
        await make_cog().on_message(make_trigger())

        assert shell.options[0]["stdout"] == asyncio.subprocess.PIPE
        assert shell.options[0]["stderr"] == asyncio.subprocess.STDOUT

    async def test_configured_commands_replace_the_defaults(self, shell: FakeShell) -> None:
        cog = make_cog(upgrade_command=["pip", "install", "-U", "pkg"], sync_command=["true"])

        await cog.on_message(make_trigger())

        assert shell.launches == [["pip", "install", "-U", "pkg"], ["true"]]

    async def test_the_progress_thread_is_opened_on_the_trigger_message(
        self, shell: FakeShell
    ) -> None:
        message = make_trigger()

        await make_cog().on_message(message)

        message.create_thread.assert_awaited_once_with(
            name=TRIGGER, auto_archive_duration=DEFAULT_AUTO_ARCHIVE_DURATION
        )

    async def test_a_long_trigger_line_is_cut_to_a_valid_thread_name(
        self, shell: FakeShell
    ) -> None:
        """Discord refuses thread names over 100 characters."""
        line = "u" * 130
        message = make_trigger(line)

        await make_cog(trigger_prefix=line).on_message(message)

        assert message.create_thread.await_args.kwargs["name"] == "u" * 100

    async def test_the_thread_shows_each_command_and_the_outcome(self, shell: FakeShell) -> None:
        thread = make_thread()

        await make_cog().on_message(make_trigger(thread=thread))

        assert said(thread) == [
            "⚙️ `uv lock --upgrade-package pkg`",
            "⚙️ `uv sync`",
            "✅ Upgrade complete (no restart configured).",
        ]

    async def test_the_trigger_message_gets_a_check_mark(self, shell: FakeShell) -> None:
        message = make_trigger()

        await make_cog().on_message(message)

        assert reactions(message) == ["✅"]

    async def test_command_output_is_shown_in_a_code_block(self, shell: FakeShell) -> None:
        shell.will(*UPGRADE, output=b"  Updated pkg v1.0 -> v1.1\n")
        thread = make_thread()

        await make_cog().on_message(make_trigger(thread=thread))

        assert said(thread)[1] == "```\nUpdated pkg v1.0 -> v1.1\n```"

    async def test_long_output_is_cut_to_fit_one_message(self, shell: FakeShell) -> None:
        shell.will(*UPGRADE, output=b"x" * 5000)
        thread = make_thread()

        await make_cog().on_message(make_trigger(thread=thread))

        assert said(thread)[1] == "```\n" + "x" * 1800 + "\n```"

    async def test_undecodable_output_does_not_break_the_step(self, shell: FakeShell) -> None:
        shell.will(*UPGRADE, output=b"ok \xff\xfe done")
        thread = make_thread()
        message = make_trigger(thread=thread)

        await make_cog().on_message(message)

        assert said(thread)[1] == "```\nok �� done\n```"
        assert reactions(message) == ["✅"]

    @pytest.mark.parametrize("output", [b"", b"  \n\n", None], ids=["empty", "blank", "none"])
    async def test_a_silent_command_posts_no_empty_block(
        self, shell: FakeShell, output: bytes | None
    ) -> None:
        shell.will(*UPGRADE, output=output)
        thread = make_thread()

        await make_cog().on_message(make_trigger(thread=thread))

        assert not any(text.startswith("```") for text in said(thread))

    async def test_nothing_is_restarted(self, shell: FakeShell, naps: list[float]) -> None:
        await make_cog().on_message(make_trigger())

        assert len(shell.launches) == 2
        assert naps == []


class TestFailingSteps:
    async def test_a_failed_upgrade_stops_before_sync(self, shell: FakeShell) -> None:
        shell.will(*UPGRADE, exit_code=2, output=b"error: no such package")
        thread = make_thread()
        message = make_trigger(thread=thread)

        await make_cog(restart_command=RESTART).on_message(message)

        assert shell.launches == [UPGRADE]
        assert said(thread) == [
            "⚙️ `uv lock --upgrade-package pkg`",
            "```\nerror: no such package\n```",
            "❌ `upgrade` failed (exit code 2).",
        ]
        assert reactions(message) == ["❌"]

    async def test_a_failed_sync_stops_before_the_restart(
        self, shell: FakeShell, naps: list[float]
    ) -> None:
        shell.will(*SYNC, exit_code=1)
        thread = make_thread()
        message = make_trigger(thread=thread)

        await make_cog(restart_command=RESTART).on_message(message)

        assert shell.launches == [UPGRADE, SYNC]
        assert said(thread)[-1] == "❌ `sync` failed (exit code 1)."
        assert reactions(message) == ["❌"]

    async def test_a_step_that_outlasts_its_time_limit_is_reported(self, shell: FakeShell) -> None:
        shell.will(*UPGRADE, hang=True)
        thread = make_thread()
        message = make_trigger(thread=thread)

        await make_cog(step_timeout=0).on_message(message)

        assert shell.launches == [UPGRADE]
        assert said(thread)[-1] == "❌ Step timed out."
        assert reactions(message) == ["❌"]

    @pytest.mark.parametrize(
        "timeout_error", [TimeoutError, asyncio.TimeoutError], ids=["builtin", "asyncio"]
    )
    async def test_both_kinds_of_timeout_are_reported_the_same_way(
        self, timeout_error: type[BaseException]
    ) -> None:
        """On Python 3.10 ``asyncio.TimeoutError`` is not the built-in one."""
        cog = make_cog()
        cog._run_step = AsyncMock(side_effect=timeout_error())  # type: ignore[method-assign]
        thread = make_thread()
        message = make_trigger(thread=thread)

        await cog.on_message(message)

        assert said(thread) == ["❌ Step timed out."]
        assert reactions(message) == ["❌"]

    async def test_a_command_that_cannot_be_started_is_reported_not_raised(
        self, shell: FakeShell, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``uv`` missing from PATH must end as a ❌ in Discord, not a dead listener."""
        shell.launch_error = FileNotFoundError("uv")
        thread = make_thread()
        message = make_trigger(thread=thread)

        with caplog.at_level(logging.ERROR, logger="c_lord.cogs.auto_upgrade"):
            await make_cog().on_message(message)

        assert said(thread)[-1] == "❌ Upgrade failed with an unexpected error."
        assert reactions(message) == ["❌"]
        assert "Auto-upgrade error" in caplog.text


class TestRestart:
    async def test_the_restart_command_is_launched_after_upgrade_and_sync(
        self, shell: FakeShell, naps: list[float]
    ) -> None:
        await make_cog(restart_command=RESTART).on_message(make_trigger())

        assert shell.launches == [UPGRADE, SYNC, RESTART]

    async def test_the_restart_is_announced_and_marked_before_it_happens(
        self, shell: FakeShell, naps: list[float]
    ) -> None:
        thread = make_thread()
        message = make_trigger(thread=thread)

        await make_cog(restart_command=RESTART).on_message(message)

        assert said(thread)[-1] == "🔄 Restarting..."
        assert reactions(message) == ["✅"]

    async def test_restart_method_says_so_then_pauses_then_launches(
        self, shell: FakeShell, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pause lets the last messages leave before the process is replaced."""
        thread = make_thread()
        message = make_trigger()
        seen_at_pause: list[Any] = []

        async def pause(seconds: float) -> None:
            seen_at_pause.append((seconds, said(thread), reactions(message), len(shell.launches)))

        monkeypatch.setattr(asyncio, "sleep", pause)

        await make_cog(restart_command=RESTART)._restart(message, thread)

        assert seen_at_pause == [(1, ["🔄 Restarting..."], ["✅"], 0)]
        assert shell.launches == [RESTART]

    async def test_the_restart_command_is_detached_from_the_bot(
        self, shell: FakeShell, naps: list[float]
    ) -> None:
        """Its output goes nowhere and it is not tied to the upgrade's working directory."""
        cog = make_cog(restart_command=RESTART, working_dir="/srv/bot")

        await cog._restart(None, make_thread())

        assert shell.options == [
            {"stdout": asyncio.subprocess.DEVNULL, "stderr": asyncio.subprocess.DEVNULL}
        ]

    async def test_without_a_trigger_message_nothing_is_reacted_to(
        self, shell: FakeShell, naps: list[float]
    ) -> None:
        """``/upgrade`` has no message to mark; the pipeline must cope with ``None``."""
        thread = make_thread()

        await make_cog(restart_command=RESTART)._run_pipeline(thread, None)

        assert shell.launches == [UPGRADE, SYNC, RESTART]
        assert said(thread)[-1] == "🔄 Restarting..."

    async def test_a_failure_without_a_trigger_message_is_still_said_in_the_thread(
        self, shell: FakeShell
    ) -> None:
        shell.will(*UPGRADE, exit_code=1)
        thread = make_thread()

        await make_cog()._run_pipeline(thread, None)

        assert said(thread)[-1] == "❌ `upgrade` failed (exit code 1)."


class TestOneUpgradeAtATime:
    async def test_a_second_trigger_during_an_upgrade_is_turned_away(
        self, shell: FakeShell
    ) -> None:
        cog = make_cog()
        message = make_trigger()

        async with cog._lock:
            await cog.on_message(message)

        message.reply.assert_awaited_once_with("⏳ Upgrade is already running. Skipping.")
        message.create_thread.assert_not_called()
        assert shell.launches == []

    async def test_the_upgrade_holds_the_lock_while_it_runs(self) -> None:
        cog = make_cog()
        held: list[bool] = []

        async def pipeline(_thread: Any, status_target: Any) -> None:
            held.append(cog._lock.locked())

        cog._run_pipeline = pipeline  # type: ignore[method-assign]

        await cog.on_message(make_trigger())

        assert held == [True]
        assert not cog._lock.locked()

    async def test_a_new_upgrade_can_start_once_the_previous_one_ended(
        self, shell: FakeShell
    ) -> None:
        cog = make_cog()

        await cog.on_message(make_trigger())
        await cog.on_message(make_trigger())

        assert shell.launches == [UPGRADE, SYNC, UPGRADE, SYNC]


# ---------------------------------------------------------------------------
# Waiting for running turns (drain)
# ---------------------------------------------------------------------------


def _answers(*values: bool) -> MagicMock:
    """A ``drain_check`` that gives *values* in turn."""
    return MagicMock(side_effect=list(values))


class TestDrain:
    async def test_an_idle_bot_restarts_without_waiting(self, naps: list[float]) -> None:
        thread = make_thread()

        await make_cog(drain_check=lambda: True)._drain(thread)

        assert said(thread) == []
        assert naps == []

    async def test_with_no_other_cogs_there_is_nothing_to_wait_for(self, naps: list[float]) -> None:
        """No ``drain_check`` and no drain-aware cogs: the default answer is "idle"."""
        thread = make_thread()

        await make_cog()._drain(thread)

        assert said(thread) == []
        assert naps == []

    async def test_waits_poll_by_poll_until_the_turns_finish(self, naps: list[float]) -> None:
        check = _answers(False, False, False, True)
        thread = make_thread()
        cog = make_cog(drain_check=check, drain_timeout=300, drain_poll_interval=10)

        await cog._drain(thread)

        assert naps == [10, 10, 10]
        assert said(thread) == [
            "⏳ Upgrade ready — waiting for active sessions to finish (max 300s)...",
            "✅ Sessions finished (30s). Restarting now...",
        ]

    async def test_gives_up_after_the_timeout_and_says_so(self, naps: list[float]) -> None:
        thread = make_thread()
        cog = make_cog(drain_check=lambda: False, drain_timeout=30, drain_poll_interval=10)

        await cog._drain(thread)

        assert naps == [10, 10, 10]
        assert said(thread) == [
            "⏳ Upgrade ready — waiting for active sessions to finish (max 30s)...",
            "⚠️ Drain timeout (30s elapsed) — restarting anyway.",
        ]

    async def test_the_restart_goes_ahead_after_a_drain_timeout(
        self, shell: FakeShell, naps: list[float]
    ) -> None:
        cog = make_cog(
            restart_command=RESTART,
            drain_check=lambda: False,
            drain_timeout=20,
            drain_poll_interval=10,
        )

        await cog.on_message(make_trigger())

        assert shell.launches[-1] == RESTART

    async def test_the_restart_waits_for_the_drain(
        self, shell: FakeShell, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """While turns are running the restart command has not been launched yet."""
        launched_while_draining: list[list[list[str]]] = []
        check = _answers(False, True)

        async def nap(seconds: float) -> None:
            if seconds == 10:  # a drain poll, not the pause before the restart
                launched_while_draining.append(list(shell.launches))

        monkeypatch.setattr(asyncio, "sleep", nap)
        cog = make_cog(restart_command=RESTART, drain_check=check, drain_poll_interval=10)

        await cog.on_message(make_trigger())

        assert launched_while_draining == [[UPGRADE, SYNC]]
        assert shell.launches == [UPGRADE, SYNC, RESTART]


class _Busy:
    """A cog-like object that satisfies ``DrainAware``."""

    def __init__(self, active_count: int) -> None:
        self.active_count = active_count


class TestAutomaticDrainCheck:
    """Without a ``drain_check``, the cog asks every other drain-aware cog on the bot."""

    def test_idle_when_every_drain_aware_cog_reports_zero(self) -> None:
        cog = make_cog(make_bot(cogs={"A": _Busy(0), "B": _Busy(0)}))

        assert cog._auto_drain_check() is True

    def test_busy_when_any_drain_aware_cog_has_work(self) -> None:
        cog = make_cog(make_bot(cogs={"A": _Busy(0), "B": _Busy(2)}))

        assert cog._auto_drain_check() is False

    def test_idle_when_no_cog_is_drain_aware(self) -> None:
        cog = make_cog(make_bot(cogs={"Plain": object()}))

        assert cog._auto_drain_check() is True

    def test_the_upgrade_cog_does_not_wait_for_itself(self) -> None:
        """Even an upgrade cog that counts its own work must not deadlock on it."""

        class CountingUpgradeCog(AutoUpgradeCog):
            active_count = 1

        bot = make_bot()
        cog = CountingUpgradeCog(bot, UpgradeConfig(package_name="pkg"))  # type: ignore[arg-type]
        bot.cogs = {"Upgrade": cog, "Other": _Busy(0)}

        assert cog._auto_drain_check() is True

    async def test_a_given_drain_check_is_used_instead_of_asking_the_cogs(
        self, naps: list[float]
    ) -> None:
        check = MagicMock(return_value=True)
        cog = make_cog(make_bot(cogs={"Busy": _Busy(5)}), drain_check=check)
        thread = make_thread()

        await cog._drain(thread)

        check.assert_called_once_with()
        assert said(thread) == []

    async def test_a_busy_cog_is_waited_for_when_no_check_is_given(self, naps: list[float]) -> None:
        busy = _Busy(1)
        cog = make_cog(make_bot(cogs={"Busy": busy}), drain_timeout=20, drain_poll_interval=10)
        thread = make_thread()

        await cog._drain(thread)

        assert said(thread)[-1] == "⚠️ Drain timeout (20s elapsed) — restarting anyway."


def _chat_cog() -> ClaudeChatCog:
    return ClaudeChatCog(bot=MagicMock(), repo=MagicMock(), runner=MagicMock(spec=ClaudeConfig))


class TestChatCogIsWaitedFor:
    """The real chat cog is what the drain mostly waits for; its count must be honest."""

    def test_a_fresh_chat_cog_has_nothing_running(self) -> None:
        assert _chat_cog().active_session_count == 0

    def test_the_count_follows_the_running_turns(self) -> None:
        chat = _chat_cog()
        chat._active_runners[101] = MagicMock()
        chat._active_runners[102] = MagicMock()

        assert chat.active_session_count == 2
        assert chat.active_count == 2

    def test_a_running_turn_holds_the_restart_back(self) -> None:
        chat = _chat_cog()
        cog = make_cog(make_bot(cogs={"ClaudeChatCog": chat}))
        assert cog._auto_drain_check() is True

        chat._active_runners[101] = MagicMock()

        assert cog._auto_drain_check() is False


# ---------------------------------------------------------------------------
# Remembering the threads a restart interrupts
# ---------------------------------------------------------------------------


def _with_runners(*thread_ids: int) -> SimpleNamespace:
    return SimpleNamespace(_active_runners={thread_id: MagicMock() for thread_id in thread_ids})


def _session_repo(sessions: dict[int, str]) -> MagicMock:
    """A session repository that knows the Claude session id of some threads."""

    async def get(thread_id: int) -> SimpleNamespace | None:
        if thread_id in sessions:
            return SimpleNamespace(session_id=sessions[thread_id])
        return None

    repo = MagicMock()
    repo.get = AsyncMock(side_effect=get)
    return repo


def _resume_repo() -> MagicMock:
    repo = MagicMock()
    repo.mark = AsyncMock()
    return repo


def _marked(resume_repo: MagicMock) -> dict[int, dict[str, Any]]:
    """What was recorded, per thread id."""
    return {call.args[0]: call.kwargs for call in resume_repo.mark.await_args_list}


class TestCollectActiveThreads:
    def test_nothing_running_gives_an_empty_set(self) -> None:
        assert make_cog()._collect_active_thread_ids() == frozenset()

    def test_gathers_the_running_threads_of_every_cog(self) -> None:
        bot = make_bot(cogs={"Chat": _with_runners(1, 2), "Skill": _with_runners(3)})

        assert make_cog(bot)._collect_active_thread_ids() == frozenset({1, 2, 3})

    def test_cogs_that_do_not_track_runners_are_skipped(self) -> None:
        bot = make_bot(
            cogs={
                "Plain": object(),
                "NotADict": SimpleNamespace(_active_runners=[9, 9]),
                "Chat": _with_runners(1),
            }
        )

        assert make_cog(bot)._collect_active_thread_ids() == frozenset({1})

    def test_the_upgrade_cog_itself_is_not_consulted(self) -> None:
        bot = make_bot()
        cog = make_cog(bot)
        cog._active_runners = {99: MagicMock()}  # type: ignore[attr-defined]
        bot.cogs = {"Upgrade": cog, "Chat": _with_runners(1)}

        assert cog._collect_active_thread_ids() == frozenset({1})

    def test_the_result_cannot_be_changed_by_later_turns(self) -> None:
        """It is a snapshot: turns ending during the drain do not shrink it."""
        chat = _with_runners(1, 2)
        cog = make_cog(make_bot(cogs={"Chat": chat}))

        snapshot = cog._collect_active_thread_ids()
        chat._active_runners.clear()

        assert snapshot == frozenset({1, 2})


class TestRestartNotices:
    async def test_no_interrupted_threads_means_nothing_to_record(self) -> None:
        resume_repo = _resume_repo()
        thread = make_thread()

        await make_cog(make_bot(resume_repo=resume_repo))._mark_sessions_for_resume(
            frozenset(), thread
        )

        resume_repo.mark.assert_not_called()
        assert said(thread) == []

    @pytest.mark.parametrize("bot_extra", [{}, {"resume_repo": None}], ids=["absent", "none"])
    async def test_a_bot_without_a_resume_repository_is_left_alone(
        self, bot_extra: dict[str, Any]
    ) -> None:
        """An instance that never wired the repository still upgrades."""
        thread = make_thread()

        await make_cog(make_bot(**bot_extra))._mark_sessions_for_resume(frozenset({1, 2}), thread)

        assert said(thread) == []

    async def test_every_interrupted_thread_is_recorded_as_an_upgrade_restart(self) -> None:
        resume_repo = _resume_repo()
        cog = make_cog(make_bot(resume_repo=resume_repo))

        await cog._mark_sessions_for_resume(frozenset({1, 2, 3}), make_thread())

        expected = {"session_id": None, "reason": "bot_upgrade", "resume_prompt": None}
        assert _marked(resume_repo) == {1: expected, 2: expected, 3: expected}

    async def test_the_claude_session_id_is_recorded_when_it_is_known(self) -> None:
        resume_repo = _resume_repo()
        bot = make_bot(resume_repo=resume_repo, session_repo=_session_repo({1: "abc-123"}))

        await make_cog(bot)._mark_sessions_for_resume(frozenset({1, 2}), make_thread())

        marked = _marked(resume_repo)
        assert marked[1]["session_id"] == "abc-123"
        assert marked[2]["session_id"] is None

    async def test_no_thread_is_told_to_re_run_its_prompt(self) -> None:
        """#406: the restart leaves a notice; it never makes Claude repeat work."""
        resume_repo = _resume_repo()
        bot = make_bot(resume_repo=resume_repo, session_repo=_session_repo({1: "abc-123"}))

        await make_cog(bot)._mark_sessions_for_resume(frozenset({1}), make_thread())

        assert _marked(resume_repo)[1]["resume_prompt"] is None

    async def test_the_upgrade_thread_says_how_many_were_noted(self) -> None:
        thread = make_thread()
        cog = make_cog(make_bot(resume_repo=_resume_repo()))

        await cog._mark_sessions_for_resume(frozenset({1, 2}), thread)

        assert said(thread) == [
            "📌 2 active session(s) noted — a quiet restart notice will be posted (no auto-resume)."
        ]

    async def test_one_thread_failing_does_not_lose_the_others(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        resume_repo = _resume_repo()

        async def mark(thread_id: int, **_fields: Any) -> None:
            if thread_id == 2:
                raise RuntimeError("database is locked")

        resume_repo.mark.side_effect = mark
        thread = make_thread()
        cog = make_cog(make_bot(resume_repo=resume_repo))

        with caplog.at_level(logging.WARNING, logger="c_lord.cogs.auto_upgrade"):
            await cog._mark_sessions_for_resume(frozenset({1, 2, 3}), thread)

        assert set(_marked(resume_repo)) == {1, 2, 3}
        assert said(thread)[0].startswith("📌 2 active session(s) noted")
        assert "Failed to record thread 2 for restart notice" in caplog.text

    async def test_a_failing_session_lookup_only_costs_that_thread(self) -> None:
        resume_repo = _resume_repo()
        session_repo = MagicMock()
        session_repo.get = AsyncMock(side_effect=RuntimeError("database is locked"))
        thread = make_thread()
        cog = make_cog(make_bot(resume_repo=resume_repo, session_repo=session_repo))

        await cog._mark_sessions_for_resume(frozenset({1}), thread)

        resume_repo.mark.assert_not_called()
        assert said(thread) == []

    async def test_when_nothing_could_be_recorded_nothing_is_announced(self) -> None:
        resume_repo = _resume_repo()
        resume_repo.mark.side_effect = RuntimeError("database is locked")
        thread = make_thread()

        await make_cog(make_bot(resume_repo=resume_repo))._mark_sessions_for_resume(
            frozenset({1}), thread
        )

        assert said(thread) == []

    async def test_turns_that_finish_during_the_drain_still_get_their_notice(
        self, shell: FakeShell, naps: list[float]
    ) -> None:
        """The running threads are noted before the drain empties the list of them."""
        chat = _with_runners(41, 42)
        resume_repo = _resume_repo()

        def turns_finish() -> bool:
            chat._active_runners.clear()
            return True

        cog = make_cog(
            make_bot(cogs={"Chat": chat}, resume_repo=resume_repo),
            restart_command=RESTART,
            drain_check=turns_finish,
        )

        await cog.on_message(make_trigger())

        assert set(_marked(resume_repo)) == {41, 42}

    async def test_the_notices_are_recorded_before_the_restart_command_runs(
        self, shell: FakeShell, naps: list[float]
    ) -> None:
        resume_repo = _resume_repo()
        launches_when_marked: list[int] = []

        async def mark(_thread_id: int, **_fields: Any) -> None:
            launches_when_marked.append(len(shell.launches))

        resume_repo.mark.side_effect = mark
        cog = make_cog(
            make_bot(cogs={"Chat": _with_runners(41)}, resume_repo=resume_repo),
            restart_command=RESTART,
            drain_check=lambda: True,
        )

        await cog.on_message(make_trigger())

        assert launches_when_marked == [2]  # upgrade and sync only
        assert shell.launches[-1] == RESTART


# ---------------------------------------------------------------------------
# The approval button
# ---------------------------------------------------------------------------


def _press(user_id: int) -> MagicMock:
    """An interaction from *user_id* pressing a button."""
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


class TestUpgradeApprovalView:
    async def test_shows_one_green_approve_button(self) -> None:
        view = UpgradeApprovalView(approved_event=asyncio.Event())

        (button,) = view.children
        assert isinstance(button, discord.ui.Button)
        assert button.label == "✅ Approve"
        assert button.style == discord.ButtonStyle.green
        assert button.disabled is False

    async def test_the_label_can_be_chosen(self) -> None:
        view = UpgradeApprovalView(approved_event=asyncio.Event(), label="🚀 Ship it")

        assert view.children[0].label == "🚀 Ship it"  # type: ignore[attr-defined]

    async def test_never_expires(self) -> None:
        """Approval may come hours later; the button must still work then."""
        assert UpgradeApprovalView(approved_event=asyncio.Event()).timeout is None

    async def test_a_press_approves(self) -> None:
        approved = asyncio.Event()
        view = UpgradeApprovalView(approved_event=approved, bot_id=BOT_ID)

        await view.approve.callback(_press(HUMAN_ID))

        assert approved.is_set()

    async def test_a_press_retires_the_button(self) -> None:
        """The message is redrawn with a disabled "Approved" button, and the view is over."""
        view = UpgradeApprovalView(approved_event=asyncio.Event(), bot_id=BOT_ID)
        interaction = _press(HUMAN_ID)

        await view.approve.callback(interaction)

        assert view.approve.disabled is True
        assert view.approve.label == "✅ Approved"
        interaction.response.edit_message.assert_awaited_once_with(view=view)
        assert view.is_finished()

    async def test_the_bots_own_press_decides_nothing(self) -> None:
        approved = asyncio.Event()
        view = UpgradeApprovalView(approved_event=approved, bot_id=BOT_ID)
        interaction = _press(BOT_ID)

        await view.approve.callback(interaction)

        assert not approved.is_set()
        assert view.approve.disabled is False
        interaction.response.defer.assert_awaited_once_with()
        interaction.response.edit_message.assert_not_called()
        assert not view.is_finished()

    async def test_without_a_bot_id_any_press_approves(self) -> None:
        approved = asyncio.Event()
        view = UpgradeApprovalView(approved_event=approved)

        await view.approve.callback(_press(BOT_ID))

        assert approved.is_set()

    async def test_who_may_press_is_decided_by_the_given_authorizer(self) -> None:
        view = UpgradeApprovalView(
            approved_event=asyncio.Event(), authorizer=Authorizer(allowed_user_ids={HUMAN_ID})
        )
        stranger = MagicMock()
        stranger.user = MagicMock(spec=discord.User)
        stranger.user.id = HUMAN_ID + 1
        stranger.response.send_message = AsyncMock()
        owner = MagicMock()
        owner.user = MagicMock(spec=discord.User)
        owner.user.id = HUMAN_ID

        assert await view.interaction_check(stranger) is False
        assert await view.interaction_check(owner) is True


# ---------------------------------------------------------------------------
# Waiting for a person to approve
# ---------------------------------------------------------------------------


def _reaction(message: MagicMock, *, user_id: int = HUMAN_ID, emoji: str = "✅") -> Any:
    return SimpleNamespace(
        message_id=message.id, emoji=discord.PartialEmoji(name=emoji), user_id=user_id
    )


def _question(thread: MagicMock) -> MagicMock:
    """The message in *thread* that carries the ✅ people are asked to press."""
    return next(message for message in thread.posted if message.add_reaction.await_count)


class Gateway:
    """Stands in for ``bot.wait_for``.

    *script* is what happens on successive waits: an exception instance is
    raised; a callable is given the question message and returns the reaction
    event that arrives. As in discord.py, an event the caller's ``check``
    rejects is skipped. Once the script is used up the wait never returns.
    """

    def __init__(self, thread: MagicMock, *script: Any) -> None:
        self._thread = thread
        self._script = list(script)
        self.waits: list[tuple[str, float]] = []

    async def __call__(self, event: str, *, check: Any, timeout: float) -> Any:
        self.waits.append((event, timeout))
        while self._script:
            item = self._script.pop(0)
            if isinstance(item, BaseException):
                raise item
            payload = item(_question(self._thread))
            if check(payload):
                return payload
        await asyncio.Event().wait()


async def _run_approval(cog: AutoUpgradeCog, thread: MagicMock, **options: Any) -> None:
    """Run the approval to its end; a wait that never ends fails the test instead of hanging."""
    await asyncio.wait_for(cog._wait_for_approval(None, thread, **options), timeout=5)


def _button_of(parent: MagicMock) -> UpgradeApprovalView:
    return parent.send.await_args.kwargs["view"]


class TestApprovalByReaction:
    async def test_the_question_is_posted_with_a_check_mark_to_press(self) -> None:
        thread = make_thread()
        bot = make_bot(wait_for=Gateway(thread, _reaction))

        await _run_approval(make_cog(bot), thread)

        assert said(thread)[0] == "📦 Update installed. React ✅ on this message to restart."
        _question(thread).add_reaction.assert_awaited_once_with("✅")

    async def test_a_persons_reaction_approves(self, caplog: pytest.LogCaptureFixture) -> None:
        thread = make_thread()
        gateway = Gateway(thread, _reaction)

        with caplog.at_level(logging.INFO, logger="c_lord.cogs.auto_upgrade"):
            await _run_approval(make_cog(make_bot(wait_for=gateway), drain_timeout=45), thread)

        assert said(thread)[-1] == "👍 Restart approved!"
        assert gateway.waits == [("raw_reaction_add", 45.0)]
        assert f"Restart approved by user {HUMAN_ID}" in caplog.text

    async def test_a_custom_question_replaces_the_default(self) -> None:
        thread = make_thread()
        bot = make_bot(wait_for=Gateway(thread, _reaction))

        await _run_approval(make_cog(bot), thread, prompt="Ready when you are.")

        assert said(thread)[0] == "Ready when you are."

    @pytest.mark.parametrize(
        "not_an_approval",
        [
            lambda question: _reaction(question, user_id=BOT_ID),
            lambda question: _reaction(question, emoji="👀"),
            lambda question: _reaction(MagicMock(id=question.id + 1)),
        ],
        ids=["the-bots-own", "another-emoji", "another-message"],
    )
    async def test_reactions_that_are_not_an_approval_keep_it_waiting(
        self, not_an_approval: Any
    ) -> None:
        """The bot's own ✅ is on the question from the start; it must not approve itself."""
        thread = make_thread()
        cog = make_cog(make_bot(wait_for=Gateway(thread, not_an_approval)))

        waiting = asyncio.create_task(cog._wait_for_approval(None, thread))
        await _let_tasks_run()

        assert not waiting.done()
        assert "👍 Restart approved!" not in said(thread)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    async def test_a_bot_that_does_not_know_its_own_user_accepts_any_reactor(self) -> None:
        thread = make_thread()
        bot = make_bot(user=None, wait_for=Gateway(thread, lambda q: _reaction(q, user_id=BOT_ID)))

        await _run_approval(make_cog(bot), thread)

        assert said(thread)[-1] == "👍 Restart approved!"

    @pytest.mark.parametrize(
        "timeout_error", [TimeoutError, asyncio.TimeoutError], ids=["builtin", "asyncio"]
    )
    async def test_silence_is_answered_with_a_reminder_and_more_waiting(
        self, timeout_error: type[BaseException]
    ) -> None:
        thread = make_thread()
        gateway = Gateway(thread, timeout_error(), timeout_error(), _reaction)

        await _run_approval(make_cog(make_bot(wait_for=gateway)), thread)

        reminder = (
            "⏳ Still waiting for restart approval... "
            "React ✅ above or click the button in the channel."
        )
        assert said(thread)[1:] == [reminder, reminder, "👍 Restart approved!"]
        assert len(gateway.waits) == 3

    async def test_an_unexpected_failure_while_waiting_is_not_hidden(self) -> None:
        thread = make_thread()
        bot = make_bot(wait_for=Gateway(thread, RuntimeError("gateway closed")))

        with pytest.raises(RuntimeError, match="gateway closed"):
            await _run_approval(make_cog(bot), thread)

        assert "👍 Restart approved!" not in said(thread)

    async def test_a_thread_without_a_parent_channel_asks_only_in_the_thread(self) -> None:
        thread = make_thread(parent=None)
        bot = make_bot(wait_for=Gateway(thread, _reaction))

        await _run_approval(make_cog(bot), thread)

        assert said(thread)[-1] == "👍 Restart approved!"


class TestApprovalByButton:
    async def test_the_parent_channel_gets_the_question_with_a_button(self) -> None:
        """A thread is easy to miss; the channel people watch carries the button."""
        parent = make_parent()
        thread = make_thread(parent=parent)
        bot = make_bot(wait_for=Gateway(thread, _reaction))

        await _run_approval(make_cog(bot), thread)

        assert said(parent) == [
            "🔔 **Approval needed** — 📦 Update installed. React ✅ on this message to restart.\n"
            "(React ✅ in the upgrade thread above, or click here ↓)"
        ]
        assert isinstance(_button_of(parent), UpgradeApprovalView)

    async def test_the_button_knows_the_bot_and_the_allowlist(self) -> None:
        authorizer = Authorizer(allowed_user_ids={HUMAN_ID})
        parent = make_parent()
        thread = make_thread(parent=parent)
        bot = make_bot(wait_for=Gateway(thread, _reaction), authorizer=authorizer)

        await _run_approval(make_cog(bot), thread)

        view = _button_of(parent)
        assert view._authorizer is authorizer
        await view.approve.callback(_press(BOT_ID))  # the bot's own press is recognised
        assert not view.is_finished()

    async def test_pressing_the_button_approves_without_any_reaction(self) -> None:
        """The reaction wait never returns here; the button alone must be enough."""
        parent = make_parent()
        thread = make_thread(parent=parent)
        cog = make_cog(make_bot(wait_for=Gateway(thread)))

        waiting = asyncio.create_task(cog._wait_for_approval(None, thread))
        await _let_tasks_run()
        assert not waiting.done()
        await _button_of(parent).approve.callback(_press(HUMAN_ID))
        await asyncio.wait_for(waiting, timeout=5)

        assert said(thread)[-1] == "👍 Restart approved!"

    async def test_the_button_exists_as_soon_as_the_wait_begins(self) -> None:
        """Both posts are out before the first pause, so an early press is never lost."""
        parent = make_parent()
        thread = make_thread(parent=parent)
        cog = make_cog(make_bot(wait_for=Gateway(thread)))

        waiting = asyncio.create_task(cog._wait_for_approval(None, thread))
        await _let_tasks_run()

        assert len(said(thread)) == 1
        assert len(said(parent)) == 1
        _button_of(parent)._event.set()
        await asyncio.wait_for(waiting, timeout=5)

    @pytest.mark.parametrize("approve_with", ["reaction", "button"])
    async def test_the_channel_message_is_removed_once_approved(self, approve_with: str) -> None:
        parent = make_parent()
        button_message = _posted_message()
        parent.send = AsyncMock(return_value=button_message)
        thread = make_thread(parent=parent)
        script = (_reaction,) if approve_with == "reaction" else ()
        cog = make_cog(make_bot(wait_for=Gateway(thread, *script)))

        waiting = asyncio.create_task(cog._wait_for_approval(None, thread))
        await _let_tasks_run()
        if approve_with == "button":
            _button_of(parent)._event.set()
        await asyncio.wait_for(waiting, timeout=5)

        button_message.delete.assert_awaited_once_with()

    async def test_a_channel_message_someone_already_deleted_is_not_an_error(self) -> None:
        parent = make_parent()
        button_message = _posted_message()
        button_message.delete.side_effect = discord.NotFound(
            MagicMock(status=404, reason="Not Found"), "Unknown Message"
        )
        parent.send = AsyncMock(return_value=button_message)
        thread = make_thread(parent=parent)
        bot = make_bot(wait_for=Gateway(thread, _reaction))

        await _run_approval(make_cog(bot), thread)

        assert said(thread)[-1] == "👍 Restart approved!"

    async def test_a_channel_that_refuses_the_button_leaves_the_reaction_working(self) -> None:
        """No permission to post in the parent channel must not block the upgrade."""
        parent = make_parent()
        parent.send = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no access")
        )
        thread = make_thread(parent=parent)
        bot = make_bot(wait_for=Gateway(thread, _reaction))

        await _run_approval(make_cog(bot), thread)

        assert said(thread)[-1] == "👍 Restart approved!"


class TestApprovalInThePipeline:
    @staticmethod
    def _recording_approval(cog: AutoUpgradeCog, shell: FakeShell) -> list[dict[str, Any]]:
        """Replace the wait with a recorder of when, and with what question, it was asked."""
        asked: list[dict[str, Any]] = []

        async def approve(_status: Any, _thread: Any, *, prompt: str | None = None) -> None:
            asked.append({"prompt": prompt, "launched": list(shell.launches)})

        cog._wait_for_approval = approve  # type: ignore[method-assign]
        return asked

    async def test_by_default_nobody_is_asked(self, shell: FakeShell, naps: list[float]) -> None:
        cog = make_cog(restart_command=RESTART)
        asked = self._recording_approval(cog, shell)

        await cog.on_message(make_trigger())

        assert asked == []
        assert shell.launches[-1] == RESTART

    async def test_restart_approval_is_asked_after_installing_and_before_restarting(
        self, shell: FakeShell, naps: list[float]
    ) -> None:
        cog = make_cog(restart_command=RESTART, restart_approval=True)
        asked = self._recording_approval(cog, shell)

        await cog.on_message(make_trigger())

        assert asked == [{"prompt": None, "launched": [UPGRADE, SYNC]}]
        assert shell.launches[-1] == RESTART

    async def test_restart_approval_without_a_restart_command_asks_nothing(
        self, shell: FakeShell
    ) -> None:
        cog = make_cog(restart_approval=True)
        asked = self._recording_approval(cog, shell)

        await cog.on_message(make_trigger())

        assert asked == []

    async def test_upgrade_approval_is_asked_before_anything_runs(self, shell: FakeShell) -> None:
        cog = make_cog(upgrade_approval=True)
        asked = self._recording_approval(cog, shell)

        await cog.on_message(make_trigger())

        assert asked == [
            {
                "prompt": (
                    "📦 New release of **pkg** detected. "
                    "React ✅ on this message to start the upgrade."
                ),
                "launched": [],
            }
        ]
        assert shell.launches == [UPGRADE, SYNC]

    async def test_the_restart_really_waits_for_the_approval(
        self, shell: FakeShell, naps: list[float]
    ) -> None:
        """End to end: no restart until the ✅ arrives, restart right after."""
        thread = make_thread()
        someone_reacts = asyncio.Event()

        async def wait_for(_event: str, *, check: Any, timeout: float) -> Any:
            await someone_reacts.wait()
            return _reaction(_question(thread))

        cog = make_cog(
            make_bot(wait_for=wait_for),
            restart_command=RESTART,
            restart_approval=True,
            drain_check=lambda: True,
        )
        upgrade = asyncio.create_task(cog.on_message(make_trigger(thread=thread)))
        await _let_tasks_run()
        assert shell.launches == [UPGRADE, SYNC]

        someone_reacts.set()
        await asyncio.wait_for(upgrade, timeout=5)

        assert shell.launches == [UPGRADE, SYNC, RESTART]
