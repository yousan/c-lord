"""#672: the multiSelect ✅ 確定 button must actually deliver the choice.

Production incident (2026-09-01, W140): the user picked options and pressed
✅ 確定, and **nothing happened** — no keystrokes reached tmux, and the bot log
carried not one line about it.  ``answer_menu_multi`` logs unconditionally on
entry, and that line was absent for the whole 8 minutes the menu stayed open.

Root cause: the choice lived **only** in ``AskView._selected_values``, an
instance attribute of the one View object in the one process that happened to
render the menu.  Three routes empty it:

1. the dropdown's own interaction never fired (a mobile sheet dismissed without
   submitting) — nothing was ever recorded;
2. the confirm click is processed before the select's record;
3. a *different* View instance receives the confirm — the restart-restored view
   (#671) or a watchdog re-bridge (#633) always starts with an empty list.

All three look identical to the user: press ✅ 確定, get silence.

The fix stores the choice where every route can see it — in the message's own
components, as ``default=True`` on the chosen options — and reads it back when
the in-memory copy is empty.  These tests pin both halves plus the logging that
makes a future failure visible (#585).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.components import ActionRow

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.discord_ui.ask_bus import ask_bus
from c_lord.discord_ui.ask_view import AskView


def _question() -> AskQuestion:
    return AskQuestion(
        question="あわせて片付けますか？（複数選択可）",
        header="整理",
        options=[
            AskOption(label="残骸ウィンドウ9枚の掃除"),
            AskOption(label="古い Open PR 3本の始末"),
            AskOption(label="放置スレッドの棚卸し"),
            AskOption(label="いまはやらない"),
        ],
        multi_select=True,
    )


def _select_of(view: AskView) -> discord.ui.Select:
    """The View's own Select child."""
    return next(
        c
        for c in view.children
        if isinstance(c, discord.ui.Select) and (c.custom_id or "").endswith("_select")
    )


def _message_with_defaults(custom_id: str, labels: list[str], chosen: list[str]) -> MagicMock:
    """A ``discord.Message`` stand-in whose components carry *chosen* as defaults.

    Built from a real Discord components payload through ``ActionRow`` so the
    test exercises the same object graph the gateway produces, not a mock shape
    invented here.
    """
    payload: Any = {
        "type": 1,
        "components": [
            {
                "type": 3,
                "custom_id": custom_id,
                "min_values": 1,
                "max_values": len(labels),
                "options": [
                    {"label": lb, "value": lb, **({"default": True} if lb in chosen else {})}
                    for lb in labels
                ],
            }
        ],
    }
    row = ActionRow(payload)
    message = MagicMock()
    message.components = [row]
    return message


def _interaction(message: MagicMock | None = None) -> MagicMock:
    interaction = MagicMock()
    interaction.message = message if message is not None else MagicMock(components=[])
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    return interaction


class TestSelectionIsStoredOnTheMessage:
    """AC1 — recording a choice writes it back to the message, not just memory."""

    @pytest.mark.asyncio
    async def test_record_marks_chosen_options_as_default(self) -> None:
        view = AskView(_question(), thread_id=672_0001, q_idx=0)
        select = _select_of(view)

        sel = _interaction()
        sel.data = {"values": ["残骸ウィンドウ9枚の掃除", "放置スレッドの棚卸し"]}
        await view._multi_select_record(sel)

        marked = [o.value for o in select.options if o.default]
        assert marked == ["残骸ウィンドウ9枚の掃除", "放置スレッドの棚卸し"], (
            "the chosen options must be marked default=True so the selection "
            "lives on the message, not only in this View instance"
        )

    @pytest.mark.asyncio
    async def test_record_sends_the_view_so_discord_stores_the_defaults(self) -> None:
        view = AskView(_question(), thread_id=672_0002, q_idx=0)

        sel = _interaction()
        sel.data = {"values": ["いまはやらない"]}
        await view._multi_select_record(sel)

        await_args = sel.response.edit_message.await_args
        assert await_args is not None
        kwargs = await_args.kwargs
        assert kwargs.get("view") is view, (
            "edit_message must carry the view, otherwise the default=True marks "
            "never reach Discord and the selection stays process-local"
        )

    @pytest.mark.asyncio
    async def test_reselecting_clears_the_previous_marks(self) -> None:
        view = AskView(_question(), thread_id=672_0003, q_idx=0)
        select = _select_of(view)

        first = _interaction()
        first.data = {"values": ["残骸ウィンドウ9枚の掃除"]}
        await view._multi_select_record(first)

        second = _interaction()
        second.data = {"values": ["いまはやらない"]}
        await view._multi_select_record(second)

        assert [o.value for o in select.options if o.default] == ["いまはやらない"]


class TestConfirmRecoversTheSelection:
    """AC2 — ✅ 確定 delivers even when this View never saw the dropdown."""

    @pytest.mark.asyncio
    async def test_fresh_view_confirms_using_the_message_components(self) -> None:
        """The exact shape of #671 / #633: the View that receives the confirm is
        not the one that recorded the choice, so ``_selected_values`` is empty."""
        q = _question()
        tid = 672_0010
        recorded = ["残骸ウィンドウ9枚の掃除", "古い Open PR 3本の始末", "放置スレッドの棚卸し"]

        # A different instance entirely — a restored view, or a re-bridge.
        view = AskView(q, thread_id=tid, q_idx=0)
        assert view._selected_values == []

        message = _message_with_defaults(
            _select_of(view).custom_id, [o.label for o in q.options], recorded
        )
        queue = cast("asyncio.Queue[list[str]]", ask_bus.register(tid))
        try:
            await view._confirm_callback(_interaction(message))
            delivered = await asyncio.wait_for(queue.get(), timeout=1.0)
        finally:
            ask_bus.unregister(tid)

        assert delivered == recorded, (
            "the confirm must fall back to the selection recorded on the message"
        )

    @pytest.mark.asyncio
    async def test_in_memory_selection_still_wins_when_present(self) -> None:
        q = _question()
        tid = 672_0011
        view = AskView(q, thread_id=tid, q_idx=0)

        sel = _interaction()
        sel.data = {"values": ["いまはやらない"]}
        await view._multi_select_record(sel)

        queue = cast("asyncio.Queue[list[str]]", ask_bus.register(tid))
        try:
            await view._confirm_callback(_interaction())
            delivered = await asyncio.wait_for(queue.get(), timeout=1.0)
        finally:
            ask_bus.unregister(tid)

        assert delivered == ["いまはやらない"]

    @pytest.mark.asyncio
    async def test_nothing_selected_anywhere_warns_and_delivers_nothing(self) -> None:
        """AC4 — the ephemeral guard stays, but only for a genuinely empty menu."""
        q = _question()
        tid = 672_0012
        view = AskView(q, thread_id=tid, q_idx=0)
        message = _message_with_defaults(
            _select_of(view).custom_id, [o.label for o in q.options], []
        )

        queue = cast("asyncio.Queue[list[str]]", ask_bus.register(tid))
        try:
            interaction = _interaction(message)
            await view._confirm_callback(interaction)
            interaction.response.send_message.assert_awaited()
            assert queue.empty()
        finally:
            ask_bus.unregister(tid)


class TestConfirmIsNoLongerSilent:
    """AC3/AC4 — every outcome of a confirm press leaves a line in the log.

    The incident was invisible precisely because this path logged nothing: the
    only way to tell the answer never left Discord was the *absence* of a log
    line further downstream (#585).
    """

    @pytest.mark.asyncio
    async def test_delivered_confirm_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        tid = 672_0020
        view = AskView(_question(), thread_id=tid, q_idx=0)
        sel = _interaction()
        sel.data = {"values": ["放置スレッドの棚卸し"]}
        await view._multi_select_record(sel)

        queue = cast("asyncio.Queue[list[str]]", ask_bus.register(tid))
        try:
            with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.ask_view"):
                await view._confirm_callback(_interaction())
            await asyncio.wait_for(queue.get(), timeout=1.0)
        finally:
            ask_bus.unregister(tid)

        assert any(str(tid) in r.getMessage() for r in caplog.records), (
            "a confirm press must be traceable by thread id in the log"
        )

    @pytest.mark.asyncio
    async def test_empty_confirm_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        tid = 672_0021
        view = AskView(_question(), thread_id=tid, q_idx=0)

        queue = cast("asyncio.Queue[list[str]]", ask_bus.register(tid))
        try:
            with caplog.at_level(logging.INFO, logger="c_lord.discord_ui.ask_view"):
                await view._confirm_callback(_interaction())
        finally:
            ask_bus.unregister(tid)
            assert queue.empty()

        assert any(str(tid) in r.getMessage() for r in caplog.records), (
            "a confirm that delivered nothing is exactly what must not be silent"
        )


class TestRecoveryIsBoundedByTheOptionSet:
    """The recovered answer becomes keystrokes in a live pane, so it may only
    ever contain options this question actually offers."""

    @pytest.mark.asyncio
    async def test_marks_for_options_this_question_does_not_offer_are_ignored(self) -> None:
        q = _question()
        tid = 672_0030
        view = AskView(q, thread_id=tid, q_idx=0)
        message = _message_with_defaults(
            _select_of(view).custom_id,
            [o.label for o in q.options] + ["よそから来た選択肢"],
            ["放置スレッドの棚卸し", "よそから来た選択肢"],
        )

        queue = cast("asyncio.Queue[list[str]]", ask_bus.register(tid))
        try:
            await view._confirm_callback(_interaction(message))
            delivered = await asyncio.wait_for(queue.get(), timeout=1.0)
        finally:
            ask_bus.unregister(tid)

        assert delivered == ["放置スレッドの棚卸し"]
