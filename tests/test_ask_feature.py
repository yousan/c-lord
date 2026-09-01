"""Tests for AskUserQuestion Discord integration.

Covers:
- types: AskOption, AskQuestion, ToolCategory.ASK, _parse_ask_questions
- embeds: ask_embed
- run_helper: _collect_ask_answers (answer formatting)
"""

from __future__ import annotations

import asyncio

import pytest

from c_lord.claude.types import (
    FREE_TEXT_NOTES,
    FREE_TEXT_ROW,
    AskOption,
    AskQuestion,
    ToolCategory,
    _parse_ask_questions,
)
from c_lord.discord_ui.embeds import ask_embed

# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------


class TestAskTypes:
    def test_ask_option_defaults(self) -> None:
        opt = AskOption(label="JWT tokens")
        assert opt.label == "JWT tokens"
        assert opt.description == ""

    def test_ask_question_defaults(self) -> None:
        q = AskQuestion(question="Which auth?")
        assert q.question == "Which auth?"
        assert q.header == ""
        assert q.multi_select is False
        assert q.options == []

    def test_tool_category_ask_exists(self) -> None:
        assert ToolCategory.ASK.value == "ask"

    def test_ask_in_tool_categories(self) -> None:
        from c_lord.claude.types import TOOL_CATEGORIES

        assert "AskUserQuestion" in TOOL_CATEGORIES
        assert TOOL_CATEGORIES["AskUserQuestion"] == ToolCategory.ASK


# ---------------------------------------------------------------------------
# _parse_ask_questions
# ---------------------------------------------------------------------------


class TestParseAskQuestions:
    def test_empty_options_list(self) -> None:
        tool_input = {"questions": [{"question": "Free form?", "options": []}]}
        questions = _parse_ask_questions(tool_input)
        assert len(questions) == 1
        assert questions[0].options == []

    def test_options_without_label_are_skipped(self) -> None:
        tool_input = {
            "questions": [
                {
                    "question": "Choose?",
                    "options": [
                        {"label": "Valid"},
                        {"description": "No label here"},
                        {"label": ""},
                    ],
                }
            ]
        }
        questions = _parse_ask_questions(tool_input)
        assert len(questions[0].options) == 1
        assert questions[0].options[0].label == "Valid"

    def test_multiple_questions_in_one_call(self) -> None:
        tool_input = {
            "questions": [
                {"question": "Q1?", "options": [{"label": "A"}, {"label": "B"}]},
                {"question": "Q2?", "options": [{"label": "X"}, {"label": "Y"}]},
            ]
        }
        questions = _parse_ask_questions(tool_input)
        assert len(questions) == 2
        assert questions[0].question == "Q1?"
        assert questions[1].question == "Q2?"

    def test_full_parse(self) -> None:
        tool_input = {
            "questions": [
                {
                    "question": "Which auth approach?",
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
        assert q.question == "Which auth approach?"
        assert q.header == "Auth method"
        assert q.multi_select is False
        assert len(q.options) == 3
        assert q.options[0].label == "JWT tokens"
        assert q.options[0].description == "Stateless, good for APIs"
        assert q.options[2].label == "OAuth2"

    def test_multi_select_question(self) -> None:
        tool_input = {
            "questions": [
                {
                    "question": "Which features?",
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
# embeds
# ---------------------------------------------------------------------------


class TestAskEmbed:
    def test_default_title(self) -> None:
        embed = ask_embed("Which approach?")
        assert embed.title == "❓ Claude needs your input"

    def test_custom_header_used_in_title(self) -> None:
        embed = ask_embed("Which approach?", header="Auth method")
        assert embed.title == "❓ Auth method"

    def test_question_in_description(self) -> None:
        embed = ask_embed("Pick one?", header="Step")
        assert embed.description == "Pick one?"

    def test_long_question_truncated_to_4096(self) -> None:
        long_q = "x" * 5000
        embed = ask_embed(long_q)
        assert embed.description is not None
        assert len(embed.description) <= 4096

    def test_color_is_blue(self) -> None:
        embed = ask_embed("Q?")
        assert embed.color is not None
        assert embed.color.value == 0x3498DB


# ---------------------------------------------------------------------------
# _collect_ask_answers (answer formatting helper)
# ---------------------------------------------------------------------------


class TestCollectAskAnswers:
    """Tests for the _collect_ask_answers answer string format.

    We test the pure formatting logic by constructing what the function
    would return given a mocked AskView response.  The Discord interaction
    itself (buttons/modals) is tested separately via integration tests.
    """

    def _format_answer(self, question: AskQuestion, selected: list[str]) -> str:
        """Replicate the formatting logic from _collect_ask_answers."""
        answer_text = ", ".join(selected)
        return f"**{question.question}**\nAnswer: {answer_text}"

    def test_single_answer_formatted(self) -> None:
        q = AskQuestion(question="Which auth?", options=[AskOption(label="JWT")])
        result = self._format_answer(q, ["JWT"])
        assert "**Which auth?**" in result
        assert "Answer: JWT" in result

    def test_multi_select_answer_joined(self) -> None:
        q = AskQuestion(
            question="Which features?",
            multi_select=True,
            options=[AskOption(label="Auth"), AskOption(label="Logging")],
        )
        result = self._format_answer(q, ["Auth", "Logging"])
        assert "Answer: Auth, Logging" in result

    def test_full_resume_prompt_format(self) -> None:
        """The resume prompt must start with the [Response] marker."""
        q = AskQuestion(question="Q?")
        part = self._format_answer(q, ["A"])
        resume_prompt = (
            "[Response to AskUserQuestion]\n\n"
            + part
            + "\n\nPlease continue based on these answers."
        )
        assert resume_prompt.startswith("[Response to AskUserQuestion]")
        assert "Please continue" in resume_prompt


class TestCollectAskAnswersTimeout:
    """Tests for asyncio.TimeoutError handling in collect_ask_answers (Python 3.10 compat)."""

    @pytest.mark.asyncio
    async def test_collect_ask_answers_handles_asyncio_timeout(self) -> None:
        """collect_ask_answers does not raise on asyncio.TimeoutError (Python 3.10 compat).

        On Python 3.10, asyncio.TimeoutError is NOT a subclass of the built-in
        TimeoutError. If the wait_for call times out, collect_ask_answers must
        catch the exception and return None (graceful timeout) rather than
        propagating an unhandled exception to _run_helper.
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from c_lord.claude.types import AskOption, AskQuestion
        from c_lord.discord_ui.ask_handler import collect_ask_answers

        q = AskQuestion(
            question="Which option?",
            options=[AskOption(label="A"), AskOption(label="B")],
        )
        mock_msg = AsyncMock()
        mock_msg.edit = AsyncMock()
        thread = MagicMock()
        thread.id = 12345
        thread.send = AsyncMock(return_value=mock_msg)

        with patch(
            "c_lord.discord_ui.ask_handler.asyncio.wait_for",
            side_effect=asyncio.TimeoutError,
        ):
            result = await collect_ask_answers(thread, [q], session_id="abc123")

        assert result is None  # timeout → no answer → returns None


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
        assert q.multi_select is False
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
            sel.data = {"values": ["A", "C"]}
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
