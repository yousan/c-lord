# <<REDACTED 10 lines>>
from c_lord.cogs.scheduler import SchedulerCog
from c_lord.database.task_repo import TaskRepository
# <<REDACTED 5 lines>>
    # No settings repo → thread auto-archive resolver uses the 3-day default.
    bot.settings_repo = None
    # <<REDACTED 4 lines>>
    """Create a mock ClaudeConfig with required attributes."""
    # <<REDACTED 1 lines>>
    runner.model = "sonnet"
    runner.working_dir = None
    runner.timeout_seconds = 300
    # <<REDACTED 30 lines>>
            "c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 12 lines>>
            "c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 62 lines>>
        cog._resolve_tmux_manager = AsyncMock(return_value=MagicMock())
# <<REDACTED 2 lines>>
            "c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 12 lines>>
        # Defaults to 3 days (4320 min) when no setting is configured.
        assert mock_starter_msg.create_thread.call_args[1]["auto_archive_duration"] == 4320
# <<REDACTED 13 lines>>
            "c_lord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock
        # <<REDACTED 3 lines>>


class TestScheduledRunMirrorOwnership:
    """#719: last week's thread must not fill up with this week's run."""

    async def test_a_new_run_takes_the_mirror_from_last_runs_thread(
        self, repo: TaskRepository, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Replays the production sequence that leaked for a week (#719).

        Run 1 mirrors into thread A.  The bot restarts: ``on_ready`` restores a
        mirror for A (its session row is open), while the scheduler's in-memory
        "previous thread" note is gone.  Run 2 then starts thread B — and used
        to leave A tailing the same transcript, so every message of run 2 was
        posted into a thread that had been finished for a week.
        """
        from c_lord.cogs.transcript_mirror import TranscriptMirrorCog

        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude" / "projects" / "-fixed-checkout").mkdir(parents=True)
        working_dir = "/fixed/checkout"
        session_repo = MagicMock()
        session_repo.list_all = AsyncMock(return_value=[])

        # ── run 1 ────────────────────────────────────────────────────────
        mirrors = TranscriptMirrorCog(_make_bot(), session_repo=session_repo)
        bot = _make_bot()
        bot.transcript_mirror_cog = mirrors
        cog = SchedulerCog(bot, _make_runner(), repo=repo)
        try:
            await cog._start_transcript_mirror(1, 111, working_dir)
            assert set(mirrors._mirrors) == {111}
        finally:
            await mirrors.cog_unload()

        # ── restart: on_ready restores A's mirror, the scheduler forgets ──
        mirrors = TranscriptMirrorCog(_make_bot(), session_repo=session_repo)
        assert mirrors.start_for(111, working_dir) is True
        bot = _make_bot()
        bot.transcript_mirror_cog = mirrors
        cog = SchedulerCog(bot, _make_runner(), repo=repo)

        # ── run 2 ────────────────────────────────────────────────────────
        try:
            await cog._start_transcript_mirror(1, 222, working_dir)
            assert set(mirrors._mirrors) == {222}
        finally:
            await mirrors.cog_unload()
