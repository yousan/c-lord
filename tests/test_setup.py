"""Tests for c_lord.setup — the one call that assembles the bridge.

``setup_bridge`` is what an instance repo calls instead of wiring Cogs by hand,
so these pin what a caller can rely on: which Cogs end up on the bot for which
arguments, what comes back in :class:`BridgeComponents`, and that the
repositories reach an ``ApiServer`` without further wiring.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from c_lord.setup import BridgeComponents, setup_bridge

#: Attributes under which ``setup_bridge`` leaves a started background loop on the bot.
_LOOP_ATTRS = (
    "thread_state_sync",
    "idle_stop_loop",
    "stopped_sweep_loop",
    "idle_sleep_loop",
    "resident_cap_loop",
    "orphan_sweep_loop",
    "menu_watchdog",
)

#: Cogs every instance gets, whatever it passes, in the order they are added.
_ALWAYS_REGISTERED = [
    "ClaudeChatCog",
    "SessionManageCog",
    "ChannelRepoCog",
    "SessionCleanupCog",
    "TranscriptMirrorCog",
    "LoginRelayCog",
    "VersionCog",
]

_booted_bots: list[MagicMock] = []


def _make_bot() -> MagicMock:
    """A bot that accepts any Cog and remembers which ones it was given."""
    bot = MagicMock()
    bot.add_cog = AsyncMock()
    _booted_bots.append(bot)
    return bot


def _make_runner() -> MagicMock:
    """The ClaudeConfig handed to every Cog that starts Claude."""
    runner = MagicMock()
    runner.working_dir = "/tmp/work"
    runner.clone = MagicMock(return_value=runner)
    return runner


def _session_db(tmp_path: object) -> str:
    return str(Path(str(tmp_path)) / "sessions.db")


def _registered(bot: MagicMock) -> list[str]:
    """Class names of the Cogs added to *bot*, in registration order."""
    return [call.args[0].__class__.__name__ for call in bot.add_cog.call_args_list]


def _started_loops(bot: MagicMock) -> set[str]:
    """Which background loops setup left on *bot* (a MagicMock answers ``hasattr`` to anything)."""
    return {name for name in _LOOP_ATTRS if name in vars(bot)}


async def _boot(tmp_path: object, **kwargs: object) -> tuple[MagicMock, BridgeComponents]:
    """Run ``setup_bridge`` against a throwaway database; no scheduler unless asked."""
    bot = _make_bot()
    kwargs.setdefault("enable_scheduler", False)
    components = await setup_bridge(
        bot,
        _make_runner(),
        session_db_path=_session_db(tmp_path),
        **kwargs,  # type: ignore[arg-type]
    )
    return bot, components


#: Environment ``setup_bridge`` reads that decides which Cogs are registered or
#: where files go. Cleared for every test, so the developer's shell (or a CI
#: job's) cannot change what "nothing configured" means here; a test that is
#: about one of them sets it itself.
_SETUP_ENV = (
    "CLORD_LOGIN_RELAY",
    "CLORD_ALLOWED_ROLE",
    "COORDINATION_CHANNEL_ID",
    "SESSION_DIR_BASE",
    "SESSION_SOURCE_REPO",
    "CLORD_INSTANCE",
)


@pytest.fixture(autouse=True)
def _nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SETUP_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
async def _stop_background_loops() -> AsyncIterator[None]:
    """Stop the sweep loops a boot started, so none outlives its test."""
    _booted_bots.clear()
    yield
    for bot in _booted_bots:
        for name in _started_loops(bot):
            await vars(bot)[name].stop()
    _booted_bots.clear()


@pytest.mark.asyncio
async def test_minimal_boot_registers_the_always_on_cogs_in_order(tmp_path: object) -> None:
    """With nothing optional asked for, the bot gets exactly the always-on Cogs, in order."""
    bot, _ = await _boot(tmp_path)

    assert _registered(bot) == _ALWAYS_REGISTERED


@pytest.mark.asyncio
async def test_scheduler_cog_and_task_db_come_with_enable_scheduler(tmp_path: object) -> None:
    """The default (enable_scheduler=True) adds SchedulerCog last, on its own task database."""
    task_db = Path(str(tmp_path)) / "scheduled" / "tasks.db"

    bot, components = await _boot(tmp_path, enable_scheduler=True, task_db_path=str(task_db))

    assert _registered(bot) == [*_ALWAYS_REGISTERED, "SchedulerCog"]
    assert components.task_repo is not None
    assert task_db.is_file(), "the task database (and its directory) is created on demand"


@pytest.mark.asyncio
async def test_no_scheduler_means_no_cog_no_repo_no_task_db(tmp_path: object) -> None:
    """enable_scheduler=False: no SchedulerCog, no task repository, no tasks.db on disk."""
    task_db = Path(str(tmp_path)) / "scheduled" / "tasks.db"

    bot, components = await _boot(tmp_path, enable_scheduler=False, task_db_path=str(task_db))

    assert "SchedulerCog" not in _registered(bot)
    assert components.task_repo is None
    assert not task_db.parent.exists()


@pytest.mark.asyncio
async def test_components_hand_back_the_repositories_the_bot_got(tmp_path: object) -> None:
    """The return value hands back the repositories setup built — the same ones the bot got."""
    from c_lord.database.lounge_repo import LoungeRepository
    from c_lord.database.repository import SessionRepository
    from c_lord.database.resume_repo import PendingResumeRepository
    from c_lord.database.task_repo import TaskRepository

    sessions_base = str(Path(str(tmp_path)) / "clord-sessions")

    bot, components = await _boot(
        tmp_path,
        enable_scheduler=True,
        task_db_path=str(Path(str(tmp_path)) / "tasks.db"),
        session_dir_base=sessions_base,
    )

    assert isinstance(components, BridgeComponents)
    assert isinstance(components.session_repo, SessionRepository)
    # ...opened on the database the caller named, not one of setup's choosing.
    assert components.session_repo.db_path == _session_db(tmp_path)
    assert isinstance(components.task_repo, TaskRepository)
    assert isinstance(components.lounge_repo, LoungeRepository)
    assert isinstance(components.resume_repo, PendingResumeRepository)
    assert components.session_dir_base == sessions_base
    # Generic Cogs find the repositories on the bot, without importing c-lord internals.
    assert bot.session_repo is components.session_repo
    assert bot.resume_repo is components.resume_repo
    assert bot.lounge_repo is components.lounge_repo


@pytest.mark.asyncio
async def test_session_database_directory_is_created_on_demand(tmp_path: object) -> None:
    """A session_db_path in a directory that does not exist yet is not an error."""
    db_path = Path(str(tmp_path)) / "not" / "yet" / "sessions.db"
    bot = _make_bot()

    await setup_bridge(bot, _make_runner(), session_db_path=str(db_path), enable_scheduler=False)

    assert db_path.is_file()


@pytest.mark.asyncio
async def test_explicit_lounge_channel_wins_over_the_coordination_env(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COORDINATION_CHANNEL_ID is only the fallback for a lounge channel nobody named."""
    monkeypatch.setenv("COORDINATION_CHANNEL_ID", "111")

    bot, _ = await _boot(tmp_path, lounge_channel_id=222)

    assert bot.lounge_channel_id == 222


@pytest.mark.asyncio
async def test_session_source_repo_is_accepted_but_deprecated(tmp_path: object) -> None:
    """The pre-/clord-init way of naming a repo still boots, and says it is on its way out."""
    with pytest.warns(DeprecationWarning, match="/clord-init"):
        bot, _ = await _boot(tmp_path, session_source_repo="https://example.com/repo.git")

    assert _registered(bot) == _ALWAYS_REGISTERED


# ---------------------------------------------------------------------------
# Background loops and their opt-outs
# ---------------------------------------------------------------------------

_LOOP_OPT_OUTS = {
    "CLORD_IDLE_STOP_DAYS": ("0", "idle_stop_loop"),
    "CLORD_STOPPED_SWEEP": ("0", "stopped_sweep_loop"),
    "CLORD_IDLE_SLEEP_HOURS": ("0", "idle_sleep_loop"),
    "CLORD_MAX_RESIDENT_WORKSPACES": ("0", "resident_cap_loop"),
    "CLORD_ORPHAN_SWEEP_DAYS": ("0", "orphan_sweep_loop"),
    "CLORD_MENU_WATCHDOG": ("0", "menu_watchdog"),
}


def _zero_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_LOOP_OPT_OUTS, "CLORD_THREAD_LAMP", "CLORD_THREAD_STATE_SYNC"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_zero_config_starts_every_sweep_but_not_the_thread_lamp(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing set: the housekeeping loops run; the thread-name lamp poll stays off (#329)."""
    _zero_config_env(monkeypatch)

    bot, _ = await _boot(tmp_path, session_dir_base=str(Path(str(tmp_path)) / "sessions"))

    assert _started_loops(bot) == {attr for _, attr in _LOOP_OPT_OUTS.values()}


@pytest.mark.parametrize("env_name", sorted(_LOOP_OPT_OUTS))
@pytest.mark.asyncio
async def test_each_loop_has_its_own_off_switch(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    """Setting one opt-out removes that loop and leaves the others running."""
    _zero_config_env(monkeypatch)
    value, attr = _LOOP_OPT_OUTS[env_name]
    monkeypatch.setenv(env_name, value)

    bot, _ = await _boot(tmp_path, session_dir_base=str(Path(str(tmp_path)) / "sessions"))

    assert _started_loops(bot) == {a for _, a in _LOOP_OPT_OUTS.values()} - {attr}


@pytest.mark.parametrize(
    ("sync_env", "expected"),
    [(None, True), ("0", False)],
    ids=["lamp-on", "lamp-on-poll-off"],
)
@pytest.mark.asyncio
async def test_thread_lamp_opt_in_starts_the_state_sync_poll(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, sync_env: str | None, expected: bool
) -> None:
    """CLORD_THREAD_LAMP=1 brings the poll back; CLORD_THREAD_STATE_SYNC=0 drops just the poll."""
    _zero_config_env(monkeypatch)
    monkeypatch.setenv("CLORD_THREAD_LAMP", "1")
    if sync_env is not None:
        monkeypatch.setenv("CLORD_THREAD_STATE_SYNC", sync_env)

    bot, _ = await _boot(tmp_path)

    assert ("thread_state_sync" in _started_loops(bot)) is expected


@pytest.mark.asyncio
async def test_login_relay_can_be_switched_off(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLORD_LOGIN_RELAY=0 leaves the relay Cog out; everything else is registered as usual."""
    monkeypatch.setenv("CLORD_LOGIN_RELAY", "0")

    bot, _ = await _boot(tmp_path)

    assert _registered(bot) == [name for name in _ALWAYS_REGISTERED if name != "LoginRelayCog"]


def _find_chat_cog(bot: MagicMock) -> object:
    return next(
        call.args[0]
        for call in bot.add_cog.call_args_list
        if call.args[0].__class__.__name__ == "ClaudeChatCog"
    )


@pytest.mark.asyncio
async def test_setup_bridge_forwards_max_concurrent(tmp_path: object) -> None:
    """setup_bridge(max_concurrent=N) must reach ClaudeChatCog's semaphore limit."""
    bot = _make_bot()

    await setup_bridge(
        bot,
        _make_runner(),
        session_db_path=_session_db(tmp_path),
        enable_scheduler=False,
        max_concurrent=30,
    )

    chat_cog = _find_chat_cog(bot)
    assert chat_cog._max_concurrent == 30  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_setup_bridge_max_concurrent_defaults_to_3(tmp_path: object) -> None:
    """Omitting max_concurrent keeps the backward-compatible default of 3."""
    bot = _make_bot()

    await setup_bridge(
        bot,
        _make_runner(),
        session_db_path=_session_db(tmp_path),
        enable_scheduler=False,
    )

    chat_cog = _find_chat_cog(bot)
    assert chat_cog._max_concurrent == 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_skill_cog_needs_the_claude_channel(tmp_path: object) -> None:
    """/skill opens threads in the Claude channel; without one there is nothing to register."""
    bot, _ = await _boot(tmp_path, claude_channel_id=None)

    assert "SkillCommandCog" not in _registered(bot)


@pytest.mark.asyncio
async def test_skill_cog_follows_channel_repo_cog_when_registered(tmp_path: object) -> None:
    """Given the Claude channel, SkillCommandCog is added right after ChannelRepoCog."""
    bot, _ = await _boot(tmp_path, claude_channel_id=12345)

    registered = _registered(bot)
    assert registered.index("SkillCommandCog") == registered.index("ChannelRepoCog") + 1


# ---------------------------------------------------------------------------
# BridgeComponents.apply_to_api_server
# ---------------------------------------------------------------------------


def _api_slots(**preset: object) -> SimpleNamespace:
    """Stand-in for the attributes of an ApiServer that setup fills in."""
    slots = {"session_repo": None, "task_repo": None, "lounge_repo": None, "resume_repo": None}
    return SimpleNamespace(**{**slots, "session_dir_base": None, **preset})


def test_apply_copies_every_built_repository_to_the_server() -> None:
    """Every repository the components hold lands on the server under the same name."""
    from c_lord.database.lounge_repo import LoungeRepository
    from c_lord.database.repository import SessionRepository
    from c_lord.database.task_repo import TaskRepository

    components = BridgeComponents(
        session_repo=MagicMock(spec=SessionRepository),
        task_repo=MagicMock(spec=TaskRepository),
        lounge_repo=MagicMock(spec=LoungeRepository),
        resume_repo=MagicMock(),
        session_dir_base="/srv/clord-sessions",
    )
    api = _api_slots()

    components.apply_to_api_server(api)  # type: ignore[arg-type]

    assert api.session_repo is components.session_repo
    assert api.task_repo is components.task_repo
    assert api.lounge_repo is components.lounge_repo
    assert api.resume_repo is components.resume_repo
    assert api.session_dir_base == "/srv/clord-sessions"


def test_apply_leaves_slots_alone_for_repositories_not_built() -> None:
    """A repository setup did not build must not blank out the one the server already has."""
    from c_lord.database.repository import SessionRepository

    already_there = {"task_repo": object(), "lounge_repo": object(), "resume_repo": object()}
    api = _api_slots(**already_there)
    components = BridgeComponents(session_repo=MagicMock(spec=SessionRepository))

    components.apply_to_api_server(api)  # type: ignore[arg-type]

    assert api.session_repo is components.session_repo
    for slot, value in already_there.items():
        assert getattr(api, slot) is value, slot


def test_apply_twice_equals_apply_once() -> None:
    """Applying twice leaves the server exactly as applying once did."""
    from c_lord.database.lounge_repo import LoungeRepository
    from c_lord.database.repository import SessionRepository
    from c_lord.database.task_repo import TaskRepository

    components = BridgeComponents(
        session_repo=MagicMock(spec=SessionRepository),
        task_repo=MagicMock(spec=TaskRepository),
        lounge_repo=MagicMock(spec=LoungeRepository),
    )
    api = _api_slots()

    components.apply_to_api_server(api)  # type: ignore[arg-type]
    after_once = dict(vars(api))
    components.apply_to_api_server(api)  # type: ignore[arg-type]

    assert vars(api) == after_once


# ---------------------------------------------------------------------------
# setup_bridge(api_server=...)
# ---------------------------------------------------------------------------


def _real_api_server(tmp_path: object, *, port: int = 0) -> object:
    from c_lord.database.notification_repo import NotificationRepository
    from c_lord.ext.api_server import ApiServer

    notifications = NotificationRepository(str(Path(str(tmp_path)) / "notifications.db"))
    return ApiServer(repo=notifications, bot=MagicMock(), default_channel_id=1, port=port)


@pytest.mark.asyncio
async def test_api_server_argument_gets_the_repositories_without_further_wiring(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    """setup_bridge(api_server=...) should auto-wire repos."""
    api = _real_api_server(tmp_path, port=8123)

    with caplog.at_level(logging.INFO, logger="c_lord.setup"):
        _, components = await _boot(
            tmp_path,
            api_server=api,
            enable_scheduler=True,
            task_db_path=str(Path(str(tmp_path)) / "tasks.db"),
        )

    assert api.session_repo is components.session_repo  # type: ignore[attr-defined]
    assert api.task_repo is components.task_repo  # type: ignore[attr-defined]
    assert api.lounge_repo is components.lounge_repo  # type: ignore[attr-defined]
    assert api.resume_repo is components.resume_repo  # type: ignore[attr-defined]
    assert api.session_dir_base == components.session_dir_base  # type: ignore[attr-defined]
    assert "Auto-wired repos to ApiServer (port=8123)" in caplog.messages


@pytest.mark.asyncio
async def test_setup_bridge_does_not_set_runner_api_port(tmp_path: object) -> None:
    """setup_bridge no longer sets runner.api_port (ClaudeConfig doesn't have it)."""
    bot = _make_bot()
    runner = _make_runner()
    runner.api_port = 9999  # Pre-existing value

    await setup_bridge(
        bot,
        runner,
        api_server=_real_api_server(tmp_path, port=8123),  # type: ignore[arg-type]
        session_db_path=_session_db(tmp_path),
        enable_scheduler=False,
    )

    # setup_bridge no longer touches runner.api_port — it stays as-is.
    assert runner.api_port == 9999


# ---------------------------------------------------------------------------
# Global manager generation removed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_global_session_dir_manager(tmp_path: object) -> None:
    """setup_bridge should NOT set bot.session_dir_manager even with env vars."""
    bot = _make_bot()
    runner = _make_runner()

    # Track attribute assignments by wrapping __setattr__
    assigned_attrs: list[str] = []
    original_setattr = type(bot).__setattr__

    def tracking_setattr(self: object, name: str, value: object) -> None:
        assigned_attrs.append(name)
        original_setattr(self, name, value)

    type(bot).__setattr__ = tracking_setattr  # type: ignore[assignment]
    try:
        await setup_bridge(
            bot,
            runner,
            session_db_path=_session_db(tmp_path),
            enable_scheduler=False,
            session_dir_base=str(Path(str(tmp_path)) / "test-base"),
            session_source_repo="https://example.com/repo.git",
        )
    finally:
        type(bot).__setattr__ = original_setattr  # type: ignore[assignment]

    assert "session_dir_manager" not in assigned_attrs


@pytest.mark.asyncio
async def test_no_global_tmux_manager(tmp_path: object) -> None:
    """setup_bridge should NOT set bot.tmux_manager."""
    bot = _make_bot()
    runner = _make_runner()

    assigned_attrs: list[str] = []
    original_setattr = type(bot).__setattr__

    def tracking_setattr(self: object, name: str, value: object) -> None:
        assigned_attrs.append(name)
        original_setattr(self, name, value)

    type(bot).__setattr__ = tracking_setattr  # type: ignore[assignment]
    try:
        await setup_bridge(
            bot,
            runner,
            session_db_path=_session_db(tmp_path),
            enable_scheduler=False,
            enable_tmux=True,
        )
    finally:
        type(bot).__setattr__ = original_setattr  # type: ignore[assignment]

    assert "tmux_manager" not in assigned_attrs


@pytest.mark.asyncio
async def test_setup_bridge_logs_clord_version(
    tmp_path: object, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#722 AC1/AC4: boot must state which build is running.

    Regression for 2026-09-10: an instance ran an 09-02 build for two days and
    told its user a feature "does not exist" that had shipped 09-08. Nothing in
    30+ lines of startup INFO said which build it was — ``grep -i version``
    returned nothing.
    """
    from c_lord.version import runtime_version

    monkeypatch.setattr("c_lord.version.resolve_version", lambda: "v1.4.183-bd80c47e-20260908")
    runtime_version.cache_clear()
    bot = _make_bot()
    runner = _make_runner()

    try:
        with caplog.at_level("INFO", logger="c_lord.setup"):
            await setup_bridge(
                bot,
                runner,
                session_db_path=_session_db(tmp_path),
                enable_scheduler=False,
            )
    finally:
        runtime_version.cache_clear()

    # The line a grepping operator actually types: `grep -i version <log>`.
    hits = [r.getMessage() for r in caplog.records if "version" in r.getMessage().lower()]
    assert hits, "startup log must contain a line matching `grep -i version`"
    assert any("v1.4.183-bd80c47e-20260908" in h for h in hits), hits
    assert any(h.startswith("c-lord version ") for h in hits), hits


async def _boot_log(tmp_path: object, caplog: pytest.LogCaptureFixture, monkeypatch, version: str):
    from c_lord.version import runtime_version

    monkeypatch.setattr("c_lord.version.resolve_version", lambda: version)
    runtime_version.cache_clear()
    try:
        with caplog.at_level("INFO", logger="c_lord.setup"):
            await setup_bridge(
                _make_bot(),
                _make_runner(),
                session_db_path=_session_db(tmp_path),
                enable_scheduler=False,
            )
    finally:
        runtime_version.cache_clear()
    return [r for r in caplog.records if r.levelname == "WARNING" and "days old" in r.getMessage()]


def _days_ago(days: int) -> str:
    from datetime import date, timedelta

    return (date.today() - timedelta(days=days)).strftime("%Y%m%d")


@pytest.mark.asyncio
async def test_setup_bridge_warns_when_build_is_stale(
    tmp_path: object, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#756 AC1: a build 7+ days old says so at boot, with its age.

    Regression for 2026-09-10..18: instances ran builds up to 18 releases
    behind and users kept hitting bugs already fixed on main. The boot log
    named the build (#722) but nobody computes "how many days ago" from
    ``-20260908`` — so the log has to.
    """
    warnings = await _boot_log(tmp_path, caplog, monkeypatch, f"v1.4.183-bd80c47e-{_days_ago(17)}")
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert "17 days old" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_setup_bridge_quiet_when_build_is_fresh(
    tmp_path: object, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#756 AC1: under 7 days, no warning."""
    assert not await _boot_log(tmp_path, caplog, monkeypatch, f"v1.4.197-b3f06814-{_days_ago(6)}")


@pytest.mark.asyncio
async def test_setup_bridge_quiet_when_build_is_undatable(
    tmp_path: object, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#756 AC4: no date in the version → nothing, never ``None days old``."""
    assert not await _boot_log(tmp_path, caplog, monkeypatch, "unknown")


# ---------------------------------------------------------------------------
# Default session-dir base (#837)
# ---------------------------------------------------------------------------


def _channel_repo_base(bot: MagicMock) -> object:
    from c_lord.cogs.channel_repo import ChannelRepoCog

    for call in bot.add_cog.call_args_list:
        cog = call.args[0]
        if isinstance(cog, ChannelRepoCog):
            return cog._session_dir_base
    raise AssertionError("ChannelRepoCog not registered")


async def _boot_for_base(tmp_path, monkeypatch, **kwargs) -> MagicMock:  # noqa: ANN001
    monkeypatch.setenv("CLORD_ORPHAN_SWEEP_DAYS", "0")
    bot = _make_bot()
    await setup_bridge(
        bot,
        _make_runner(),
        session_db_path=str(tmp_path / "db" / "sessions.db"),
        enable_scheduler=False,
        **kwargs,
    )
    return bot


@pytest.mark.asyncio
async def test_session_dir_base_defaults_to_home_dot_c_lord(tmp_path, monkeypatch) -> None:
    """Nothing configured → workspaces go to ~/.c-lord/<clone dir>/sessions (#837)."""
    from pathlib import Path

    clone = tmp_path / "my-clone"
    clone.mkdir()
    monkeypatch.chdir(clone)
    monkeypatch.delenv("SESSION_DIR_BASE", raising=False)
    monkeypatch.delenv("CLORD_INSTANCE", raising=False)
    bot = await _boot_for_base(tmp_path, monkeypatch)
    assert _channel_repo_base(bot) == str(Path.home() / ".c-lord" / "my-clone" / "sessions")


@pytest.mark.asyncio
async def test_session_dir_base_follows_clord_instance(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SESSION_DIR_BASE", raising=False)
    monkeypatch.setenv("CLORD_INSTANCE", "staging-9")
    bot = await _boot_for_base(tmp_path, monkeypatch)
    assert _channel_repo_base(bot) == str(Path.home() / ".c-lord" / "staging-9" / "sessions")


@pytest.mark.asyncio
async def test_session_dir_base_env_is_unchanged(tmp_path, monkeypatch) -> None:
    """An .env that already names SESSION_DIR_BASE keeps it (Zero-Config, #837)."""
    monkeypatch.chdir(tmp_path)
    named = str(tmp_path / "clord-sessions")
    monkeypatch.setenv("SESSION_DIR_BASE", named)
    monkeypatch.setenv("CLORD_INSTANCE", "ignored")
    bot = await _boot_for_base(tmp_path, monkeypatch)
    assert _channel_repo_base(bot) == named


@pytest.mark.asyncio
async def test_session_dir_base_argument_is_unchanged(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SESSION_DIR_BASE", raising=False)
    explicit = str(tmp_path / "explicit")
    bot = await _boot_for_base(tmp_path, monkeypatch, session_dir_base=explicit)
    assert _channel_repo_base(bot) == explicit


@pytest.mark.asyncio
async def test_legacy_data_sessions_keeps_the_old_behaviour(tmp_path, monkeypatch) -> None:
    """An instance already using ./data/sessions is not moved (--resume would break)."""
    (tmp_path / "data" / "sessions").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SESSION_DIR_BASE", raising=False)
    bot = await _boot_for_base(tmp_path, monkeypatch)
    assert _channel_repo_base(bot) is None
