# <<REDACTED 9 lines>>
from c_lord.discord_ui.views import StopView
# <<REDACTED 21 lines>>
    msg.delete = AsyncMock()
    # <<REDACTED 79 lines>>
    async def test_disable_deletes_message(self) -> None:
        """disable() deletes the stop-button message entirely."""
        # <<REDACTED 6 lines>>
        msg.delete.assert_called_once()
# <<REDACTED 3 lines>>
        """disable() after the stop button was clicked should not delete the message."""
        # <<REDACTED 7 lines>>
        msg.delete.assert_not_called()
# <<REDACTED 7 lines>>
        msg.delete = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "rate limited"))
# <<REDACTED 13 lines>>
        msg.delete.assert_called_once()
# <<REDACTED 9 lines>>
    @pytest.mark.asyncio
    async def test_disable_suppresses_runtime_error_session_closed(self) -> None:
        """Issue #91: disable() must not raise RuntimeError('Session is closed').

        During bot shutdown the aiohttp session may already be closed when
        the _run_claude finally block calls disable().  Previously this
        RuntimeError propagated and turned the bot into a zombie.
        """
        # <<REDACTED 3 lines>>
        msg.delete = AsyncMock(side_effect=RuntimeError("Session is closed"))

        await view.disable(msg)  # must not raise

    @pytest.mark.asyncio
    async def test_disable_logs_warning_on_runtime_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """RuntimeError in disable() must produce a WARNING log (not silently swallowed)."""
        import logging

        # <<REDACTED 3 lines>>
        msg.delete = AsyncMock(side_effect=RuntimeError("Session is closed"))

        with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.views"):
            await view.disable(msg)

        assert any(r.levelno >= logging.WARNING for r in caplog.records)

# <<REDACTED 1 lines>>
class TestStopViewSharedStopPath:
    """#878: the button runs the same stop as /stop and !stop (``on_stop``)."""

    @pytest.mark.asyncio
    async def test_click_runs_on_stop_instead_of_the_runner(self) -> None:
        from c_lord.session_stop import StopOutcome, StopResult

        runner = _make_runner()
        on_stop = AsyncMock(return_value=StopResult(StopOutcome.STOPPED, shells_stopped=1))
        view = StopView(runner, on_stop=on_stop)
# <<REDACTED 3 lines>>
        on_stop.assert_awaited_once()
        # <<REDACTED 1 lines>>

    @pytest.mark.asyncio
    async def test_failed_stop_is_not_announced_as_stopped(self) -> None:
        from c_lord.session_stop import FAILED_MESSAGE, StopOutcome, StopResult

        on_stop = AsyncMock(return_value=StopResult(StopOutcome.FAILED))
        view = StopView(_make_runner(), on_stop=on_stop)
        # <<REDACTED 1 lines>>
        interaction.edit_original_response = AsyncMock()
# <<REDACTED 3 lines>>
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs.get("embed") is None
        assert interaction.followup.send.call_args.args[0] == FAILED_MESSAGE
        # The button comes back so the user can try again.
        assert view.stop_button.disabled is False

    @pytest.mark.asyncio
    async def test_runner_reporting_no_c_c_is_a_failure_without_on_stop(self) -> None:
        runner = _make_runner()
        runner.interrupt = AsyncMock(return_value=False)
        # <<REDACTED 2 lines>>
        interaction.edit_original_response = AsyncMock()
# <<REDACTED 3 lines>>
        assert interaction.followup.send.call_args.kwargs.get("embed") is None
