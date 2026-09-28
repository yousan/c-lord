"""#674: a chosen option is identified by its INDEX, never by its (truncated) label.

Reproduced in the 2026-09-09 inventory: a Select option's ``value`` was the
label cut to Discord's 80-character display limit, and ``send_answer_keystrokes``
matched what came back against the **untruncated** labels. Nothing matched, so
the choice fell through to the free-text path — Claude received the first 80
characters as a sentence the user had typed:

    label len : 120
    value len : 80
    equal?    : False
    indices   : [] -> 自由入力パスに落ちる

"Chose it and got a different answer" is the worst way a question menu can fail
(docs/specs/ask-menu-lifecycle.md): the send succeeds, and what arrives is not
what was picked (#651's "sent ≠ received").

These tests drive the whole route a click takes — the rendered component's
value, the View, the ask bus, and ``send_answer_keystrokes`` — and assert the
runner is told the option's index. They read the value off the rendered
component rather than spelling it out, so they pin the behaviour (the right
option is chosen), not a value format.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.components import ActionRow

from c_lord.claude.types import AskOption, AskQuestion
from c_lord.discord_ui.ask_bus import AskAnswerBus
from c_lord.discord_ui.ask_handler import send_answer_keystrokes
from c_lord.discord_ui.ask_view import AskView

# 120 characters — the exact length the inventory reproduced with. The two long
# labels share their first 80 characters, which is what a truncated value could
# never tell apart.
_PREFIX = (
    "古い Open PR 3本の始末: #612 と #640 はクローズ、#655 は main に追従してから"
    "再レビュー依頼、CI が緑になったらマージ判断を仰ぐ、"
)
_LONG_A = (_PREFIX + "ついでに残骸ブランチも消す" + "。" * 120)[:120]
_LONG_B = (_PREFIX + "ブランチは残して後で判断する" + "。" * 120)[:120]
assert len(_PREFIX) > 80 and len(_LONG_A) == len(_LONG_B) == 120


def _single_select_question() -> AskQuestion:
    """Five options → rendered as a Select, not buttons."""
    return AskQuestion(
        question="どれから片付けますか？",
        header="整理",
        options=[
            AskOption("残骸ウィンドウ9枚の掃除"),
            AskOption("放置スレッドの棚卸し"),
            AskOption(_LONG_A),
            AskOption(_LONG_B),
            AskOption("いまはやらない"),
        ],
    )


def _multi_select_question() -> AskQuestion:
    return AskQuestion(
        question="あわせて片付けますか？（複数選択可）",
        header="整理",
        options=[
            AskOption("残骸ウィンドウ9枚の掃除"),
            AskOption(_LONG_A),
            AskOption("放置スレッドの棚卸し"),
            AskOption(_LONG_B),
        ],
        multi_select=True,
    )


def _select_of(view: AskView) -> discord.ui.Select:
    return next(c for c in view.children if isinstance(c, discord.ui.Select))


def _value_of(view: AskView, index: int) -> str:
    """The value Discord echoes back when the option at *index* is picked."""
    return _select_of(view).options[index].value


def _interaction(message: Any = None, values: list[str] | None = None) -> MagicMock:
    interaction = MagicMock()
    interaction.message = message if message is not None else MagicMock(components=[])
    interaction.data = {"values": values or []}
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _message_with_defaults(select: discord.ui.Select, chosen: list[int]) -> MagicMock:
    """A menu message whose components carry the options at *chosen* as defaults.

    Built from the View's OWN rendered options, through ``ActionRow``, so it is
    the payload Discord actually echoes back for a menu this code posted.
    """
    payload: Any = {
        "type": 1,
        "components": [
            {
                "type": 3,
                "custom_id": select.custom_id,
                "min_values": 1,
                "max_values": len(select.options),
                "options": [
                    {
                        "label": o.label,
                        "value": o.value,
                        **({"default": True} if i in chosen else {}),
                    }
                    for i, o in enumerate(select.options)
                ],
            }
        ],
    }
    message = MagicMock()
    message.components = [ActionRow(payload)]
    return message


def _legacy_message(custom_id: str, values: list[str], chosen: list[str]) -> MagicMock:
    """A menu posted BEFORE #674: every option's value was its display label."""
    payload: Any = {
        "type": 1,
        "components": [
            {
                "type": 3,
                "custom_id": custom_id,
                "min_values": 1,
                "max_values": len(values),
                "options": [
                    {"label": v, "value": v, **({"default": True} if v in chosen else {})}
                    for v in values
                ],
            }
        ],
    }
    message = MagicMock()
    message.components = [ActionRow(payload)]
    return message


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.answer_menu = AsyncMock(return_value=True)
    runner.answer_menu_multi = AsyncMock(return_value=True)
    runner.answer_menu_text = AsyncMock(return_value=True)
    return runner


async def _answer_of(bus: AskAnswerBus, thread_id: int) -> list[str]:
    queue = cast("asyncio.Queue[list[str]]", bus._waiters[thread_id])
    return await asyncio.wait_for(queue.get(), timeout=1.0)


class TestLongLabelSingleSelect:
    """AC1 — a Select pick of an 80+ character option reaches answer_menu(index)."""

    @pytest.mark.asyncio
    async def test_long_label_is_answered_by_its_index(self) -> None:
        q = _single_select_question()
        bus = AskAnswerBus()
        tid = 674_0001
        bus.register(tid, allow_free_text=True)
        view = AskView(q, thread_id=tid, q_idx=0, bus=bus)

        await view._select_callback(_interaction(values=[_value_of(view, 3)]))
        answer = await _answer_of(bus, tid)

        runner = _runner()
        await send_answer_keystrokes(runner, q, answer)

        runner.answer_menu_text.assert_not_called()
        runner.answer_menu.assert_awaited_once_with(3)

    @pytest.mark.asyncio
    async def test_the_answer_reads_as_the_whole_label_not_the_cut_one(self) -> None:
        """What the thread shows (and the non-tmux path gives Claude) is the
        option the user picked — all of it, not its first 80 characters."""
        q = _single_select_question()
        bus = AskAnswerBus()
        tid = 674_0002
        bus.register(tid, allow_free_text=True)
        view = AskView(q, thread_id=tid, q_idx=0, bus=bus)

        await view._select_callback(_interaction(values=[_value_of(view, 2)]))

        assert await _answer_of(bus, tid) == [_LONG_A]

    @pytest.mark.asyncio
    async def test_options_sharing_their_first_80_characters_get_distinct_values(self) -> None:
        """Discord rejects a Select whose values repeat — the WHOLE message, so
        the menu would never appear. A truncated label cannot tell these apart."""
        view = AskView(_single_select_question(), thread_id=674_0003, q_idx=0, bus=AskAnswerBus())
        values = [o.value for o in _select_of(view).options]
        assert len(set(values)) == len(values), values
        assert all(len(v) <= 100 for v in values)


class TestLongLabelMultiSelect:
    """AC2 — the same for multiSelect: answer_menu_multi gets the right indices."""

    @pytest.mark.asyncio
    async def test_long_labels_are_toggled_by_index(self) -> None:
        q = _multi_select_question()
        bus = AskAnswerBus()
        tid = 674_0010
        bus.register(tid, allow_free_text=True)
        view = AskView(q, thread_id=tid, q_idx=0, bus=bus)

        await view._multi_select_record(
            _interaction(values=[_value_of(view, 1), _value_of(view, 3)])
        )
        await view._confirm_callback(_interaction())
        answer = await _answer_of(bus, tid)

        runner = _runner()
        await send_answer_keystrokes(runner, q, answer)

        runner.answer_menu_text.assert_not_called()
        runner.answer_menu_multi.assert_awaited_once_with([1, 3], 4)

    @pytest.mark.asyncio
    async def test_pending_line_shows_what_was_picked(self) -> None:
        """「🔲 選択中」 names the options, not whatever the value happens to be."""
        view = AskView(_multi_select_question(), thread_id=674_0011, q_idx=0, bus=AskAnswerBus())
        interaction = _interaction(values=[_value_of(view, 0), _value_of(view, 2)])

        await view._multi_select_record(interaction)

        content = interaction.response.edit_message.await_args.kwargs["content"]
        assert "残骸ウィンドウ9枚の掃除" in content
        assert "放置スレッドの棚卸し" in content


class TestRecoveredSelection:
    """AC3 — the #672 route (selection read back off the message) uses the same key."""

    @pytest.mark.asyncio
    async def test_fresh_view_confirms_long_labels_from_the_message(self) -> None:
        q = _multi_select_question()
        bus = AskAnswerBus()
        tid = 674_0020
        bus.register(tid, allow_free_text=True)
        # A different View instance from the one that recorded the choice — the
        # restart-restored view of #671, or a watchdog re-bridge of #633.
        view = AskView(q, thread_id=tid, q_idx=0, bus=bus)
        message = _message_with_defaults(_select_of(view), chosen=[1, 3])

        await view._confirm_callback(_interaction(message))
        answer = await _answer_of(bus, tid)

        runner = _runner()
        await send_answer_keystrokes(runner, q, answer)

        runner.answer_menu_text.assert_not_called()
        runner.answer_menu_multi.assert_awaited_once_with([1, 3], 4)

    @pytest.mark.asyncio
    async def test_recorded_choice_is_written_back_under_the_same_key(self) -> None:
        """The marks _multi_select_record writes are what _recover_selection
        reads — change one without the other and recovery breaks (#674 注意)."""
        q = _multi_select_question()
        recorder = AskView(q, thread_id=674_0021, q_idx=0, bus=AskAnswerBus())
        await recorder._multi_select_record(
            _interaction(values=[_value_of(recorder, 1), _value_of(recorder, 3)])
        )
        marked = [i for i, o in enumerate(_select_of(recorder).options) if o.default]
        assert marked == [1, 3]

        bus = AskAnswerBus()
        tid = 674_0022
        bus.register(tid, allow_free_text=True)
        fresh = AskView(q, thread_id=tid, q_idx=0, bus=bus)
        message = _message_with_defaults(_select_of(fresh), marked)
        await fresh._confirm_callback(_interaction(message))

        assert await _answer_of(bus, tid) == [_LONG_A, _LONG_B]


class TestMenusPostedBeforeTheFix:
    """A menu already on screen at deploy time carries the OLD values — the
    display label itself. The deploy restarts the bot and #671 re-arms that
    message, so a press on it must still choose the right option.

    Such a menu never has two options sharing their first 80 characters: their
    values collided and Discord rejected the whole message. So these questions
    carry one long label, not the ``_LONG_A`` / ``_LONG_B`` pair.
    """

    @staticmethod
    def _question(*, multi_select: bool) -> AskQuestion:
        return AskQuestion(
            question="どれから片付けますか？",
            header="整理",
            options=[
                AskOption("残骸ウィンドウ9枚の掃除"),
                AskOption("放置スレッドの棚卸し"),
                AskOption("いまはやらない"),
                AskOption(_LONG_B),
                AskOption("別の日にする"),
            ],
            multi_select=multi_select,
        )

    @pytest.mark.asyncio
    async def test_old_single_select_value_still_resolves(self) -> None:
        q = self._question(multi_select=False)
        bus = AskAnswerBus()
        tid = 674_0030
        bus.register(tid, allow_free_text=True)
        view = AskView(q, thread_id=tid, q_idx=0, bus=bus)

        await view._select_callback(_interaction(values=[_LONG_B[:80]]))
        answer = await _answer_of(bus, tid)

        runner = _runner()
        await send_answer_keystrokes(runner, q, answer)
        runner.answer_menu_text.assert_not_called()
        runner.answer_menu.assert_awaited_once_with(3)

    @pytest.mark.asyncio
    async def test_old_message_defaults_still_recover(self) -> None:
        q = self._question(multi_select=True)
        bus = AskAnswerBus()
        tid = 674_0031
        bus.register(tid, allow_free_text=True)
        view = AskView(q, thread_id=tid, q_idx=0, bus=bus)
        old_values = [o.label[:80] for o in q.options]
        message = _legacy_message(
            _select_of(view).custom_id or "", old_values, [old_values[1], old_values[3]]
        )

        await view._confirm_callback(_interaction(message))
        answer = await _answer_of(bus, tid)

        runner = _runner()
        await send_answer_keystrokes(runner, q, answer)
        runner.answer_menu_text.assert_not_called()
        runner.answer_menu_multi.assert_awaited_once_with([1, 3], 5)


class TestUnreadableValue:
    @pytest.mark.asyncio
    async def test_a_value_naming_no_option_is_not_delivered(self) -> None:
        """An empty answer is the #315 pre-emption signal — it Esc's the menu
        away. A pick that names no option must never turn into one."""
        q = _single_select_question()
        bus = AskAnswerBus()
        tid = 674_0040
        queue = cast("asyncio.Queue[list[str]]", bus.register(tid, allow_free_text=True))
        view = AskView(q, thread_id=tid, q_idx=0, bus=bus)
        interaction = _interaction(values=["この質問には無い選択肢"])

        await view._select_callback(interaction)

        assert queue.empty()
        interaction.response.send_message.assert_awaited()


class TestButtonsAreIdentifiedByIndexToo:
    @pytest.mark.asyncio
    async def test_second_of_two_unreadable_labels_answers_the_second(self) -> None:
        """#579 placeholders: two options the pane parser could not read both
        have the label ``""``. Matching by label always chose the first."""
        q = AskQuestion(
            question="どちら?",
            options=[AskOption(""), AskOption(""), AskOption("やめる")],
        )
        bus = AskAnswerBus()
        tid = 674_0050
        bus.register(tid, allow_free_text=True)
        view = AskView(q, thread_id=tid, q_idx=0, bus=bus)
        button = next(c for c in view.children if getattr(c, "label", "") == "2.")

        await button.callback(_interaction())
        answer = await _answer_of(bus, tid)

        runner = _runner()
        await send_answer_keystrokes(runner, q, answer)
        runner.answer_menu.assert_awaited_once_with(1)


class TestFreeTextStaysFreeText:
    @pytest.mark.asyncio
    async def test_typed_text_is_typed(self) -> None:
        runner = _runner()
        await send_answer_keystrokes(runner, _single_select_question(), ["別の案で"])
        runner.answer_menu.assert_not_called()
        runner.answer_menu_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_typed_text_that_spells_a_digit_is_not_an_index(self) -> None:
        """A sentence of "3" typed in ✏️ Other is text, not option 3."""
        runner = _runner()
        await send_answer_keystrokes(runner, _single_select_question(), ["3"])
        runner.answer_menu.assert_not_called()
        runner.answer_menu_text.assert_awaited_once()


class TestRestartRecoveryPath:
    """#671's PaneMenuAnswerer receives the same answer the View builds."""

    @pytest.mark.asyncio
    async def test_restored_menu_answers_a_long_label_by_index(self) -> None:
        from c_lord.ask_menu_recovery import PaneMenuAnswerer

        q = _single_select_question()
        runner = _runner()
        runner.peek_pending_ask = AsyncMock(return_value=q)
        # After the keys: no transcript to read, and the pane says the menu closed.
        runner.transcript_project_dir = MagicMock(return_value=None)
        runner.peek_menu_state = AsyncMock(return_value=(None, True))

        async def factory(thread_id: int) -> MagicMock:
            assert thread_id == tid
            return runner

        tid = 674_0060
        answerer = PaneMenuAnswerer(thread_id=tid, question=q, runner_factory=factory)
        # No waiter on this bus — the state every re-armed menu is in after a restart.
        view = AskView(q, thread_id=tid, q_idx=0, bus=AskAnswerBus(), recovery=answerer)

        await view._select_callback(_interaction(values=[_value_of(view, 2)]))

        runner.answer_menu_text.assert_not_called()
        runner.answer_menu.assert_awaited_once_with(2)
