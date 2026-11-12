"""The ⏹ Stop button: what a press does, and how the button goes away."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.discord_ui.views import StopView


def _make_runner() -> MagicMock:
    """A runner whose ``interrupt()`` reports nothing (older runners: counts as sent)."""
    runner = MagicMock()
    runner.interrupt = AsyncMock(return_value=None)
    return runner


def _make_interaction() -> MagicMock:
    """A button press: the initial response and the followup are both awaitable."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.extras = {}
    return interaction


def _make_message() -> MagicMock:
    """The "session running" message the button sits on."""
    msg = MagicMock(spec=discord.Message)
    msg.delete = AsyncMock()
    return msg


async def _press(view: StopView, interaction: MagicMock | None = None) -> MagicMock:
    """Press ⏹ Stop the way discord.py would, and hand back the interaction."""
    interaction = interaction or _make_interaction()
    await view.stop_button.callback(interaction)
    return interaction


class TestStopViewShape:
    """What the control is, before anyone touches it."""

    @pytest.mark.asyncio
    async def test_one_red_stop_button_that_never_times_out(self) -> None:
        view = StopView(_make_runner())
        (button,) = view.children
        assert button.label == "⏹ Stop"
        assert button.style is discord.ButtonStyle.danger
        assert button.disabled is False
        assert view.timeout is None


class TestPressingStop:
    """A press interrupts the turn, greys the button out, and says what happened."""

    @pytest.mark.asyncio
    async def test_press_interrupts_the_runner(self) -> None:
        runner = _make_runner()
        await _press(StopView(runner))
        runner.interrupt.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_press_greys_the_button_out_for_good(self) -> None:
        view = StopView(_make_runner())
        await _press(view)
        assert view.stop_button.disabled is True
        assert view.is_finished()

    @pytest.mark.asyncio
    async def test_press_is_acknowledged_by_redrawing_the_message(self) -> None:
        """The press is acknowledged by redrawing the message with the greyed button."""
        view = StopView(_make_runner())
        interaction = await _press(view)
        interaction.response.edit_message.assert_awaited_once_with(view=view)

    @pytest.mark.asyncio
    async def test_button_is_greyed_out_before_the_stop_runs(self) -> None:
        """The stop can take seconds; the user must see the press was taken at once."""
        seen: list[bool] = []
        view = StopView(_make_runner())
        interaction = _make_interaction()

        async def interrupt() -> None:
            seen.append(view.stop_button.disabled)
            seen.append(interaction.response.edit_message.await_count == 1)

        view._runner.interrupt = interrupt
        await _press(view, interaction)
        assert seen == [True, True]

    @pytest.mark.asyncio
    async def test_stopped_notice_follows_in_the_thread(self) -> None:
        interaction = await _press(StopView(_make_runner()))
        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert embed.title == "⏹️ Session stopped"

    @pytest.mark.asyncio
    async def test_a_failing_followup_does_not_fail_the_press(self) -> None:
        """The turn is already stopped; a notice Discord refuses changes nothing."""
        view = StopView(_make_runner())
        interaction = _make_interaction()
        interaction.followup.send.side_effect = discord.HTTPException(MagicMock(), "boom")

        await _press(view, interaction)  # must not raise

        assert view.stop_button.disabled is True

    @pytest.mark.asyncio
    async def test_second_press_is_acknowledged_and_ignored(self) -> None:
        """A second press is acknowledged and nothing more: one interrupt, one notice."""
        runner = _make_runner()
        view = StopView(runner)
        await _press(view)

        second = await _press(view)

        runner.interrupt.assert_awaited_once()
        second.response.defer.assert_awaited_once()
        second.response.edit_message.assert_not_called()
        second.followup.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_press_while_the_first_is_still_stopping_is_only_acknowledged(self) -> None:
        # Every wait is bounded: a regression here must fail, not hang the suite.
        stopping = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def slow_interrupt() -> None:
            nonlocal calls
            calls += 1
            stopping.set()
            await release.wait()

        runner = _make_runner()
        runner.interrupt = slow_interrupt
        view = StopView(runner)
        first = asyncio.create_task(_press(view))
        try:
            await asyncio.wait_for(stopping.wait(), timeout=5)

            second = await asyncio.wait_for(_press(view), timeout=5)
        finally:
            release.set()
            await asyncio.wait_for(first, timeout=5)

        assert calls == 1
        second.response.defer.assert_awaited_once()
        second.followup.send.assert_not_called()


class TestTurnEndedByItself:
    """When the turn ends by itself, the button's message is removed — quietly."""

    @pytest.mark.asyncio
    async def test_disable_deletes_message(self) -> None:
        """disable() deletes the stop-button message entirely."""
        view = StopView(_make_runner())
        msg = _make_message()

        await view.disable(msg)

        assert view.is_finished()
        msg.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_message_is_kept_once_stop_was_pressed(self) -> None:
        """disable() after the stop button was clicked should not delete the message."""
        view = StopView(_make_runner())
        msg = _make_message()
        await _press(view)

        await view.disable(msg)

        msg.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_press_after_disable_does_nothing(self) -> None:
        """The turn is over and the message is going away: there is nothing to stop."""
        runner = _make_runner()
        view = StopView(runner)
        await view.disable(_make_message())

        late = await _press(view)

        runner.interrupt.assert_not_called()
        late.response.defer.assert_awaited_once()
        late.followup.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_twice_deletes_once(self) -> None:
        view = StopView(_make_runner())
        msg = _make_message()

        await view.disable(msg)
        await view.disable(msg)

        msg.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_refused_delete_is_not_an_error(self) -> None:
        """A delete Discord refuses (already gone, rate limited) is not an error."""
        view = StopView(_make_runner())
        msg = _make_message()
        msg.delete = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "rate limited"))

        await view.disable(msg)  # must not raise

        msg.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failed_delete_still_retires_the_button(self) -> None:
        """The view must not look live after a delete that failed: no retry, no stop."""
        runner = _make_runner()
        view = StopView(runner)
        msg = _make_message()
        msg.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(), "Unknown Message"))

        await view.disable(msg)
        await view.disable(msg)
        await _press(view)

        msg.delete.assert_awaited_once()
        runner.interrupt.assert_not_called()
        assert view.is_finished()

    @pytest.mark.asyncio
    async def test_message_from_set_message_is_the_default_target(self) -> None:
        """Without an argument, the message given to ``set_message`` is the one deleted."""
        view = StopView(_make_runner())
        msg = _make_message()
        view.set_message(msg)

        await view.disable()

        msg.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_an_explicit_message_wins_over_the_stored_one(self) -> None:
        view = StopView(_make_runner())
        stored, explicit = _make_message(), _make_message()
        view.set_message(stored)

        await view.disable(explicit)

        explicit.delete.assert_awaited_once()
        stored.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_nothing_to_delete_is_fine(self) -> None:
        """Nothing was ever sent (the post failed): disabling is still safe."""
        view = StopView(_make_runner())

        await view.disable()  # must not raise

        assert view.is_finished()

    @pytest.mark.asyncio
    async def test_disable_suppresses_runtime_error_session_closed(self) -> None:
        """Issue #91: disable() must not raise RuntimeError('Session is closed').

        During bot shutdown the aiohttp session may already be closed when
        the _run_claude finally block calls disable().  Previously this
        RuntimeError propagated and turned the bot into a zombie.
        """
        view = StopView(_make_runner())
        msg = _make_message()
        msg.delete = AsyncMock(side_effect=RuntimeError("Session is closed"))

        await view.disable(msg)  # must not raise

    @pytest.mark.asyncio
    async def test_disable_logs_warning_on_runtime_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """RuntimeError in disable() must produce a WARNING log (not silently swallowed)."""
        import logging

        view = StopView(_make_runner())
        msg = _make_message()
        msg.delete = AsyncMock(side_effect=RuntimeError("Session is closed"))

        with caplog.at_level(logging.WARNING, logger="c_lord.discord_ui.views"):
            await view.disable(msg)

        assert any(r.levelno >= logging.WARNING for r in caplog.records)


class TestStopViewSharedStopPath:
    """#878: the button runs the same stop as /stop and !stop (``on_stop``)."""

    @pytest.mark.asyncio
    async def test_click_runs_on_stop_instead_of_the_runner(self) -> None:
        from c_lord.session_stop import StopOutcome, StopResult

        runner = _make_runner()
        on_stop = AsyncMock(return_value=StopResult(StopOutcome.STOPPED, shells_stopped=1))
        view = StopView(runner, on_stop=on_stop)

        await _press(view)

        on_stop.assert_awaited_once()
        runner.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_stop_is_not_announced_as_stopped(self) -> None:
        from c_lord.session_stop import FAILED_MESSAGE, StopOutcome, StopResult

        on_stop = AsyncMock(return_value=StopResult(StopOutcome.FAILED))
        view = StopView(_make_runner(), on_stop=on_stop)
        interaction = _make_interaction()
        interaction.edit_original_response = AsyncMock()

        await _press(view, interaction)

        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs.get("embed") is None
        assert interaction.followup.send.call_args.args[0] == FAILED_MESSAGE
        # The button comes back so the user can try again.
        assert view.stop_button.disabled is False

    @pytest.mark.asyncio
    async def test_runner_reporting_no_c_c_is_a_failure_without_on_stop(self) -> None:
        runner = _make_runner()
        runner.interrupt = AsyncMock(return_value=False)
        view = StopView(runner)
        interaction = _make_interaction()
        interaction.edit_original_response = AsyncMock()

        await _press(view, interaction)

        assert interaction.followup.send.call_args.kwargs.get("embed") is None
        assert view.stop_button.disabled is False

    @pytest.mark.asyncio
    async def test_on_stop_reports_the_commands_it_ended(self) -> None:
        from c_lord.session_stop import StopOutcome, StopResult

        on_stop = AsyncMock(return_value=StopResult(StopOutcome.STOPPED, shells_stopped=3))

        interaction = await _press(StopView(_make_runner(), on_stop=on_stop))

        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert "3 running command(s)" in embed.description

    @pytest.mark.asyncio
    async def test_nothing_running_is_said_privately_and_the_button_returns(self) -> None:
        from c_lord.session_stop import NOTHING_RUNNING_MESSAGE, StopOutcome, StopResult

        on_stop = AsyncMock(return_value=StopResult(StopOutcome.NOTHING_RUNNING))
        view = StopView(_make_runner(), on_stop=on_stop)

        interaction = await _press(view)

        call = interaction.followup.send.await_args
        assert call.args[0] == NOTHING_RUNNING_MESSAGE
        assert call.kwargs["ephemeral"] is True
        assert view.stop_button.disabled is False
        interaction.edit_original_response.assert_awaited_once_with(view=view)

    @pytest.mark.asyncio
    async def test_a_failed_stop_can_be_retried(self) -> None:
        """The button came back for a reason: the second press runs the stop again."""
        from c_lord.session_stop import StopOutcome, StopResult

        on_stop = AsyncMock(
            side_effect=[StopResult(StopOutcome.FAILED), StopResult(StopOutcome.STOPPED)]
        )
        view = StopView(_make_runner(), on_stop=on_stop)

        await _press(view)
        second = await _press(view)

        assert on_stop.await_count == 2
        assert second.followup.send.await_args.kwargs["embed"].title == "⏹️ Session stopped"
        assert view.stop_button.disabled is True

    @pytest.mark.asyncio
    async def test_a_failed_stop_keeps_the_view_alive_for_disable(self) -> None:
        """The turn may still end by itself; the message must then still be removable."""
        from c_lord.session_stop import StopOutcome, StopResult

        view = StopView(
            _make_runner(), on_stop=AsyncMock(return_value=StopResult(StopOutcome.FAILED))
        )
        msg = _make_message()
        await _press(view)

        await view.disable(msg)

        msg.delete.assert_awaited_once()
