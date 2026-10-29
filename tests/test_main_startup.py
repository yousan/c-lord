"""``c_lord.main``: the start-up sequence and the helpers around it.

``main()`` is the one place the pieces meet — config, locks, the bridge, the REST
API, the 30-day sweep and the signal handlers — and it had no test of its own
(``test_main.py`` covers ``load_config`` and the locks one by one). These tests
drive ``main()`` with the bot, the bridge and the sweep stubbed and assert what
it hands to each of them, in the order a real start does it.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord import main as main_mod
from c_lord.main import (
    _env_source,
    acquire_token_lock,
    build_api_server,
    install_shutdown_signal_handlers,
    load_config,
    start_api_server,
)
from c_lord.session_cleanup import DirOutcome


class _FakeBot:
    """Just enough of ``ClaudeDiscordBot`` for ``main()``."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.cogs: dict[str, object] = {}
        self.events: list[str] = []
        self.start = AsyncMock(side_effect=lambda token: self.events.append(f"start:{token}"))
        self.close = AsyncMock(side_effect=lambda: self.events.append("close"))

    async def __aenter__(self) -> _FakeBot:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def get_cog(self, name: str) -> object | None:
        return self.cogs.get(name)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A clean process env with the two required keys, run from *tmp_path*."""
    for key in (
        "CLAUDE_MODEL",
        "CLAUDE_EFFORT",
        "CLAUDE_WORKING_DIR",
        "DISCORD_OWNER_ID",
        "COORDINATION_CHANNEL_ID",
        "EXPECTED_BOT_USER_ID",
        "CLORD_ALLOWED_ROLE",
        "CLORD_ENV_PATH",
        "SESSION_TIMEOUT_SECONDS",
        "MAX_CONCURRENT_SESSIONS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "111")
    monkeypatch.setenv("CLORD_ALLOW_MULTI_INSTANCE", "1")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _components(deleted: Sequence[object], *, claimed: list[str] | None = None) -> SimpleNamespace:
    repo = MagicMock()
    repo.cleanup_old = AsyncMock(return_value=deleted)
    repo.all_working_dirs = AsyncMock(return_value=claimed or [])
    return SimpleNamespace(session_repo=repo, session_dir_base="/sessions")


async def _run_main(
    *,
    deleted: Sequence[object] | None = None,
    claimed: list[str] | None = None,
    outcome: DirOutcome = DirOutcome.REMOVED,
    cleanup_cog: object | None = None,
    api: object | None = None,
) -> SimpleNamespace:
    """Run ``main()`` to the point the bot would connect; return what it touched."""
    seen = SimpleNamespace(bot=None, shutdown=None, setup_kwargs=None, retire=[])

    def make_bot(**kwargs: object) -> _FakeBot:
        bot = _FakeBot(**kwargs)
        if cleanup_cog is not None:
            bot.cogs["SessionCleanupCog"] = cleanup_cog
        seen.bot = bot
        return bot

    async def fake_setup_bridge(bot: object, runner: object, **kwargs: object) -> SimpleNamespace:
        seen.runner = runner
        seen.setup_kwargs = kwargs
        return _components(list(deleted or []), claimed=claimed)

    def fake_retire(record: object, **kwargs: object) -> DirOutcome:
        seen.retire.append((record, kwargs))
        return outcome

    def capture_handlers(loop: object, shutdown: object) -> None:
        seen.shutdown = shutdown

    with (
        patch.object(main_mod, "setup_logging"),
        patch.object(main_mod, "ClaudeDiscordBot", side_effect=make_bot),
        patch.object(main_mod, "build_api_server", AsyncMock(return_value=api)),
        patch.object(main_mod, "setup_bridge", side_effect=fake_setup_bridge),
        patch.object(main_mod, "retire_swept_workspace", side_effect=fake_retire),
        patch.object(main_mod, "sweep_days", return_value=30),
        patch.object(main_mod, "install_shutdown_signal_handlers", side_effect=capture_handlers),
    ):
        await main_mod.main()
    return seen


class TestMainWiring:
    async def test_bot_connects_with_the_token_from_the_env(self, env: Path) -> None:
        seen = await _run_main()

        assert seen.bot.events == ["start:tok"]
        assert seen.bot.kwargs["channel_id"] == 111

    async def test_data_lives_under_data_next_to_the_cwd_without_env_path(self, env: Path) -> None:
        seen = await _run_main()

        assert (env / "data").is_dir()
        assert seen.setup_kwargs["session_db_path"] == str(Path("data") / "sessions.db")
        assert seen.setup_kwargs["task_db_path"] == str(Path("data") / "tasks.db")

    async def test_unset_optional_keys_reach_the_runner_as_none(self, env: Path) -> None:
        seen = await _run_main()

        assert seen.runner.effort is None
        assert seen.runner.working_dir is None
        assert seen.runner.timeout_seconds == 300
        assert seen.setup_kwargs["max_concurrent"] == 3
        assert seen.setup_kwargs["allowed_user_ids"] is None
        assert seen.setup_kwargs["allowed_role_name"] is None

    async def test_owner_role_and_coordination_channel_are_handed_on(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISCORD_OWNER_ID", "42")
        monkeypatch.setenv("COORDINATION_CHANNEL_ID", "777")
        monkeypatch.setenv("EXPECTED_BOT_USER_ID", "9")
        monkeypatch.setenv("CLORD_ALLOWED_ROLE", "devs")
        monkeypatch.setenv("CLAUDE_EFFORT", "high")
        monkeypatch.setenv("SESSION_TIMEOUT_SECONDS", "45")

        seen = await _run_main()

        assert seen.bot.kwargs["owner_id"] == 42
        assert seen.bot.kwargs["coordination_channel_id"] == 777
        assert seen.bot.kwargs["expected_bot_user_id"] == 9
        assert seen.setup_kwargs["allowed_user_ids"] == {42}
        assert seen.setup_kwargs["allowed_role_name"] == "devs"
        assert seen.runner.effort == "high"
        assert seen.runner.timeout_seconds == 45

    async def test_api_is_started_before_the_bot_connects(self, env: Path) -> None:
        order: list[str] = []
        api = SimpleNamespace(
            host="127.0.0.1",
            port=8080,
            start=AsyncMock(side_effect=lambda: order.append("api")),
            stop=AsyncMock(),
        )

        seen = await _run_main(api=api)

        assert order == ["api"]
        assert seen.setup_kwargs["api_server"] is api
        api.start.assert_awaited_once()
        seen.bot.start.assert_awaited_once_with("tok")

    async def test_shutdown_stops_the_api_and_then_closes_the_bot(self, env: Path) -> None:
        events: list[str] = []
        api = SimpleNamespace(
            host="127.0.0.1",
            port=8080,
            start=AsyncMock(),
            stop=AsyncMock(side_effect=lambda: events.append("api.stop")),
        )
        seen = await _run_main(api=api)
        seen.bot.close.side_effect = lambda: events.append("bot.close")

        await seen.shutdown()

        assert events == ["api.stop", "bot.close"]

    async def test_shutdown_without_an_api_still_closes_the_bot(self, env: Path) -> None:
        seen = await _run_main(api=None)

        await seen.shutdown()

        seen.bot.close.assert_awaited_once()


class TestStartupSweep:
    """#554/#575/#902: the 30-day sweep at start-up, before the bot connects."""

    async def test_each_swept_row_retires_its_workspace_under_the_sessions_root(
        self, env: Path
    ) -> None:
        rows = [SimpleNamespace(thread_id=1, working_dir="/sessions/1/1")]
        seen = await _run_main(deleted=rows, claimed=["/live"])

        assert len(seen.retire) == 1
        record, kwargs = seen.retire[0]
        assert record is rows[0]
        assert kwargs == {"sessions_root": "/sessions", "claimed": ["/live"]}

    async def test_a_kept_dirty_checkout_is_logged_with_its_thread(
        self, env: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = [SimpleNamespace(thread_id=5, working_dir="/sessions/1/5")]
        with caplog.at_level(logging.INFO, logger="c_lord.main"):
            await _run_main(deleted=rows, outcome=DirOutcome.KEPT_DIRTY)

        kept = [r.getMessage() for r in caplog.records if "kept" in r.getMessage()]
        assert kept == ["Session cleanup: kept /sessions/1/5 (uncommitted work) thread=5"]

    async def test_swept_rows_are_handed_to_the_notice_cog(self, env: Path) -> None:
        rows = [SimpleNamespace(thread_id=5, working_dir=None)]
        cog = MagicMock()

        await _run_main(deleted=rows, cleanup_cog=cog)

        cog.announce.assert_called_once_with(rows)

    async def test_without_the_notice_cog_the_swept_threads_are_still_logged(
        self, env: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = [SimpleNamespace(thread_id=5, working_dir=None)]
        with caplog.at_level(logging.INFO, logger="c_lord.main"):
            await _run_main(deleted=rows)

        assert any(
            r.getMessage()
            == "Cleaned up 1 session(s) unused for 30+ days: threads=[5] (no notice cog)"
            for r in caplog.records
        )

    async def test_nothing_swept_logs_nothing(
        self, env: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="c_lord.main"):
            await _run_main(deleted=[])

        assert not any("Cleaned up" in r.getMessage() for r in caplog.records)


class _FakeLoop:
    def __init__(self) -> None:
        self.handlers: dict[int, object] = {}
        self.created: list[object] = []

    def add_signal_handler(self, sig: int, cb: object) -> None:
        self.handlers[sig] = cb

    def create_task(self, coro: object) -> None:
        self.created.append(coro)
        coro.close()  # type: ignore[attr-defined]


class TestShutdownSignals:
    """#699/#877: the watchdog is armed first; only the first signal shuts down."""

    def test_sigint_and_sigterm_are_both_handled(self) -> None:
        loop = _FakeLoop()

        install_shutdown_signal_handlers(loop, AsyncMock())  # type: ignore[arg-type]

        assert set(loop.handlers) == {signal.SIGINT, signal.SIGTERM}

    def test_first_signal_arms_the_watchdog_and_starts_one_shutdown(self) -> None:
        loop = _FakeLoop()
        shutdown = AsyncMock()
        install_shutdown_signal_handlers(loop, shutdown)  # type: ignore[arg-type]

        with (
            patch.object(main_mod, "arm_shutdown_watchdog") as arm,
            patch.object(main_mod, "shutdown_timeout_from_env", return_value=12.0),
        ):
            loop.handlers[signal.SIGTERM]()  # type: ignore[operator]

        arm.assert_called_once_with(12.0)
        assert len(loop.created) == 1

    def test_a_repeated_signal_is_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        loop = _FakeLoop()
        install_shutdown_signal_handlers(loop, AsyncMock())  # type: ignore[arg-type]

        with (
            patch.object(main_mod, "arm_shutdown_watchdog") as arm,
            caplog.at_level(logging.INFO, logger="c_lord.main"),
        ):
            loop.handlers[signal.SIGTERM]()  # type: ignore[operator]
            loop.handlers[signal.SIGINT]()  # type: ignore[operator]

        assert arm.call_count == 1
        assert len(loop.created) == 1
        assert any("repeated signal" in r.getMessage() for r in caplog.records)


class TestLoadConfigRequiredKeys:
    @pytest.mark.parametrize("missing", ["DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"])
    def test_a_missing_required_key_exits(
        self, env: Path, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        monkeypatch.delenv(missing)

        with pytest.raises(SystemExit) as exc:
            load_config()

        assert exc.value.code == 1


class TestEnvSource:
    """#918: where CLAUDE_MODEL / CLAUDE_EFFORT came from, for ``/model``."""

    def test_a_key_in_the_env_file_is_reported_as_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("CLAUDE_MODEL=opus\n")
        monkeypatch.setenv("CLAUDE_MODEL", "opus")

        assert _env_source("CLAUDE_MODEL", str(env_file), "既定") == ".env"

    def test_a_key_only_in_the_process_env_is_reported_as_the_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=1\n")
        monkeypatch.setenv("CLAUDE_MODEL", "opus")

        assert _env_source("CLAUDE_MODEL", str(env_file), "既定") == "環境変数"

    def test_an_unreadable_env_file_does_not_stop_start_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)
        with patch("dotenv.dotenv_values", side_effect=PermissionError("denied")):
            assert _env_source("CLAUDE_MODEL", "/nope/.env", "組み込み既定") == "組み込み既定"


class TestTokenLockDefaultDir:
    def test_without_a_lock_dir_the_lock_lives_under_the_home_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLORD_ALLOW_MULTI_INSTANCE", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        handle = acquire_token_lock("tok-default")
        try:
            locks = list((tmp_path / ".cache" / "c-lord" / "locks").iterdir())
            assert len(locks) == 1
            assert locks[0].name.startswith("token-")
        finally:
            assert handle is not None
            handle.close()


class TestApiServerHelpers:
    async def test_without_aiohttp_there_is_no_api_and_it_says_why(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch.dict(sys.modules, {"c_lord.ext.api_server": None}),
            caplog.at_level(logging.WARNING, logger="c_lord.main"),
        ):
            api = await build_api_server(MagicMock(), default_channel_id=1, data_dir=tmp_path)

        assert api is None
        assert any("REST API will NOT start" in r.getMessage() for r in caplog.records)

    async def test_starting_no_api_reports_not_listening(self) -> None:
        assert await start_api_server(None) is False


@pytest.mark.filterwarnings("ignore:.*found in sys.modules:RuntimeWarning")
def test_module_entry_point_runs_main() -> None:
    """``python -m c_lord.main`` hands ``main()`` to ``asyncio.run``."""
    import runpy

    with patch.object(asyncio, "run") as run:
        runpy.run_module("c_lord.main", run_name="__main__")

    run.assert_called_once()
    run.call_args.args[0].close()
