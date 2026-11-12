"""Tests for the AskUserQuestion feature.

What a question looks like on its way from Claude's tool call to Discord:
- types: AskOption, AskQuestion, ToolCategory.ASK, _parse_ask_questions
- embeds: ask_embed
- view: AskView — which components a question gets, and what a press delivers
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from c_lord.claude.types import (
    FREE_TEXT_NOTES,
    FREE_TEXT_ROW,
    AskOption,
    AskQuestion,
    ToolCategory,
    _parse_ask_questions,
)
from c_lord.discord_ui.embeds import COLOR_ASK, ask_embed

# ---------------------------------------------------------------------------
# The data types
# ---------------------------------------------------------------------------


class TestQuestionDataShapes:
    """The shapes the rest of the feature passes around, and their defaults."""

    def test_an_option_needs_only_a_label(self) -> None:
        """A label alone is a complete option; the description is optional."""
        assert AskOption(label="Yes").description == ""

    def test_a_bare_question_is_single_select_with_free_text(self) -> None:
        """A bare question is single-select, has no options and offers ✏️ Other."""
        q = AskQuestion(question="Proceed?")
        assert q.header == ""
        assert q.multi_select is False
        assert q.options == []
        assert q.allow_other is True
        assert q.free_text_mode == FREE_TEXT_ROW
        assert q.context == ""

    def test_questions_do_not_share_one_options_list(self) -> None:
        first, second = AskQuestion(question="a"), AskQuestion(question="b")
        first.options.append(AskOption(label="only mine"))
        assert second.options == []

    def test_asking_is_its_own_tool_category(self) -> None:
        assert ToolCategory.ASK.value == "ask"

    def test_only_ask_user_question_is_filed_under_ask(self) -> None:
        """AskUserQuestion is filed under ASK — and is the only tool that is."""
        from c_lord.claude.types import TOOL_CATEGORIES

        assert TOOL_CATEGORIES["AskUserQuestion"] is ToolCategory.ASK
        asking = [name for name, kind in TOOL_CATEGORIES.items() if kind is ToolCategory.ASK]
        assert asking == ["AskUserQuestion"]


# ---------------------------------------------------------------------------
# _parse_ask_questions
# ---------------------------------------------------------------------------


class TestParseAskQuestions:
    """Claude's tool input → the questions c-lord shows."""

    def test_input_without_questions_parses_to_nothing(self) -> None:
        assert _parse_ask_questions({}) == []
        assert _parse_ask_questions({"questions": []}) == []

    def test_a_question_without_options_is_still_a_question(self) -> None:
        """A question with no options is still a question (free text only)."""
        questions = _parse_ask_questions({"questions": [{"question": "Why?", "options": []}]})
        assert len(questions) == 1
        assert questions[0].question == "Why?"
        assert questions[0].options == []

    def test_missing_fields_fall_back_to_defaults(self) -> None:
        (q,) = _parse_ask_questions({"questions": [{}]})
        assert (q.question, q.header, q.multi_select, q.options) == ("", "", False, [])

    def test_an_option_with_no_label_is_not_offered(self) -> None:
        """An option that cannot be shown is not offered: no label, or an empty one."""
        tool_input = {
            "questions": [
                {
                    "question": "Pick",
                    "options": [
                        {"description": "no label at all"},
                        {"label": "", "description": "empty label"},
                        {"label": "Keep me"},
                    ],
                }
            ]
        }
        (q,) = _parse_ask_questions(tool_input)
        assert [o.label for o in q.options] == ["Keep me"]
        assert q.options[0].description == ""

    def test_every_question_comes_back_in_the_order_asked(self) -> None:
        """Every question of the call comes back, in the order Claude asked them."""
        tool_input = {
            "questions": [
                {"question": "First?", "header": "One", "options": [{"label": "a"}]},
                {"question": "Second?", "header": "Two", "options": [{"label": "b"}]},
                {"question": "Third?", "options": []},
            ]
        }
        questions = _parse_ask_questions(tool_input)
        assert [q.question for q in questions] == ["First?", "Second?", "Third?"]
        assert [q.header for q in questions] == ["One", "Two", ""]
        assert [[o.label for o in q.options] for q in questions] == [["a"], ["b"], []]

    def test_full_parse(self) -> None:
        tool_input = {
            "questions": [
                {
                    "question": "Which auth method should we use?",
                    "header": "Auth method",
                    "multiSelect": False,
                    "options": [
                        {"label": "JWT tokens", "description": "Stateless, good for APIs"},
                        {"label": "Session cookies", "description": "Simple, stateful"},
                        {"label": "OAuth2", "description": "Federated identity"},
                    ],
                }
            ]
        }
        questions = _parse_ask_questions(tool_input)
        assert len(questions) == 1
        q = questions[0]
        assert q.question == "Which auth method should we use?"
        assert q.header == "Auth method"
        assert q.multi_select is False
        assert len(q.options) == 3
        assert q.options[0].label == "JWT tokens"
        assert q.options[0].description == "Stateless, good for APIs"
        assert q.options[2].label == "OAuth2"

    def test_multiselect_flag_is_carried_over(self) -> None:
        tool_input = {
            "questions": [
                {
                    "question": "Which features do you want?",
                    "header": "Features",
                    "multiSelect": True,
                    "options": [{"label": "Auth"}, {"label": "Logging"}],
                }
            ]
        }
        questions = _parse_ask_questions(tool_input)
        assert questions[0].multi_select is True

    def test_preview_options_mean_the_notes_layout(self) -> None:
        """#650: ``preview`` is what makes Claude Code draw the other TUI menu.

        The menu the mirror bridges is never read off the pane, so the tool
        input is the only place the layout can be known here — and getting it
        wrong loses the user's typed answer (the answer keystrokes land on
        "Chat about this", which answers "(No answer provided)").
        """
        tool_input = {
            "questions": [
                {
                    "question": "どの配色にしますか？",
                    "header": "配色案",
                    "options": [
                        {"label": "案A ダーク", "preview": "#121212 …"},
                        {"label": "案B ライト", "preview": "#ffffff …"},
                    ],
                }
            ]
        }
        assert _parse_ask_questions(tool_input)[0].free_text_mode == FREE_TEXT_NOTES

    def test_one_preview_is_enough_to_switch_the_layout(self) -> None:
        """The CLI switches on ``options.some(o => o.preview !== undefined)``."""
        tool_input = {
            "questions": [
                {
                    "question": "Choose?",
                    "options": [{"label": "A"}, {"label": "B", "preview": "…"}],
                }
            ]
        }
        assert _parse_ask_questions(tool_input)[0].free_text_mode == FREE_TEXT_NOTES

    def test_no_preview_keeps_the_classic_row(self) -> None:
        tool_input = {
            "questions": [{"question": "Choose?", "options": [{"label": "A"}, {"label": "B"}]}]
        }
        assert _parse_ask_questions(tool_input)[0].free_text_mode == FREE_TEXT_ROW

    def test_multi_select_keeps_the_row_even_with_previews(self) -> None:
        """A multiSelect menu never gets the preview layout, previews or not."""
        tool_input = {
            "questions": [
                {
                    "question": "Which?",
                    "multiSelect": True,
                    "options": [{"label": "A", "preview": "…"}, {"label": "B", "preview": "…"}],
                }
            ]
        }
        assert _parse_ask_questions(tool_input)[0].free_text_mode == FREE_TEXT_ROW


# ---------------------------------------------------------------------------
# ask_embed
# ---------------------------------------------------------------------------


class TestQuestionEmbed:
    """The embed above the buttons: the header as title, the question as body."""

    def test_title_without_a_header_is_the_generic_prompt(self) -> None:
        assert ask_embed("What now?").title == "❓ Claude needs your input"

    def test_header_becomes_the_title(self) -> None:
        assert ask_embed("What now?", header="Next step").title == "❓ Next step"

    def test_body_is_the_question(self) -> None:
        assert ask_embed("Which database?", header="DB").description == "Which database?"

    def test_body_over_4096_characters_is_cut(self) -> None:
        """Discord refuses a longer description — and the whole message with it."""
        embed = ask_embed("あ" * 4095 + "いう")
        assert embed.description == "あ" * 4095 + "い"

    def test_question_at_the_limit_is_kept_whole(self) -> None:
        question = "q" * 4096
        assert ask_embed(question).description == question

    def test_long_header_truncated_to_256(self) -> None:
        embed = ask_embed("q", header="h" * 400)
        assert embed.title == "❓ " + "h" * 254

    def test_stripe_is_the_ask_blue(self) -> None:
        assert ask_embed("q").color.value == COLOR_ASK == 0x3498DB

    def test_four_options_still_get_the_legend_five_do_not(self) -> None:
        """The legend follows the button/dropdown boundary: buttons hide descriptions."""

        def options(count: int) -> list[AskOption]:
            return [AskOption(label=f"L{i}", description=f"about {i}") for i in range(count)]

        assert "**L3** — about 3" in ask_embed("q", "", options(4)).description
        assert ask_embed("q", "", options(5)).description == "q"

    def test_legend_lists_only_options_that_have_a_description(self) -> None:
        options = [AskOption(label="Plain"), AskOption(label="Told", description="explained")]
        assert ask_embed("q", "", options).description == "q\n\n**Told** — explained"


# ---------------------------------------------------------------------------
# AskView — which components a question gets
# ---------------------------------------------------------------------------


def _question(count: int, **kwargs) -> AskQuestion:
    """A question with *count* options labelled ``opt0``, ``opt1``, …"""
    options = [AskOption(label=f"opt{i}", description=f"desc{i}") for i in range(count)]
    return AskQuestion(question="Pick", options=options, **kwargs)


def _view(question: AskQuestion, *, thread_id: int = 4242, q_idx: int = 0, **kwargs):
    from c_lord.discord_ui.ask_view import AskView

    return AskView(question, thread_id=thread_id, q_idx=q_idx, **kwargs)


def _buttons(view: discord.ui.View) -> list[discord.ui.Button]:
    return [c for c in view.children if isinstance(c, discord.ui.Button)]


def _selects(view: discord.ui.View) -> list[discord.ui.Select]:
    return [c for c in view.children if isinstance(c, discord.ui.Select)]


def _option_buttons(view: discord.ui.View) -> list[discord.ui.Button]:
    return [b for b in _buttons(view) if b.custom_id.rsplit("_", 1)[1].isdecimal()]


class TestAskViewComponents:
    """Few options are buttons, many are a dropdown; the ids survive a restart."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [1, 2, 4])
    async def test_up_to_four_options_are_one_button_each(self, count: int) -> None:
        view = _view(_question(count))
        assert [b.label for b in _option_buttons(view)] == [f"opt{i}" for i in range(count)]
        assert _selects(view) == []

    @pytest.mark.asyncio
    async def test_five_options_become_a_dropdown(self) -> None:
        view = _view(_question(5))
        (select,) = _selects(view)
        assert [o.label for o in select.options] == [f"opt{i}" for i in range(5)]
        assert _option_buttons(view) == []

    @pytest.mark.asyncio
    async def test_multi_select_is_a_dropdown_even_with_two_options(self) -> None:
        view = _view(_question(2, multi_select=True))
        (select,) = _selects(view)
        assert (select.min_values, select.max_values) == (1, 2)
        assert _option_buttons(view) == []

    @pytest.mark.asyncio
    async def test_single_select_dropdown_takes_exactly_one_pick(self) -> None:
        (select,) = _selects(_view(_question(6)))
        assert (select.min_values, select.max_values) == (1, 1)

    @pytest.mark.asyncio
    async def test_component_ids_name_the_thread_and_the_question(self) -> None:
        """Stable ids are what lets ``bot.add_view()`` re-attach after a restart."""
        buttons = _buttons(_view(_question(2), thread_id=77, q_idx=3))
        assert [b.custom_id for b in buttons] == ["ask_77_3_0", "ask_77_3_1", "ask_77_3_other"]
        multi = _view(_question(2, multi_select=True), thread_id=77, q_idx=3)
        assert [c.custom_id for c in multi.children] == [
            "ask_77_3_select",
            "ask_77_3_confirm",
            "ask_77_3_other",
        ]

    @pytest.mark.asyncio
    async def test_the_view_never_times_out(self) -> None:
        """A question may wait for hours; a timed-out button would strand it."""
        assert _view(_question(2)).timeout is None

    @pytest.mark.asyncio
    async def test_dropdown_placeholder_is_the_header_or_a_generic_prompt(self) -> None:
        (with_header,) = _selects(_view(_question(5, header="Deploy target")))
        (without,) = _selects(_view(_question(5)))
        assert with_header.placeholder == "Deploy target"
        assert without.placeholder == "Choose an option…"

    @pytest.mark.asyncio
    async def test_dropdown_shows_descriptions_and_omits_empty_ones(self) -> None:
        q = _question(5)
        q.options[1].description = ""
        (select,) = _selects(_view(q))
        assert select.options[0].description == "desc0"
        assert select.options[1].description is None

    @pytest.mark.asyncio
    async def test_dropdown_values_name_options_by_index(self) -> None:
        (select,) = _selects(_view(_question(5)))
        assert [o.value for o in select.options] == [f"option:{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_dropdown_offers_at_most_25_options(self) -> None:
        """Discord's ceiling for one Select; a multiSelect may pick all of those."""
        (select,) = _selects(_view(_question(30, multi_select=True)))
        assert len(select.options) == 25
        assert select.options[-1].label == "opt24"
        assert select.max_values == 25

    @pytest.mark.asyncio
    async def test_question_without_options_offers_only_free_text(self) -> None:
        view = _view(AskQuestion(question="Why?"))
        assert [c.custom_id for c in view.children] == ["ask_4242_0_other"]

    @pytest.mark.asyncio
    async def test_other_button_reads_pencil_other(self) -> None:
        other = _buttons(_view(_question(2)))[-1]
        assert other.label == "✏️ Other"
        assert other.style is discord.ButtonStyle.secondary


class TestAskViewTextLimits:
    """Discord rejects the whole message over one bad label, so labels are fitted."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("length", "shown"), [(79, 79), (80, 80), (81, 80), (300, 80)])
    async def test_button_label_is_cut_to_80_characters(self, length: int, shown: int) -> None:
        q = AskQuestion(question="Pick", options=[AskOption(label="x" * length)])
        assert _option_buttons(_view(q))[0].label == "x" * shown

    @pytest.mark.asyncio
    async def test_dropdown_description_is_cut_to_100_characters(self) -> None:
        q = _question(5)
        q.options[0].description = "d" * 100
        q.options[1].description = "d" * 101
        (select,) = _selects(_view(q))
        assert select.options[0].description == "d" * 100
        assert select.options[1].description == "d" * 100

    @pytest.mark.asyncio
    async def test_dropdown_label_is_cut_like_a_button_label(self) -> None:
        q = _question(5)
        q.options[2].label = "y" * 150
        (select,) = _selects(_view(q))
        assert select.options[2].label == "y" * 80

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unreadable", ["", "   "])
    async def test_an_unreadable_label_is_shown_as_its_number(self, unreadable: str) -> None:
        """#579: the option still exists in the TUI, so it keeps its place and gets "2."."""
        q = AskQuestion(
            question="Pick",
            options=[AskOption(label="A"), AskOption(label=unreadable), AskOption(label="C")],
        )
        assert [b.label for b in _option_buttons(_view(q))] == ["A", "2.", "C"]


class TestAskViewDelivery:
    """A press puts the chosen option on the bus and leaves an honest interim state."""

    @staticmethod
    def _interaction() -> MagicMock:
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.message.id = 9001
        interaction.message.created_at = None
        return interaction

    @pytest.mark.asyncio
    async def test_button_press_delivers_the_full_label_with_its_index(self) -> None:
        from c_lord.discord_ui.ask_bus import AskAnswerBus

        bus = AskAnswerBus()
        queue = bus.register(1)
        long_label = "z" * 120
        options = [AskOption(label="A"), AskOption(label=long_label)]
        q = AskQuestion(question="Pick", options=options)
        view = _view(q, thread_id=1, bus=bus)
        interaction = self._interaction()

        await _option_buttons(view)[1].callback(interaction)

        (answer,) = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert answer == long_label  # not the 80 characters the button shows
        assert answer.option_index == 1

    @pytest.mark.asyncio
    async def test_delivered_press_shows_sending_not_done(self) -> None:
        """#651: the bus taking the answer is not Claude receiving it."""
        from c_lord.discord_ui.ask_bus import AskAnswerBus

        bus = AskAnswerBus()
        bus.register(2)
        view = _view(_question(2, header="Env"), thread_id=2, bus=bus)
        interaction = self._interaction()

        await _option_buttons(view)[0].callback(interaction)

        kwargs = interaction.response.edit_message.await_args.kwargs
        assert kwargs["view"] is None  # the buttons are gone
        assert kwargs["embed"].title == "⏳ Env"
        assert "**送信中:** opt0" in kwargs["embed"].description
        assert view.is_finished()

    @pytest.mark.asyncio
    async def test_press_with_nobody_waiting_says_the_answer_did_not_arrive(self) -> None:
        from c_lord.discord_ui.ask_bus import CLOSE_TIMEOUT, AskAnswerBus

        bus = AskAnswerBus()
        bus.note_closed(3, CLOSE_TIMEOUT)
        view = _view(_question(2), thread_id=3, bus=bus)
        interaction = self._interaction()

        await _option_buttons(view)[1].callback(interaction)

        embed = interaction.response.edit_message.await_args.kwargs["embed"]
        assert embed.title == "⚠️ 回答が届きませんでした"
        assert "**選ばれた答え:** opt1" in embed.description
        assert "時間切れ" in embed.description

    @pytest.mark.asyncio
    async def test_single_select_dropdown_delivers_its_pick_at_once(self) -> None:
        from c_lord.discord_ui.ask_bus import AskAnswerBus

        bus = AskAnswerBus()
        queue = bus.register(4)
        view = _view(_question(6), thread_id=4, bus=bus)
        interaction = self._interaction()
        interaction.data = {"values": ["option:5"]}

        await _selects(view)[0].callback(interaction)

        assert await asyncio.wait_for(queue.get(), timeout=1.0) == ["opt5"]

    @pytest.mark.asyncio
    async def test_dropdown_value_naming_no_option_delivers_nothing(self) -> None:
        """An empty answer would read as "cancel the menu", so nothing is sent at all."""
        from c_lord.discord_ui.ask_bus import AskAnswerBus

        bus = AskAnswerBus()
        queue = bus.register(5)
        view = _view(_question(6), thread_id=5, bus=bus)
        interaction = self._interaction()
        interaction.data = {"values": ["option:6"]}

        await _selects(view)[0].callback(interaction)

        assert queue.empty()
        assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
        interaction.response.edit_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# AskView — free-text ✏️ Other gating (#251)
# ---------------------------------------------------------------------------


class TestAskViewOtherGating:
    """#251: plan-approval menus reuse AskView but must suppress the free-text
    ✏️ Other affordance — plan's "Tell Claude what to change" is a normal
    numbered option, not AskUserQuestion's "Type something." row, so a generic
    Other modal would mis-send keystrokes into the open TUI menu.
    """

    @staticmethod
    def _other_button_ids(q: AskQuestion) -> list[str]:
        from c_lord.discord_ui.ask_view import AskView

        view = AskView(q, thread_id=123, q_idx=0)
        return [
            cid
            for c in view.children
            if (cid := getattr(c, "custom_id", "")) and cid.endswith("_other")
        ]

    @pytest.mark.asyncio
    async def test_other_button_present_by_default(self) -> None:
        q = AskQuestion(
            question="Pick one",
            options=[AskOption(label="A"), AskOption(label="B")],
        )
        assert q.allow_other is True
        assert self._other_button_ids(q)  # AskUserQuestion keeps ✏️ Other

    @pytest.mark.asyncio
    async def test_other_button_suppressed_for_plan(self) -> None:
        q = AskQuestion(
            question="Would you like to proceed?",
            options=[AskOption(label="Yes"), AskOption(label="No")],
            allow_other=False,
        )
        assert self._other_button_ids(q) == []


class TestAskViewMultiSelectConfirm:
    """#418: a multiSelect question needs an explicit ✅ confirm button — the
    Select records the choice, the button submits it (single-select stays
    immediate, with no confirm button)."""

    @staticmethod
    def _confirm_button_ids(q: AskQuestion) -> list[str]:
        from c_lord.discord_ui.ask_view import AskView

        view = AskView(q, thread_id=123, q_idx=0)
        return [
            cid
            for c in view.children
            if (cid := getattr(c, "custom_id", "")) and cid.endswith("_confirm")
        ]

    @pytest.mark.asyncio
    async def test_confirm_button_present_for_multi_select(self) -> None:
        q = AskQuestion(
            question="pick many",
            options=[AskOption(label="A"), AskOption(label="B"), AskOption(label="C")],
            multi_select=True,
        )
        assert self._confirm_button_ids(q), "multiSelect must expose a ✅ confirm button"

    @pytest.mark.asyncio
    async def test_no_confirm_button_for_single_select(self) -> None:
        q = AskQuestion(
            question="pick one",
            options=[AskOption(label="A"), AskOption(label="B")],
        )
        assert self._confirm_button_ids(q) == []

    @pytest.mark.asyncio
    async def test_select_records_only_confirm_delivers_all(self) -> None:
        """Selecting in the dropdown records (does NOT deliver); pressing ✅
        confirm delivers every recorded value to the bus."""
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.discord_ui.ask_bus import ask_bus
        from c_lord.discord_ui.ask_view import AskView

        tid = 418_0010
        q = AskQuestion(
            question="pick",
            options=[AskOption(label="A"), AskOption(label="B"), AskOption(label="C")],
            multi_select=True,
        )
        view = AskView(q, thread_id=tid, q_idx=0)
        queue = ask_bus.register(tid)
        try:
            # 1) Select fires — must record only, not deliver.
            sel = MagicMock()
            # The values the rendered options carry — options are identified
            # by index, not label (#674).
            select = next(c for c in view.children if hasattr(c, "options"))
            sel.data = {"values": [o.value for o in select.options if o.label in ("A", "C")]}
            sel.response.edit_message = AsyncMock()
            await view._multi_select_record(sel)
            assert queue.empty(), "selection must not deliver before confirm"

            # 2) Confirm button delivers all recorded values.
            conf = MagicMock()
            conf.response.edit_message = AsyncMock()
            await view._confirm_callback(conf)
            delivered = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert delivered == ["A", "C"]
        finally:
            ask_bus.unregister(tid)

    @pytest.mark.asyncio
    async def test_confirm_without_selection_errors_and_does_not_deliver(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from c_lord.discord_ui.ask_bus import ask_bus
        from c_lord.discord_ui.ask_view import AskView

        tid = 418_0011
        q = AskQuestion(
            question="pick",
            options=[AskOption(label="A"), AskOption(label="B")],
            multi_select=True,
        )
        view = AskView(q, thread_id=tid, q_idx=0)
        queue = ask_bus.register(tid)
        try:
            conf = MagicMock()
            conf.response.send_message = AsyncMock()
            conf.response.edit_message = AsyncMock()
            await view._confirm_callback(conf)
            conf.response.send_message.assert_awaited()  # ephemeral "select first"
            assert queue.empty()
        finally:
            ask_bus.unregister(tid)
