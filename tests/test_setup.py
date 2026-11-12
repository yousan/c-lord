# <<REDACTED 8 lines>>
from c_lord.setup import BridgeComponents, setup_bridge
# <<REDACTED 91 lines>>
def _find_chat_cog(bot: MagicMock) -> object:
    return next(
        call.args[0]
        for call in bot.add_cog.call_args_list
        if call.args[0].__class__.__name__ == "ClaudeChatCog"
    )


@pytest.mark.asyncio
async def test_setup_bridge_forwards_max_concurrent(tmp_path: object) -> None:
    """setup_bridge(max_concurrent=N) must reach ClaudeChatCog's semaphore limit."""
    # <<REDACTED 8 lines>>
        max_concurrent=30,
    )

    chat_cog = _find_chat_cog(bot)
    assert chat_cog._max_concurrent == 30  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_setup_bridge_max_concurrent_defaults_to_3(tmp_path: object) -> None:
    """Omitting max_concurrent keeps the backward-compatible default of 3."""
    # <<REDACTED 8 lines>>
    )

    chat_cog = _find_chat_cog(bot)
    assert chat_cog._max_concurrent == 3  # type: ignore[attr-defined]


# <<REDACTED 33 lines>>
    from c_lord.database.lounge_repo import LoungeRepository
    from c_lord.database.repository import SessionRepository
    from c_lord.database.task_repo import TaskRepository
# <<REDACTED 20 lines>>
    from c_lord.database.repository import SessionRepository
# <<REDACTED 19 lines>>
    from c_lord.database.lounge_repo import LoungeRepository
    from c_lord.database.repository import SessionRepository
    from c_lord.database.task_repo import TaskRepository
# <<REDACTED 21 lines>>
    """setup_bridge(api_server=...) should auto-wire repos."""
    # <<REDACTED 19 lines>>
async def test_setup_bridge_does_not_set_runner_api_port(tmp_path: object) -> None:
    """setup_bridge no longer sets runner.api_port (ClaudeConfig doesn't have it)."""
    # <<REDACTED 2 lines>>
    runner.api_port = 9999  # Pre-existing value
    # <<REDACTED 11 lines>>
    # setup_bridge no longer touches runner.api_port — it stays as-is.
    # <<REDACTED 1 lines>>


# ---------------------------------------------------------------------------
# Global manager generation removed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_global_session_dir_manager(tmp_path: object) -> None:
    """setup_bridge should NOT set bot.session_dir_manager even with env vars."""
    # <<REDACTED 2 lines>>

    # Track attribute assignments by wrapping __setattr__
    assigned_attrs: list[str] = []
    original_setattr = type(bot).__setattr__

    def tracking_setattr(self: object, name: str, value: object) -> None:
        assigned_attrs.append(name)
        original_setattr(self, name, value)

    type(bot).__setattr__ = tracking_setattr  # type: ignore[assignment]
    try:
        # <<REDACTED 5 lines>>
            session_dir_base="/tmp/test-base",
            session_source_repo="https://example.com/repo.git",
        )
    finally:
        type(bot).__setattr__ = original_setattr  # type: ignore[assignment]

    assert "session_dir_manager" not in assigned_attrs


@pytest.mark.asyncio
async def test_no_global_tmux_manager(tmp_path: object) -> None:
    """setup_bridge should NOT set bot.tmux_manager."""
    # <<REDACTED 3 lines>>
    assigned_attrs: list[str] = []
    original_setattr = type(bot).__setattr__

    def tracking_setattr(self: object, name: str, value: object) -> None:
        assigned_attrs.append(name)
        original_setattr(self, name, value)

    type(bot).__setattr__ = tracking_setattr  # type: ignore[assignment]
    try:
        # <<REDACTED 5 lines>>
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
    # <<REDACTED 2 lines>>

    try:
        with caplog.at_level("INFO", logger="c_lord.setup"):
            # <<REDACTED 5 lines>>
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
                # <<REDACTED 2 lines>>
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
        # <<REDACTED 1 lines>>
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
    monkeypatch.setenv("SESSION_DIR_BASE", "/srv/clord-sessions")
    monkeypatch.setenv("CLORD_INSTANCE", "ignored")
    bot = await _boot_for_base(tmp_path, monkeypatch)
    assert _channel_repo_base(bot) == "/srv/clord-sessions"


@pytest.mark.asyncio
async def test_session_dir_base_argument_is_unchanged(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SESSION_DIR_BASE", raising=False)
    bot = await _boot_for_base(tmp_path, monkeypatch, session_dir_base="/srv/explicit")
    assert _channel_repo_base(bot) == "/srv/explicit"


@pytest.mark.asyncio
async def test_legacy_data_sessions_keeps_the_old_behaviour(tmp_path, monkeypatch) -> None:
    """An instance already using ./data/sessions is not moved (--resume would break)."""
    (tmp_path / "data" / "sessions").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SESSION_DIR_BASE", raising=False)
    bot = await _boot_for_base(tmp_path, monkeypatch)
    assert _channel_repo_base(bot) is None
